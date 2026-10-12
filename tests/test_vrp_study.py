"""core/vrp_study.py：選擇權 IV 與之後實際波動 RV 的檢驗。

重點：歷史 IV 要算對（用已知波動度生出價格、反推要還原；只取月選擇權、一般時段、距到期 ≥ 7 天的最近月；缺值、雜訊價格不能亂算）、
RV 的視窗與年化要對、IV 百分位不偷看未來、Newey-West 的標準誤正確、假陽性率正常、植入的溢酬與條件效果檢得出來、下載不會把錯誤頁當資料。"""
from __future__ import annotations

import io
import json
import urllib.parse
from datetime import date, datetime, timedelta

import numpy as np
import pytest

import core.vrp_study as vs
from core.iv_monitor import black76
from test_trend_study import make_contracts, third_wednesday

HEAD = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,漲跌價,漲跌%,契約到期日,"


def line(d, month, k, side, settle, session="一般", exp=None, contract="TXO"):
    exp = exp if exp is not None else ""
    return f"{d},{contract},{month},{k:.4f},{side},-,-,-,-,10,{settle},5,-,-,1,1,,{session},,,{exp},"


def chain(d, month, F, sigma, T, step=100, width=1500, **kw):
    """用 Black-76 生出一整排履約價的買賣權結算價（取整數點，像真實的結算價）。"""
    rows = []
    k = round(F / step) * step - width
    while k <= F + width:
        for side, is_call in (("買權", True), ("賣權", False)):
            p = round(black76(F, k, T, sigma, 0.0, is_call))
            if p > 0:
                rows.append(line(d, month, k, side, p, **kw))
        k += step
    return rows


def csv_text(rows):
    return "\r\n".join([HEAD, *rows]) + "\r\n"


# ── 歷史 IV ───────────────────────────────────────────────────────

def test_the_known_volatility_is_recovered_from_settlement_prices():
    d = "2026/09/01"
    T = (datetime(2026, 9, 16, 13, 30) - datetime(2026, 9, 1, 13, 45)).total_seconds() / (365 * 86400)
    rows = vs.reduce_month(csv_text(chain(d, "202609", 47180.0, 0.22, T, exp="2026/09/16")))
    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2026-09-01" and r["expiry"] == "202609" and r["expiry_date"] == "2026-09-16"
    assert r["iv"] == pytest.approx(22.0, abs=0.5) and r["F"] == pytest.approx(47180.0, abs=60) and abs(r["K"] - 47180.0) <= 100 and 14.9 < r["T_days"] < 15.1


def test_only_monthly_regular_session_contracts_are_used_and_the_nearest_with_seven_days_is_picked():
    T1 = 15 / 365
    rows = chain("2026/09/01", "202609", 47000.0, 0.20, T1, exp="2026/09/16")
    rows += chain("2026/09/01", "202609W1", 47000.0, 0.60, T1, exp="2026/09/02")                  # 週選（IV 故意設很不一樣）
    rows += chain("2026/09/01", "202609F1", 47000.0, 0.60, T1, exp="2026/09/04")
    rows += chain("2026/09/01", "202609", 47000.0, 0.60, T1, session="盤後", exp="2026/09/16")      # 盤後
    rows += chain("2026/09/01", "202610", 47000.0, 0.30, 45 / 365, exp="2026/10/21")                 # 次月
    r = vs.reduce_month(csv_text(rows))[0]
    assert r["expiry"] == "202609" and r["iv"] == pytest.approx(20.0, abs=0.5)
    # 9/10 距 9/16 只剩 6 天 → 不到 7 天，要換成次月
    rows = chain("2026/09/10", "202609", 47000.0, 0.20, 6 / 365, exp="2026/09/16") + chain("2026/09/10", "202610", 47000.0, 0.30, 41 / 365, exp="2026/10/21")
    r = vs.reduce_month(csv_text(rows))[0]
    assert r["expiry"] == "202610" and r["iv"] == pytest.approx(30.0, abs=0.7)
    # 剛好 7 天以上（9/9 13:45 → 9/16 13:30 差 6.99 天）算不到；9/8 算
    rows = chain("2026/09/08", "202609", 47000.0, 0.20, 8 / 365, exp="2026/09/16") + chain("2026/09/08", "202610", 47000.0, 0.30, 43 / 365, exp="2026/10/21")
    assert vs.reduce_month(csv_text(rows))[0]["expiry"] == "202609"


