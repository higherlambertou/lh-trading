"""期交所選擇權近價鏈歷史（core/option_history.py）：整理規則、遠期價、到期日格式、下載與讀回。

原始檔用合成資料（Black-76 價格，所以買賣權平價反推的遠期價要等於當初給的），欄位與期交所〈選擇權每日交易行情〉一致。
"""
from __future__ import annotations

import gzip
import io
import urllib.error
from datetime import date

import pytest

import core.option_history as oh
from core.iv_monitor import black76

HEADER = "交易日期,契約,到期月份(週別),履約價,買賣權,開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,漲跌價,漲跌%,契約到期日,"


def line(day, code, strike, right, settle, *, session="一般", bid="-", ask="-", close="-", vol="0", oi="0", exp="", contract="TXO"):
    r = {"買權": "買權", "C": "買權", "P": "賣權", "賣權": "賣權"}[right]
    return f"{day},{contract},{code},{strike:.4f},{r},-,-,-,{close},{vol},{settle},{oi},{bid},{ask},-,-,,{session},-,-,{exp},"


def chain_lines(day, code, F, exp="", step=100, span=0.2, sigma=0.2, days=20, **kw):
    """一個到期月的整條鏈（一般時段），價格用 Black-76（r=0），所以平價反推的 F 就是給的 F。"""
    out, T = [], days / 365
    k0 = round(F / step) * step
    for i in range(-int(F * span / step), int(F * span / step) + 1):
        k = k0 + i * step
        for right in ("C", "P"):
            px = black76(F, k, T, sigma, 0.0, right == "C")
            out.append(line(day, code, k, right, round(px, 1), bid=round(px - 1, 1), ask=round(px + 1, 1), exp=exp, **kw))
    return out


def expiry_day_lines(day, code, S, exp, step=100, span=0.2):
    """到期當天（照真實檔案的樣子）：結算價整欄是 0；收盤價是最後成交價（價內＝內含價值、價外是 0.1），履約價錯開 50 讓最終結算價不在履約價上。"""
    out = []
    k0 = round(S / step) * step + step / 2
    for i in range(-int(S * span / step), int(S * span / step) + 1):
        k = k0 + i * step
        out.append(line(day, code, k, "C", "0", close=f"{max(S - k, 0.1):g}", exp=exp))
        out.append(line(day, code, k, "P", "0", close=f"{max(k - S, 0.1):g}", exp=exp))
    return out


def text(*blocks):
    return "\n".join([HEADER] + [x for b in blocks for x in b]) + "\n"


# ── 遠期價與到期日 ────────────────────────────────────────────────

def test_parity_forward_recovers_the_forward_and_needs_enough_strikes():
    F, T = 20000.0, 20 / 365
    pairs = {k: {"C": black76(F, k, T, 0.2, 0.0, True), "P": black76(F, k, T, 0.2, 0.0, False)} for k in range(19000, 21001, 100)}
    assert oh.parity_forward(pairs) == pytest.approx(F, abs=0.01)
    assert oh.parity_forward({k: pairs[k] for k in list(pairs)[:2]}) is None
    one_sided = {k: {"C": 5.0} for k in range(100, 1000, 100)}
    assert oh.parity_forward(one_sided) is None and oh.parity_forward({}) is None


def test_expiry_date_formats():
    assert oh.parse_expiry_date("20260902", "202609W1") == date(2026, 9, 2)
    assert oh.parse_expiry_date("2026/09/02", "202609W1") == date(2026, 9, 2)
    assert oh.parse_expiry_date("", "202610") == date(2026, 10, 21)                     # 舊檔沒有這欄：月選擇權用第三個週三
    assert oh.parse_expiry_date("", "202609W1") is None and oh.parse_expiry_date("garbage", "202609F1") is None


# ── 整理一個月 ────────────────────────────────────────────────────

