"""core/oi_study.py：外資期貨未平倉偏向的檢驗。

和 test_structure_study 一樣，重點是「檢驗不會騙人」：解析公開 CSV 的細節（編碼、價差單、盤後、缺值）、
訊號與目標的對齊（收盤後才知道的資料不能對到同一天的漲跌）、相關係數的算法、假陽性率、下載不會把錯誤頁存成資料。"""
from __future__ import annotations

import json
import random
import urllib.parse
from datetime import date, timedelta
from io import BytesIO

import numpy as np
import pytest

import core.oi_study as oi

OI_HEAD = ("日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,空方交易契約金額(千元),多空交易口數淨額,多空交易契約金額淨額(千元),"
           "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)")
TX_HEAD = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,"
           "歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量")


def oi_row(d, who, net, longs=1000, shorts=900):
    return f"{d},臺股期貨,{who},1,1,1,1,0,0,{longs},1,{shorts},1,{net},1"


def tx_row(d, exp, o, c, vol=100, session="一般"):
    return f"{d},TX,{exp}  ,{o},{max(o, c)},{min(o, c)},{c},0,0.00%,{vol},{c},-,-,-,1,1,,{session},,"


def write(path, head, rows, enc="cp950"):
    path.write_bytes(("\r\n".join([head, *rows]) + "\r\n").encode(enc))


# ── 解析 ──────────────────────────────────────────────────────────

def test_foreign_net_oi_is_read_from_the_foreign_rows_only_with_commas_and_negatives(tmp_path):
    write(tmp_path / "a.csv", OI_HEAD, [oi_row("2026/09/01", "自營商", -990), oi_row("2026/09/01", "投信", 76027), oi_row("2026/09/01", "外資及陸資", -7870),
                                       oi_row("2026/09/02", "外資及陸資", '"-1,234"'), oi_row("2026/09/03", "外資及陸資", "")])
    got = oi.load_foreign_net_oi(tmp_path)
    assert got == {"2026-09-01": -7870.0, "2026-09-02": -1234.0}                    # 自營商、投信不算；空值的那天略過；逗號與負號正確


def test_the_foreign_category_is_matched_by_prefix(tmp_path):
    write(tmp_path / "a.csv", OI_HEAD, [oi_row("2024/01/02", "外資及陸資(不含外資自營商)", 500), oi_row("2024/01/03", "外資", 600)])
    assert oi.load_foreign_net_oi(tmp_path) == {"2024-01-02": 500.0, "2024-01-03": 600.0}


def test_tx_day_session_uses_the_nearest_month_with_volume_and_ignores_spreads_after_hours_and_missing_prices(tmp_path):
    write(tmp_path / "tx.csv", TX_HEAD, [
        tx_row("2026/09/01", "202610", 200, 210, vol=5000),                          # 較遠的月份
        tx_row("2026/09/01", "202609", 100, 105, vol=90000),                         # 最近月（要用這個）
        tx_row("2026/09/01", "202609", 300, 400, vol=10, session="盤後"),            # 盤後不算
        tx_row("2026/09/01", "202609/202610", 1, 2, vol=50),                         # 價差單不算
        tx_row("2026/09/02", "202609", "-", "-", vol=0),                             # 沒成交、沒價格
        tx_row("2026/09/03", "202609", 110, 108, vol=0),                             # 成交量 0 的近月不用
        tx_row("2026/09/03", "202610", 120, 130, vol=40),
    ])
    assert oi.load_tx_day_sessions(tmp_path) == {"2026-09-01": (100.0, 105.0), "2026-09-03": (120.0, 130.0)}


def test_an_error_page_saved_as_a_csv_is_refused_not_parsed(tmp_path):
    (tmp_path / "bad.csv").write_bytes('<!DOCTYPE HTML><script>alert("DateTime error")</script>'.encode("cp950"))
    with pytest.raises(ValueError, match="錯誤頁"):
        oi.load_foreign_net_oi(tmp_path)
    with pytest.raises(ValueError, match="錯誤頁"):
        oi.load_tx_day_sessions(tmp_path)


# ── 訊號與目標的對齊 ───────────────────────────────────────────────