def test_the_forward_comes_from_put_call_parity_so_a_strike_off_the_money_does_not_bias_iv():
    T = 20 / 365
    for F in (46930.0, 47000.0, 47040.0, 47089.0):
        r = vs.reduce_month(csv_text(chain("2026/09/01", "202609", F, 0.25, T, exp="2026/09/21")))[0]
        assert r["iv"] == pytest.approx(25.0, abs=0.6) and r["F"] == pytest.approx(F, abs=40), F


def test_missing_zero_and_one_sided_prices_are_skipped_not_guessed():
    T = 20 / 365
    rows = chain("2026/09/01", "202609", 47000.0, 0.25, T, exp="2026/09/21")
    kept = [r for r in rows if "47000.0000" not in r]                                              # 把最平價那檔的買賣權都拿掉
    r = vs.reduce_month(csv_text(kept))[0]
    assert r["K"] != 47000.0 and r["iv"] == pytest.approx(25.0, abs=1.0)                           # 改用旁邊有雙邊價格的履約價
    only_calls = [r for r in rows if "買權" in r]
    assert vs.reduce_month(csv_text(only_calls)) == []                                             # 只有買權、沒有賣權 → 算不出來
    zero = [r.replace(",買權,", ",買權,").replace(",-,-,-,-,10,", ",-,-,-,-,10,") for r in rows]
    assert vs.reduce_month(csv_text(zero[:2])) == []                                               # 履約價太少


def test_an_absurd_iv_is_dropped():
    T = 20 / 365
    assert vs.reduce_month(csv_text(chain("2026/09/01", "202609", 47000.0, 3.0, T, exp="2026/09/21"))) == []        # 300% 不合理（超出 3~150）


def test_without_the_expiry_column_the_third_wednesday_is_used():
    T = 20 / 365
    r = vs.reduce_month(csv_text(chain("2026/09/01", "202609", 47000.0, 0.2, T)))[0]                              # 沒有『契約到期日』
    assert r["expiry_date"] == third_wednesday(2026, 9).isoformat()


# ── 資料集：RV 與 IV 百分位 ─────────────────────────────────────────

def synthetic(n_days=900, seed=1):
    """標的日報酬 g、每天一筆 IV（隨時間變動），到期日用每個月的第三個週三。"""
    rng = np.random.default_rng(seed)
    g = rng.normal(0, 0.01, n_days)
    contracts = make_contracts(g, basis_step=0.0)
    dates = list(contracts)
    iv = {}
    for d in dates:
        dd = date.fromisoformat(d)
        e = third_wednesday(dd.year, dd.month)
        month = (dd.year, dd.month)
        if dd > e - timedelta(days=7):
            month = (dd.year + (dd.month == 12), dd.month % 12 + 1)
            e = third_wednesday(*month)
        iv[d] = {"expiry": f"{month[0]}{month[1]:02d}", "expiry_date": e.isoformat(), "T_days": float((e - dd).days), "F": 10000.0, "K": 10000.0, "iv": 16.0}
    return g, contracts, iv


def test_rv_is_the_annualised_std_of_the_returns_between_the_day_and_the_expiry():
    g, contracts, iv = synthetic()
    ds = vs.build_dataset(iv, contracts)
    series = vs.build_series(contracts)
    dates = series["dates"]
    for k in (5, 100, 400):
        d = str(ds["dates"][k])
        i = dates.index(d)
        end = iv[d]["expiry_date"]
        rets = [series["r_close"][j] for j in range(i, len(dates) - 1) if dates[j + 1] <= end]
        assert len(rets) >= vs.MIN_RETURNS
        assert ds["rv"][k] == pytest.approx(np.sqrt(np.mean(np.square(rets)) * 252) * 100.0)
        assert dates[i + 1] > d and all(dates[j + 1] <= end for j in range(i, i + len(rets)))      # 只用 t 之後的報酬，不含 t 以前的