def test_reduce_keeps_the_two_nearest_monthlies_and_the_band():
    day = "2026/09/15"
    t = text(chain_lines(day, "202609", 45600.0, exp="20260916", days=1), chain_lines(day, "202610", 45700.0, exp="20261021", days=36),
             chain_lines(day, "202611", 45800.0, exp="20261118", days=64))
    rows = oh.reduce_month(t)
    assert {r["expiry"] for r in rows} == {"202609", "202610"} and all(r["kind"] == "M" and r["date"] == "2026-09-15" for r in rows)
    for r in rows:
        assert abs(r["strike"] - r["F"]) <= r["F"] * oh.MONTHLY_BAND + 1e-6
    assert {r["F"] for r in rows if r["expiry"] == "202609"} == {45600.0} and {r["expiry"]: r["expiry_date"] for r in rows} == {"202609": "2026-09-16", "202610": "2026-10-21"}
    far = [r for r in rows if r["expiry"] == "202609"]
    assert min(r["strike"] for r in far) >= 45600 * 0.88 - 100 and max(r["strike"] for r in far) <= 45600 * 1.12 + 100


def test_the_expiry_day_itself_is_kept_because_its_settlement_is_the_final_value():
    t = text(expiry_day_lines("2026/09/16", "202609", 45667.0, "20260916"), chain_lines("2026/09/16", "202610", 45700.0, exp="20261021", days=35))
    rows = oh.reduce_month(t)
    assert {r["expiry"] for r in rows} == {"202609", "202610"}
    last = [r for r in rows if r["expiry"] == "202609"]
    assert {r["F"] for r in last} == {45667.0} and all(r["expiry_date"] == "2026-09-16" for r in last)        # F＝最終結算價
    assert all(r["settle"] == 0.0 for r in last)                                                                 # 到期當天交易所的結算價整欄是 0（不是缺值）
    c = next(r for r in last if r["strike"] == 45650.0 and r["right"] == "C")
    assert c["close"] == 17.0 and max(c["F"] - c["strike"], 0) == 17.0                                          # 到期價值用 max(F − K, 0)


def test_final_settlement_from_the_last_traded_prices():
    closes = {k: {"C": max(45667.0 - k, 0.1), "P": max(k - 45667.0, 0.1)} for k in (45450.0, 45550.0, 45650.0, 45750.0, 45850.0)}
    assert oh.final_settlement(closes) == 45667.0
    noisy = {45450.0: {"C": 220.0, "P": 0.1}, 45550.0: {"C": 118.0, "P": 0.1}, 45750.0: {"C": 0.1, "P": 80.0}, 45850.0: {"C": 0.1, "P": 182.0}}
    assert abs(oh.final_settlement(noisy) - 45668.0) <= 2.0                                                      # 最後成交的時間點不同，差幾點沒關係
    assert oh.final_settlement({45650.0: {"C": 17.0, "P": 0.1}, 45750.0: {"C": 0.1, "P": 83.0}}) is None          # 不到 3 檔
    inconsistent = {45450.0: {"C": 217.0, "P": 0.1}, 45550.0: {"C": 150.0, "P": 0.1}, 45750.0: {"C": 0.1, "P": 90.0}}
    assert oh.final_settlement(inconsistent) is None                                                            # 估計差太多（不是到期日的價格）
    assert oh.final_settlement({}) is None and oh.final_settlement({45650.0: {"C": 3.0, "P": 0.1}}) is None     # 價內太淺（價 < 5）沒有資訊


def test_an_expiry_that_already_passed_is_dropped():
    t = text(chain_lines("2026/09/17", "202609", 45600.0, exp="20260916"), chain_lines("2026/09/17", "202610", 45700.0, exp="20261021", days=34))
    assert {r["expiry"] for r in oh.reduce_month(t)} == {"202610"}


def test_weekly_options_are_kept_only_within_14_days_and_a_narrower_band():
    day = "2026/09/15"
    t = text(chain_lines(day, "202610", 45700.0, exp="20261021"), chain_lines(day, "202609W4", 45620.0, exp="20260923", days=8),
             chain_lines(day, "202610W1", 45700.0, exp="20260930", days=15), chain_lines(day, "202609F3", 45610.0, exp="20260918", days=3))
    rows = oh.reduce_month(t)
    weekly = [r for r in rows if r["kind"] == "W"]
    assert {r["expiry"] for r in weekly} == {"202609W4", "202609F3"}                 # 15 天後到期的那個不留
    assert all(abs(r["strike"] - r["F"]) <= r["F"] * oh.WEEKLY_BAND + 1e-6 for r in weekly)
    assert any(r["kind"] == "M" for r in rows)