def make_series(n, rng, y_rule, oi_rule=None):
    """n 個交易日：net[t]＝外資淨未平倉；(open, close)[t]＝日盤。y_rule(t, net, rng) 決定當天日盤報酬(%)。"""
    d0, days, net, sess, cur = date(2024, 1, 2), [], {}, {}, 0.0
    for k in range(n):
        d = d0 + timedelta(days=k)
        if d.weekday() >= 5:
            continue
        cur += (oi_rule(k, rng) if oi_rule else rng.gauss(0, 500))
        iso = d.isoformat()
        net[iso] = cur
        sess[iso] = None
        days.append(iso)
    for t, iso in enumerate(days):
        ret = y_rule(t, [net[x] for x in days], rng)
        sess[iso] = (10000.0, 10000.0 * (1 + ret / 100.0))
    return net, sess


def test_the_signal_known_after_the_close_is_paired_with_the_next_days_session_not_the_same_day():
    """陷阱：外資未平倉的變化和『同一天』的漲跌高度相關（當天漲，外資加碼）——但那是收盤後才知道的。隔天的報酬若和它無關，檢驗不能報出相關。"""
    rng = random.Random(1)

    def y(t, nets, rng):
        return (nets[t] - nets[t - 1]) / 1000.0 + rng.gauss(0, 0.3) if t else 0.0           # 當天的報酬 ∝ 當天的未平倉變化；明天的報酬和今天的訊號無關
    net, sess = make_series(900, rng, y)
    ds = oi.build_dataset(net, sess)
    same_day = oi.spearman(ds["d1"], np.array([(sess[d][1] - sess[d][0]) / sess[d][0] * 100 for d in ds["dates"]]))
    assert same_day[1] > 20                                                                  # 資料確實有「同日相關」這個陷阱
    res = oi.study(ds)
    assert all(abs(t["t"]) < 3 for t in res["tests"].values()), res["tests"]                 # 但隔天不能報出相關


def test_a_planted_next_day_effect_is_found():
    rng = random.Random(2)

    def y(t, nets, rng):
        return 0.4 * (nets[t - 1] - nets[t - 2]) / 500.0 + rng.gauss(0, 0.5) if t > 1 else 0.0      # 今天的收益取決於昨天收盤後的訊號
    net, sess = make_series(900, rng, y)
    res = oi.study(oi.build_dataset(net, sess))
    d1 = res["tests"]["d1"]
    assert d1["t"] > 8 and d1["verdict"] == "過關" and d1["r"] > 0.25 and d1["same_sign_in_both_halves"]
    assert d1["top_quintile_mean_pct"] > d1["all_mean_pct"] > d1["bottom_quintile_mean_pct"]


def test_the_z_score_uses_only_the_previous_20_days():
    rng = random.Random(3)
    net, sess = make_series(120, rng, lambda t, nets, rng: rng.gauss(0, 0.5))
    ds = oi.build_dataset(net, sess)
    days = [d for d in sess if d in net]
    for k in (0, 7, 40):
        t = ds["dates"][k]
        i = days.index(t)
        hist = np.array([net[d] for d in days[i - 20:i]])
        assert ds["z"][k] == pytest.approx((net[t] - hist.mean()) / hist.std(ddof=1))
        assert ds["d1"][k] == pytest.approx(net[t] - net[days[i - 1]]) and ds["d5"][k] == pytest.approx(net[t] - net[days[i - 5]])
        assert ds["y"][k] == pytest.approx((sess[days[i + 1]][1] - sess[days[i + 1]][0]) / sess[days[i + 1]][0] * 100)


def test_changing_the_future_does_not_change_an_earlier_signal():
    rng = random.Random(4)
    net, sess = make_series(100, rng, lambda t, nets, rng: rng.gauss(0, 0.5))
    a = oi.build_dataset(net, sess)
    net2 = dict(net)
    last = list(net2)[-1]
    net2[last] += 1e9                                                                        # 把最後一天的未平倉改得很誇張
    b = oi.build_dataset(net2, sess)
    k = len(b["z"]) - 3
    assert np.array_equal(a["z"][:k], b["z"][:k]) and np.array_equal(a["d1"][:k], b["d1"][:k])


def test_a_long_gap_in_the_data_is_not_bridged():
    rng = random.Random(5)
    net, sess = make_series(120, rng, lambda t, nets, rng: rng.gauss(0, 0.5))
    days = list(net)
    for d in days[60:75]:                                                                    # 中間缺了 15 個交易日
        del net[d]
        del sess[d]
    ds = oi.build_dataset(net, sess)
    gap_start = days[59]
    assert gap_start not in set(ds["dates"])                                                 # 缺口前一天的『隔天』是三週後，不能當成隔天