def test_rv_is_about_the_true_volatility_for_a_random_walk():
    _, contracts, iv = synthetic(1500, seed=3)
    ds = vs.build_dataset(iv, contracts)
    assert float(ds["rv"].mean()) == pytest.approx(0.01 * np.sqrt(252) * 100, rel=0.06)


def test_the_iv_percentile_uses_only_the_previous_250_observations():
    _, contracts, iv = synthetic(700, seed=2)
    rng = np.random.default_rng(0)
    for d in iv:
        iv[d]["iv"] = float(rng.uniform(10, 30))
    ds = vs.build_dataset(iv, contracts)
    keys = list(iv)
    for k in (300, 400, 500):
        d = str(ds["dates"][k])
        j = keys.index(d)
        past = np.array([iv[x]["iv"] for x in keys[j - 250:j]])
        assert ds["pct"][k] == pytest.approx(np.sum(past < iv[d]["iv"]) / 250 * 100)
    assert np.isnan(ds["pct"][:3]).all()                                                          # 前 250 筆沒有百分位
    # 改掉未來的 IV，不會改變較早的百分位
    iv2 = {d: dict(v) for d, v in iv.items()}
    for d in keys[600:]:
        iv2[d]["iv"] = 99.0
    ds2 = vs.build_dataset(iv2, contracts)
    n = np.sum(ds["dates"] < keys[600])
    assert np.array_equal(ds["pct"][:n], ds2["pct"][:n], equal_nan=True)


# ── 統計 ──────────────────────────────────────────────────────────

def test_the_cluster_robust_t_matches_a_by_hand_computation():
    rng = np.random.default_rng(4)
    clusters = np.repeat(np.arange(40), 8)
    y = rng.normal(0.5, 1.0, 320) + np.repeat(rng.normal(0, 1, 40), 8)               # 同一個到期月內共用一個偏移（巢狀視窗的相依）
    r = vs.mean_t(y, clusters)
    m = y.mean()
    scores = [(y[clusters == g] - m).sum() for g in range(40)]
    se = np.sqrt(40 / 39 * sum(sc * sc for sc in scores)) / 320
    assert r["mean"] == pytest.approx(m) and r["se"] == pytest.approx(se) and r["t"] == pytest.approx(m / se) and r["clusters"] == 40


def test_clustering_makes_the_standard_error_larger_when_days_within_an_expiry_move_together():
    rng = np.random.default_rng(5)
    clusters = np.repeat(np.arange(100), 20)
    y = np.repeat(rng.normal(0, 1, 100), 20) + rng.normal(0, 0.3, 2000)               # 同一個月內幾乎一樣
    naive = abs(y.mean()) / (y.std(ddof=1) / np.sqrt(len(y)))
    assert abs(vs.mean_t(y, clusters)["t"]) < 0.5 * naive or naive < 1.0
    assert vs.mean_t(y, clusters)["se"] > 3 * (y.std(ddof=1) / np.sqrt(len(y)))


def test_too_few_observations_or_clusters_give_no_t():
    assert vs.mean_t(np.arange(10.0), np.arange(10))["t"] == 0.0
    assert vs.mean_t(np.arange(100.0), np.repeat(np.arange(5), 20))["t"] == 0.0         # 群組太少，標準誤不可靠