def test_weekly_without_an_expiry_date_is_skipped():
    day = "2026/09/15"
    rows = oh.reduce_month(text(chain_lines(day, "202610", 45700.0, exp="20261021"), chain_lines(day, "202609W4", 45620.0, exp="", days=8)))
    assert not [r for r in rows if r["kind"] == "W"] and any(r["kind"] == "M" for r in rows)


def test_both_sessions_are_kept_and_after_hours_has_no_settlement():
    day = "2026/09/15"
    gen = chain_lines(day, "202610", 45700.0, exp="20261021")
    aft = [line(day, "202610", 45700.0, "C", "-", session="盤後", bid="300", ask="310", close="305", vol="12"),
           line(day, "202610", 45700.0, "P", "-", session="盤後", bid="290", ask="297", close="-", vol="5")]
    rows = oh.reduce_month(text(gen, aft))
    a = [r for r in rows if r["session"] == "A"]
    assert len(a) == 2 and all(r["settle"] is None for r in a) and a[0]["bid"] == 300.0 and a[0]["ask"] == 310.0 and a[0]["close"] == 305.0 and a[1]["close"] is None
    assert {r["session"] for r in rows} == {"G", "A"}


def test_after_hours_rows_do_not_change_the_forward():
    day = "2026/09/15"
    gen = chain_lines(day, "202610", 45700.0, exp="20261021")
    aft = [line(day, "202610", 45700.0, "C", "999", session="盤後"), line(day, "202610", 45700.0, "P", "1", session="盤後")]
    assert {r["F"] for r in oh.reduce_month(text(gen, aft))} == {45700.0}


def test_garbage_rows_are_ignored():
    day = "2026/09/15"
    gen = chain_lines(day, "202610", 45700.0, exp="20261021")
    bad = [line(day, "202610", 45700.0, "C", "5", contract="MTX"), line(day, "TXO", 45700.0, "C", "5"),
           line(day, "202610", 0.0, "C", "5"), line(day, "202610", 45700.0, "X" if False else "C", "-")]
    rows = oh.reduce_month(text(gen, bad))
    assert all(r["expiry"] == "202610" for r in rows) and len([r for r in rows if r["strike"] == 45700.0 and r["right"] == "C" and r["session"] == "G"]) >= 1


def test_a_month_without_usable_settlements_gives_nothing():
    day = "2026/09/15"
    only_bid = [line(day, "202610", k, r, "-", bid="5", ask="6") for k in (45600, 45700, 45800, 45900) for r in "CP"]
    assert oh.reduce_month(text(only_bid)) == []
    assert oh.reduce_month(HEADER + "\n") == []


def test_rows_are_sorted_and_numbers_are_numbers():
    day1, day2 = "2026/09/16", "2026/09/15"
    rows = oh.reduce_month(text(chain_lines(day1, "202610", 45700.0, exp="20261021"), chain_lines(day2, "202610", 45650.0, exp="20261021")))
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    r = rows[0]
    assert isinstance(r["strike"], float) and isinstance(r["F"], float) and r["bid"] is not None


# ── 寫入、讀回 ────────────────────────────────────────────────────

def test_write_and_read_back_roundtrip(tmp_path):
    t = text(chain_lines("2026/09/15", "202610", 45700.0, exp="20261021"))
    rows = oh.reduce_month(t)
    (tmp_path / "opt").mkdir()
    n = oh.write_rows(tmp_path / "opt" / "chain_2026_09.csv.gz", rows)
    assert n == len(rows)
    back = list(oh.iter_rows(tmp_path))
    assert len(back) == n
    for a, b in zip(rows, back):
        assert (a["date"], a["expiry"], a["right"], a["session"]) == (b["date"], b["expiry"], b["right"], b["session"])
        assert b["strike"] == a["strike"] and b["settle"] == pytest.approx(a["settle"], abs=1e-3) and b["F"] == pytest.approx(a["F"], abs=1e-2)
    assert [r["date"] for r in oh.iter_rows(tmp_path, start="2026-09-16")] == []
    assert len(list(oh.iter_rows(tmp_path, start="2026-09-15", end="2026-09-15"))) == n
    assert not list((tmp_path / "opt").glob("*.part"))                      # 沒有留下半個檔