# ── 統計 ──────────────────────────────────────────────────────────

def test_rank_gives_average_ranks_to_ties():
    assert list(oi.rank(np.array([10.0, 20.0, 20.0, 30.0]))) == [1.0, 2.5, 2.5, 4.0]
    assert list(oi.rank(np.array([3.0, 1.0, 2.0]))) == [3.0, 1.0, 2.0]
    assert list(oi.rank(np.array([5.0, 5.0, 5.0]))) == [2.0, 2.0, 2.0]


def test_spearman_known_values():
    x = np.arange(1.0, 51.0)
    assert oi.spearman(x, x ** 3)[0] == pytest.approx(1.0)                                   # 單調關係 → 1（不是 Pearson）
    assert oi.spearman(x, -x)[0] == pytest.approx(-1.0)
    assert oi.spearman(x, np.ones(50)) == (0.0, 0.0)                                         # 一邊沒有變化 → 沒有相關，不是除以零
    assert oi.spearman(x[:5], x[:5]) == (0.0, 0.0)                                           # 太少不算
    r, t = oi.spearman(np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12.0]), np.array([2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11.0]))
    assert r == pytest.approx(1 - 6 * 12 / (12 * 143)) and t == pytest.approx(r * np.sqrt(10 / (1 - r * r)))


def test_false_positive_rate_on_unrelated_series_is_small():
    hits, ts = 0, []
    for seed in range(60):
        rng = random.Random(seed)
        net, sess = make_series(760, rng, lambda t, nets, rng: rng.gauss(0, 0.8))             # 約 540 個交易日，和真實資料同量級
        for st in oi.study(oi.build_dataset(net, sess))["tests"].values():
            ts.append(st["t"])
            hits += st["verdict"] == "過關"
    assert hits <= 5, hits                                                                  # 180 個檢定，每個約 1.7% → 預期約 3 個
    assert abs(float(np.mean(ts))) < 0.25 and 0.8 < float(np.std(ts)) < 1.2


def test_the_thresholds_are_the_pre_registered_ones():
    assert oi.N_TESTS == 3 and 2.39 < oi.Z_CLAIM < 2.40 and oi.Z_HINT == 2.0
    assert [oi.verdict(t) for t in (2.5, -2.5, 2.2, 1.9, 0.0)] == ["過關", "過關", "線索", "沒有", "沒有"]


def test_too_few_days_is_an_error():
    rng = random.Random(6)
    net, sess = make_series(60, rng, lambda t, nets, rng: 0.0)
    with pytest.raises(ValueError, match="太少"):
        oi.study(oi.build_dataset(net, sess))


def test_the_report_names_the_verdict_and_the_power():
    rng = random.Random(7)
    net, sess = make_series(900, rng, lambda t, nets, rng: rng.gauss(0, 0.5))
    text = oi.format_study(oi.study(oi.build_dataset(net, sess)))
    assert "Spearman" in text and "判定（3 個檢定" in text and "可看出 |r| ≥" in text and "1日變化" in text and "5日變化" in text


# ── 下載 ──────────────────────────────────────────────────────────

class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_downloads_missing_periods_skips_finished_ones_and_never_saves_an_error_page(tmp_path):
    calls = []

    def opener(req, timeout=0):
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        calls.append((req.full_url.rsplit("/", 1)[1], form["queryStartDate"], form["queryEndDate"]))
        if form["queryStartDate"].startswith("2023"):                                       # 太舊：伺服器回錯誤頁
            return FakeResponse('<script>alert("DateTime error")</script>'.encode("cp950"))
        head = oi.OI_HEADER if "Contracts" in req.full_url else oi.TX_HEADER
        return FakeResponse((head + "\r\nx\r\n").encode("cp950"))

    today = date(2024, 2, 15)
    first = oi.fetch(tmp_path, start_year=2023, today=today, pause=0, opener=opener)
    assert first["failed"] > 0 and first["downloaded"] > 0
    assert not any(f.read_bytes().startswith(b"<script") for f in tmp_path.rglob("*.csv"))       # 錯誤頁沒有被存起來
    assert (tmp_path / "oi" / "oi_2024_0101.csv").exists() and (tmp_path / "tx" / "tx_2024_02.csv").exists()
    assert not (tmp_path / "oi" / "oi_2023_0101.csv").exists()
    n_before = len(calls)
    second = oi.fetch(tmp_path, start_year=2024, today=today, pause=0, opener=opener)
    # 已經結束的期不重抓（2024-01 的 TX 已經有了）；還沒結束的（2024Q1 的三大法人、2024-02 的 TX）每次重抓
    assert set(calls[n_before:]) == {("dlFutContractsDateDown", "2024/01/01", "2024/02/15"), ("futDataDown", "2024/02/01", "2024/02/15")}
    assert second == {"downloaded": 2, "skipped": 1, "failed": 0}