def fake_dataset(n=2600, vrp=0.0, low_effect=0.0, high_effect=0.0, seed=0, noise=2.0):
    """合成資料集：IV＝隨時間緩慢變動（AR）。RV＝IV − VRP（− 條件效果）＋雜訊；雜訊的結構照真實資料：
    同一個到期月（21 個交易日）共用一個偏移（視窗都延伸到同一個到期日）＋短期的小雜訊，到期月與到期月之間才獨立。"""
    rng = np.random.default_rng(seed)
    iv = np.empty(n)
    x = 18.0
    for i in range(n):
        x = 18.0 + 0.97 * (x - 18.0) + rng.normal(0, 0.8)
        iv[i] = max(x, 5.0)
    pct = np.full(n, np.nan)
    for i in range(250, n):
        pct[i] = np.sum(iv[i - 250:i] < iv[i]) / 250 * 100
    block = np.arange(n) // 21
    shared = rng.normal(0, noise, block.max() + 1)[block]
    short = np.convolve(rng.normal(0, 0.8, n + 4), np.ones(5) / np.sqrt(5), "valid")
    rv = iv - vrp - np.where(pct <= 20, -low_effect, 0.0) - np.where(pct >= 80, high_effect, 0.0) + shared + short
    exp = np.array([f"{2016 + b // 12}{b % 12 + 1:02d}" for b in block])
    return {"dates": np.array([(date(2016, 1, 4) + timedelta(days=i)).isoformat() for i in range(n)]), "iv": iv, "rv": rv, "expiry": exp,
            "T_days": np.full(n, 30.0), "pct": pct}


def test_a_planted_premium_and_planted_conditional_effects_are_detected():
    s = vs.study(fake_dataset(vrp=2.5, low_effect=4.0, high_effect=4.0, seed=1))
    t = s["tests"]
    assert t["T1_all"]["t"] > 3 and t["T1_all"]["verdict"] == "過關" and t["T1_all"]["mean"] > 1.5
    assert t["T2_low_iv"]["mean"] > 0 and t["T3_high_iv"]["mean"] > 0           # T2 的符號：RV − IV，低 IV 時 RV 反而較大＝文件說的方向
    assert t["T3_high_iv"]["t"] > 2


def test_the_documents_premise_is_rejected_when_realised_vol_is_always_below_implied():
    s = vs.study(fake_dataset(vrp=3.0, seed=2))
    t = s["tests"]
    assert t["T1_all"]["mean"] > 2 and t["T2_low_iv"]["mean"] < 0 and t["T2_low_iv"]["rv_above_iv_share"] < 0.4     # IV 低時 RV 也沒有高過 IV


def test_false_positives_on_unrelated_series_are_rare():
    hits, ts_ = 0, []
    for seed in range(40):
        s = vs.study(fake_dataset(n=2000, seed=100 + seed))
        for t in s["tests"].values():
            ts_.append(t["t"])
            hits += t["verdict"] == "過關"
    assert hits <= 5, hits                                                    # 120 個檢定，每個約 1.7%~2%（群組數有限的 t 分布）
    assert abs(float(np.mean(ts_))) < 0.4 and 0.7 < float(np.std(ts_)) < 1.6, (np.mean(ts_), np.std(ts_))


def test_thresholds_are_the_pre_registered_ones_and_too_little_data_is_an_error():
    assert vs.N_TESTS == 3 and 2.39 < vs.Z_CLAIM < 2.40 and (vs.MIN_DAYS, vs.PCT_WINDOW, vs.LOW_PCT, vs.HIGH_PCT) == (7.0, 250, 20.0, 80.0)
    with pytest.raises(ValueError, match="太少"):
        vs.study(fake_dataset(n=200))


def test_the_report_says_which_tests_passed_and_describes_the_one_per_expiry_version():
    s = vs.study(fake_dataset(vrp=2.5, seed=3))
    text = vs.format_study(s)
    assert "T1 全部" in text and "T2 IV 低" in text and "T3 IV 高" in text and "每個到期月只取一筆" in text and "偵測力" in text
    assert s["one_per_expiry"]["n"] > 20