def test_missing_values_stay_none_after_the_roundtrip(tmp_path):
    rows = [{"date": "2026-09-15", "kind": "M", "expiry": "202610", "expiry_date": "2026-10-21", "session": "A", "right": "C", "strike": 45700.0,
             "F": 45700.0, "settle": None, "close": None, "bid": 300.0, "ask": None, "volume": 0.0, "oi": None}]
    (tmp_path / "opt").mkdir()
    oh.write_rows(tmp_path / "opt" / "chain_2026_09.csv.gz", rows)
    r = next(oh.iter_rows(tmp_path))
    assert r["settle"] is None and r["close"] is None and r["ask"] is None and r["oi"] is None and r["bid"] == 300.0 and r["volume"] == 0.0


# ── 下載 ──────────────────────────────────────────────────────────

class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def fake_opener(bodies, calls):
    def opener(req, timeout=0):
        calls.append(req.data.decode())
        b = bodies.pop(0)
        if isinstance(b, Exception):
            raise b
        return Resp(b.encode("cp950", errors="replace"))
    return opener


def test_months_list():
    ms = oh.months(2026, date(2026, 10, 12))
    assert [m[0] for m in ms][0] == "chain_2026_01.csv.gz" and ms[-1][0] == "chain_2026_10.csv.gz" and len(ms) == 10
    assert ms[0][1:] == (date(2026, 1, 1), date(2026, 1, 31)) and ms[-1][1:] == (date(2026, 10, 1), date(2026, 10, 12))
    assert oh.months(2025, date(2026, 1, 15))[11][2] == date(2025, 12, 31)


def test_fetch_saves_each_month_skips_finished_ones_and_refetches_the_current_month(tmp_path):
    good = text(chain_lines("2026/01/05", "202601", 45700.0, exp="20260121", days=16))
    calls = []
    got = oh.fetch(tmp_path, 2026, today=date(2026, 3, 20), pause=0, opener=fake_opener([good, good, good], calls), log=lambda s: None)
    assert got["downloaded"] == 3 and got["failed"] == 0 and got["skipped"] == 0 and len(calls) == 3 and got["rows"] > 0
    assert sorted(p.name for p in (tmp_path / "opt").glob("*.gz")) == ["chain_2026_01.csv.gz", "chain_2026_02.csv.gz", "chain_2026_03.csv.gz"]
    calls2 = []
    got2 = oh.fetch(tmp_path, 2026, today=date(2026, 3, 20), pause=0, opener=fake_opener([good], calls2), log=lambda s: None)
    assert got2["skipped"] == 2 and got2["downloaded"] == 1 and len(calls2) == 1 and "queryStartDate=2026%2F03%2F01" in calls2[0]    # 只重抓還沒結束的當月


def test_fetch_counts_failures_and_never_saves_an_error_page(tmp_path, capsys):
    good = text(chain_lines("2026/01/05", "202601", 45700.0, exp="20260121", days=16))
    calls = []
    bodies = ["<html>錯誤</html>", urllib.error.URLError("boom"), good]
    got = oh.fetch(tmp_path, 2026, today=date(2026, 3, 20), pause=0, opener=fake_opener(bodies, calls), log=lambda s: None)
    assert got["failed"] == 2 and got["downloaded"] == 1
    assert sorted(p.name for p in (tmp_path / "opt").glob("*.gz")) == ["chain_2026_03.csv.gz"]
    err = capsys.readouterr().err
    assert "chain_2026_01" in err and "chain_2026_02" in err


def test_fetch_posts_the_documented_query(tmp_path):
    calls = []
    oh.fetch(tmp_path, 2026, today=date(2026, 1, 20), pause=0, opener=fake_opener([text(chain_lines("2026/01/05", "202601", 45700.0, exp="20260121", days=16))], calls),
             log=lambda s: None)
    assert "commodity_id=TXO" in calls[0] and "queryStartDate=2026%2F01%2F01" in calls[0] and "queryEndDate=2026%2F01%2F20" in calls[0] and "down_type=1" in calls[0]


def test_status(tmp_path):
    assert oh.status(tmp_path) == {"files": 0, "first": None, "last": None, "mb": 0.0}
    (tmp_path / "opt").mkdir()
    with gzip.open(tmp_path / "opt" / "chain_2026_01.csv.gz", "wt") as f:
        f.write("x")
    s = oh.status(tmp_path)
    assert s["files"] == 1 and s["first"] == s["last"] == "chain_2026_01.csv.gz"