def test_fetch_can_be_limited_to_one_dataset(tmp_path):
    seen = []

    def opener(req, timeout=0):
        seen.append(req.full_url.rsplit("/", 1)[1])
        return FakeResponse(((oi.OI_HEADER if "Contracts" in req.full_url else oi.TX_HEADER) + "\r\nx\r\n").encode("cp950"))

    oi.fetch(tmp_path, start_year=2024, today=date(2024, 2, 15), pause=0, opener=opener, only=("tx",))
    assert set(seen) == {"futDataDown"} and not (tmp_path / "oi").exists() and (tmp_path / "tx" / "tx_2024_01.csv").exists()


def test_fetch_requests_are_spaced_out(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(oi.time, "sleep", lambda s: sleeps.append(s))
    oi.fetch(tmp_path, start_year=2024, today=date(2024, 1, 31), pause=0.7,
             opener=lambda req, timeout=0: FakeResponse((oi.OI_HEADER + "\r\n").encode("cp950") if "Contracts" in req.full_url else (oi.TX_HEADER + "\r\n").encode("cp950")))
    assert sleeps and all(s == 0.7 for s in sleeps)                                          # 對公開網站要客氣


# ── 命令列 ────────────────────────────────────────────────────────

def test_cli_runs_on_downloaded_files_and_writes_json(tmp_path, capsys):
    rng = random.Random(8)
    net, sess = make_series(560, rng, lambda t, nets, rng: rng.gauss(0, 0.6))
    (tmp_path / "oi").mkdir()
    (tmp_path / "tx").mkdir()
    write(tmp_path / "oi" / "a.csv", OI_HEAD, [oi_row(d.replace("-", "/"), "外資及陸資", v) for d, v in net.items()])
    write(tmp_path / "tx" / "a.csv", TX_HEAD, [tx_row(d.replace("-", "/"), "202610", sess[d][0], sess[d][1]) for d in sess])
    out = tmp_path / "o.json"
    assert oi.main(["--dir", str(tmp_path), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "外資期貨未平倉" in text and "判定（3 個檢定" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["tests"]) == {"d1", "d5", "z"} and data["n"] > 100


def test_cli_without_data_says_to_fetch_first(tmp_path, capsys):
    assert oi.main(["--dir", str(tmp_path / "nothing")]) == 2
    assert "fetch" in capsys.readouterr().err


# ── 補強：資料列的排列順序不能影響結果（突變驗證發現測試太剛好）──────────────────

def test_foreign_rows_are_picked_even_when_other_identities_come_after_them(tmp_path):
    write(tmp_path / "a.csv", OI_HEAD, [oi_row("2026/09/01", "外資及陸資", -7870), oi_row("2026/09/01", "自營商", -990), oi_row("2026/09/01", "投信", 76027)])
    assert oi.load_foreign_net_oi(tmp_path) == {"2026-09-01": -7870.0}                 # 真實檔案是外資排最後；順序一換，不能就取到別人的


def test_after_hours_and_spread_rows_are_ignored_even_when_they_come_first(tmp_path):
    write(tmp_path / "tx.csv", TX_HEAD, [
        tx_row("2026/09/01", "202609", 300, 400, vol=10, session="盤後"),             # 盤後（同一個月份）排在一般交易時段前面
        tx_row("2026/09/01", "202609/202610", 1, 2, vol=50),                           # 價差單
        tx_row("2026/09/01", "202609", 100, 105, vol=90000),
    ])
    assert oi.load_tx_day_sessions(tmp_path) == {"2026-09-01": (100.0, 105.0)}