# ── 下載 ──────────────────────────────────────────────────────────

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def month_text(y, m):
    d = f"{y}/{m:02d}/03"
    exp = third_wednesday(y, m)
    return csv_text(chain(d, f"{y}{m:02d}", 20000.0, 0.2, 14 / 365, exp=exp.strftime("%Y/%m/%d")))


def test_fetch_saves_small_iv_files_skips_finished_months_and_never_saves_an_error_page(tmp_path, monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(vs.time, "sleep", lambda s: sleeps.append(s))

    def opener(req, timeout=0):
        form = dict(urllib.parse.parse_qsl(req.data.decode()))
        calls.append((form["queryStartDate"], form["queryEndDate"], form["commodity_id"], form["down_type"]))
        y, m = int(form["queryStartDate"][:4]), int(form["queryStartDate"][5:7])
        if (y, m) == (2024, 1):
            return FakeResponse('<script>alert("DateTime error")</script>'.encode("cp950"))
        return FakeResponse(month_text(y, m).encode("cp950"))

    today = date(2024, 3, 15)
    r = vs.fetch(tmp_path, start_year=2024, today=today, pause=0.7, opener=opener)
    assert r == {"downloaded": 2, "skipped": 0, "failed": 1}
    assert {c[2] for c in calls} == {"TXO"} and {c[3] for c in calls} == {"1"}                   # 只抓 TXO、每日行情
    assert sleeps == [0.7, 0.7, 0.7]
    files = sorted(p.name for p in (tmp_path / "vrp").iterdir())
    assert files == ["iv_2024_02.csv", "iv_2024_03.csv"]                                        # 錯誤頁（1 月）沒有被存成資料
    assert all((tmp_path / "vrp" / f).stat().st_size < 3000 for f in files)                     # 只存每天一筆的 IV，不存原始行情
    n = len(calls)
    r2 = vs.fetch(tmp_path, start_year=2024, today=today, pause=0, opener=opener)
    assert [c[0] for c in calls[n:]] == ["2024/01/01", "2024/03/01"]                            # 失敗的月補抓、當月重抓；2 月已結束不重抓
    assert r2["skipped"] == 1


def test_a_month_with_nothing_to_compute_is_a_failure_not_an_empty_file(tmp_path):
    opener = lambda req, timeout=0: FakeResponse(csv_text(["2024/02/01,TXO,202402,20000.0000,買權,-,-,-,-,0,-,0,-,-,1,1,,一般,,,2024/02/21,"]).encode("cp950"))
    r = vs.fetch(tmp_path, start_year=2024, today=date(2024, 2, 28), pause=0, opener=opener)
    assert r["failed"] == 2 and not any((tmp_path / "vrp").glob("*.csv"))


# ── 命令列 ────────────────────────────────────────────────────────

def test_cli_end_to_end_on_files(tmp_path, capsys):
    g, contracts, iv = synthetic(1400, seed=7)
    (tmp_path / "tx").mkdir()
    (tmp_path / "vrp").mkdir()
    head = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"
    rows = [f"{d.replace('-', '/')},TX,{e}  ,{o:.2f},{max(o, c):.2f},{min(o, c):.2f},{c:.2f},0,0.00%,100,{c:.2f},-,-,-,1,1,,一般,," for d, row in contracts.items() for e, (o, c) in row.items()]
    (tmp_path / "tx" / "all.csv").write_bytes(("\r\n".join([head, *rows]) + "\r\n").encode("cp950"))
    lines = ["date,expiry,expiry_date,T_days,F,K,iv"] + [f"{d},{v['expiry']},{v['expiry_date']},{v['T_days']},{v['F']},{v['K']},{v['iv']}" for d, v in iv.items()]
    (tmp_path / "vrp" / "iv_all.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "o.json"
    assert vs.main(["--dir", str(tmp_path), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "選擇權 IV vs 之後的實際波動" in text and "判定（3 個檢定" in text
    assert set(json.loads(out.read_text(encoding="utf-8"))["tests"]) == {"T1_all", "T2_low_iv", "T3_high_iv"}


def test_cli_without_data_says_what_to_fetch(tmp_path, capsys):
    assert vs.main(["--dir", str(tmp_path / "none")]) == 2
    err = capsys.readouterr().err
    assert "vrp_study fetch" in err and "oi_study fetch --only tx" in err


def test_the_exchange_expiry_date_column_wins_over_the_third_wednesday_rule():
    """遇假日順延的情形：行情檔有契約到期日就用它，不要硬算第三個週三。"""
    T = 20 / 365
    r = vs.reduce_month(csv_text(chain("2026/09/01", "202609", 47000.0, 0.2, T, exp="2026/09/17")))[0]
    assert r["expiry_date"] == "2026-09-17" and r["T_days"] == pytest.approx(16 - 0.0 + (13.5 - 13.75) / 24, abs=0.02)


def test_the_trading_days_between_the_day_and_the_expiry_must_be_enough():
    g, contracts, iv = synthetic(300, seed=5)
    d = list(iv)[100]
    iv[d] = {**iv[d], "expiry_date": (date.fromisoformat(d) + timedelta(days=3)).isoformat()}      # 到期日只剩 3 天：不到 4 個報酬 → 這天不算
    ds = vs.build_dataset(iv, contracts)
    assert d not in set(ds["dates"])


# ── 補強（突變驗證發現測試太鬆：週選／其他契約排在前面時、平價履約價與 ATM 的選擇、缺資料、百分位的邊界）──────────

def test_weekly_and_other_contracts_listed_before_the_monthly_one_do_not_leak_in():
    T = 15 / 365
    junk = chain("2026/09/01", "202609W1", 47000.0, 0.60, T, exp="2026/09/16") + chain("2026/09/01", "202609", 47000.0, 0.60, T, exp="2026/09/16", contract="TX1")
    good = chain("2026/09/01", "202609", 47000.0, 0.20, T, exp="2026/09/16")
    r = vs.reduce_month(csv_text(junk + good))                                    # 雜訊排在前面，後面才是真正的月選擇權
    assert len(r) == 1 and r[0]["iv"] == pytest.approx(20.0, abs=0.5)
    r = vs.reduce_month(csv_text(good + junk))                                    # 反過來排也一樣
    assert len(r) == 1 and r[0]["iv"] == pytest.approx(20.0, abs=0.5)


def test_the_atm_strike_is_the_one_nearest_the_parity_forward_not_the_parity_strike():
    """平價履約價（C−P 最小）和遠期價之間可以差一檔：選 ATM 要用遠期價，且遠期價是 K0 + C − P。"""
    T = 20 / 365
    F = 47060.0                                                                   # 遠期價在 47000 與 47100 之間，靠近 47100
    r = vs.reduce_month(csv_text(chain("2026/09/01", "202609", F, 0.25, T, exp="2026/09/21")))[0]
    assert r["K"] == 47100.0 and abs(r["F"] - F) < 30
    F2 = 47040.0
    r2 = vs.reduce_month(csv_text(chain("2026/09/01", "202609", F2, 0.25, T, exp="2026/09/21")))[0]
    assert r2["K"] == 47000.0 and abs(r2["F"] - F2) < 30


def test_a_zero_or_missing_settlement_price_is_not_used_as_a_price():
    T = 20 / 365
    rows = chain("2026/09/01", "202609", 47000.0, 0.25, T, exp="2026/09/21")
    zeros = [line("2026/09/01", "202609", k, side, 0, exp="2026/09/21") for k in (46900.0, 47000.0, 47100.0) for side in ("買權", "賣權")]
    base = [r for r in rows if not any(f"{k:.4f}" in r for k in (46900.0, 47000.0, 47100.0))]
    r = vs.reduce_month(csv_text(base + zeros))[0]
    assert r["K"] not in (46900.0, 47000.0, 47100.0) and r["iv"] == pytest.approx(25.0, abs=1.5)         # 結算價 0 的三檔當作沒有


def test_the_percentile_needs_a_full_year_of_history():
    _, contracts, iv = synthetic(600, seed=2)
    ds = vs.build_dataset(iv, contracts)
    keys = list(iv)
    first_ok = next(i for i, p in enumerate(ds["pct"]) if not np.isnan(p))
    assert keys.index(str(ds["dates"][first_ok])) == vs.PCT_WINDOW                                      # 第 250 筆（0 起算）才有百分位，不是更早


def test_the_low_and_high_groups_include_the_boundary():
    ds = fake_dataset(n=900, seed=4)
    ds["pct"][:] = np.nan
    ds["pct"][300:400] = 20.0
    ds["pct"][500:600] = 80.0
    ds["pct"][400:420] = 20.1
    s = vs.study(ds)
    assert s["tests"]["T2_low_iv"]["n"] == 100 and s["tests"]["T3_high_iv"]["n"] == 100              # 剛好 20 與 80 要算進去；20.1 不算


def test_a_monthly_file_with_the_wrong_header_is_refused_even_if_it_has_rows(tmp_path):
    bad = ("日期,契約,到期月份\r\n" + "\r\n".join(chain("2026/09/01", "202609", 47000.0, 0.2, 15 / 365, exp="2026/09/16"))).encode("cp950")
    r = vs.fetch(tmp_path, start_year=2026, today=date(2026, 1, 20), pause=0, opener=lambda req, timeout=0: FakeResponse(bad))
    assert r["failed"] == 1 and not list((tmp_path / "vrp").glob("*.csv"))


def test_a_weekly_contract_must_not_be_used_when_the_monthly_one_is_too_close_to_expiry():
    """9/10 距月合約 9/16 只剩 6 天（不能用）；這時週選 202609W4（到 9/23）距離夠遠，排序上又在次月之前——不能被拿來當 IV。"""
    rows = (chain("2026/09/10", "202609", 47000.0, 0.20, 6 / 365, exp="2026/09/16") + chain("2026/09/10", "202609W4", 47000.0, 0.60, 13 / 365, exp="2026/09/23")
            + chain("2026/09/10", "202610", 47000.0, 0.30, 41 / 365, exp="2026/10/21"))
    r = vs.reduce_month(csv_text(rows))[0]
    assert r["expiry"] == "202610" and r["iv"] == pytest.approx(30.0, abs=0.7)


def test_a_gap_in_the_data_is_left_out_of_the_realised_volatility_exactly():
    g, contracts, iv = synthetic(420, seed=6)
    cut = {d: row for k, (d, row) in enumerate(contracts.items()) if not 150 <= k < 159}              # 中間缺 9 個交易日（> 10 個日曆天）：比一個到期週期短，才會有視窗跨過它
    series = vs.build_series(cut)
    dates = series["dates"]
    gap_k = int(np.flatnonzero(~series["keep"])[0])                                                    # 跨過缺口的那一期（無效、報酬記 0）
    ds = vs.build_dataset(iv, cut)
    shown = set(ds["dates"])
    # 找一天：它的視窗（到期日之前）跨過缺口，而且缺口前後各有足夠的有效報酬
    d = None
    for x in dates[60:gap_k + 1]:
        i = dates.index(x)
        ks = [k for k in range(i, len(series["r_close"])) if dates[k + 1] <= iv[x]["expiry_date"]]
        if x in shown and gap_k in ks and sum(1 for k in ks if series["keep"][k]) >= 6:
            d = (x, ks)
            break
    assert d is not None, "合成資料裡找不到視窗跨過缺口的日子"
    x, ks = d
    valid = np.array([series["r_close"][k] for k in ks if series["keep"][k]])
    assert len(valid) < len(ks)                                                                         # 確實有無效的期被排除
    k_ds = list(ds["dates"]).index(x)
    assert ds["rv"][k_ds] == pytest.approx(np.sqrt(np.mean(valid ** 2) * 252) * 100)
