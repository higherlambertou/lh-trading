"""選擇權買賣價差的時段紀錄（core/option_spread_log.py）：取樣時段、對齊時鐘、彙總成各「離平值多遠」的桶、儲存、取樣與背景迴圈的容錯、報表。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

import core.option_spread_log as sl
import core.options_calc as oc


# ── 什麼時候取樣 ──────────────────────────────────────────────────

@pytest.mark.parametrize("when,want", [
    (datetime(2026, 10, 12, 8, 44, 59), None),                 # 週一，開盤前
    (datetime(2026, 10, 12, 8, 45, 30), None),                 # 剛開盤 60 秒內不取樣
    (datetime(2026, 10, 12, 8, 46, 0), "day"),
    (datetime(2026, 10, 12, 13, 45, 59), "day"),
    (datetime(2026, 10, 12, 13, 46, 0), None),
    (datetime(2026, 10, 12, 14, 30), None),
    (datetime(2026, 10, 12, 15, 0, 30), None),                 # 夜盤開盤 60 秒內
    (datetime(2026, 10, 12, 15, 1, 0), "night"),
    (datetime(2026, 10, 12, 23, 59), "night"),
    (datetime(2026, 10, 13, 0, 0), "night"),                   # 週二凌晨＝週一夜盤的延續
    (datetime(2026, 10, 13, 4, 59, 59), "night"),
    (datetime(2026, 10, 13, 5, 0, 0), None),
    (datetime(2026, 10, 12, 3, 0), None),                      # 週一凌晨：週日沒有夜盤
    (datetime(2026, 10, 17, 3, 0), "night"),                   # 週六凌晨＝週五夜盤
    (datetime(2026, 10, 17, 10, 0), None),                     # 週六白天
    (datetime(2026, 10, 17, 16, 0), None),                     # 週六晚上沒有夜盤
    (datetime(2026, 10, 18, 3, 0), None),                      # 週日凌晨：週六沒有夜盤
    (datetime(2026, 10, 18, 10, 0), None),
    (datetime(2026, 10, 16, 21, 0), "night"),                  # 週五晚上
])
def test_sampling_session(when, want):
    assert sl.sampling_session(when) == want


def test_next_boundary_aligns_to_the_clock():
    f = sl.next_boundary
    assert f(datetime(2026, 10, 12, 10, 3, 30), 10) == datetime(2026, 10, 12, 10, 10)
    assert f(datetime(2026, 10, 12, 10, 0, 0), 10) == datetime(2026, 10, 12, 10, 10)          # 剛好在邊界：等下一個
    assert f(datetime(2026, 10, 12, 10, 59, 59), 10) == datetime(2026, 10, 12, 11, 0)
    assert f(datetime(2026, 10, 12, 23, 55), 10) == datetime(2026, 10, 13, 0, 0)
    assert f(datetime(2026, 10, 12, 10, 7), 5) == datetime(2026, 10, 12, 10, 10) and f(datetime(2026, 10, 12, 10, 7), 1) == datetime(2026, 10, 12, 10, 8)


# ── 彙總 ──────────────────────────────────────────────────────────

def chain_with(spreads: dict[tuple[float, str], float], F=49300.0, mid=100.0, days=9.0):
    """每個 (履約價, 'C'/'P') 指定買賣價差（點），中價固定 mid；其餘沒有報價。"""
    quotes = []
    for (k, right), sp in spreads.items():
        quotes.append({"strike": k, "right": right, "bid": mid - sp / 2, "ask": mid + sp / 2, "close": mid})
    return oc.build_chain(quotes, days / 365.0, F=F)


def test_summary_buckets_by_distance_from_the_forward():
    F = 49300.0
    ch = chain_with({(49300.0, "C"): 4.0, (49300.0, "P"): 6.0,                        # ATM（距離 0）
                     (49400.0, "C"): 10.0, (49100.0, "P"): 14.0,                      # 0.2%~0.4%：還是 ATM 桶
                     (49800.0, "C"): 20.0, (48800.0, "P"): 30.0,                      # 1.0%、1.0%：NEAR
                     (51000.0, "C"): 40.0,                                            # 3.4%：MID
                     (52500.0, "C"): 60.0,                                            # 6.5%：FAR
                     (60000.0, "C"): 80.0},                                           # 21%：不在任何桶
                    F=F)
    s = {x["bucket"]: x for x in sl.summarize_chain(ch)}
    assert (s["ATM"]["n_two"], s["ATM"]["n_total"]) == (4, 4) and s["ATM"]["med_pts"] == 8.0            # 4、6、10、14 的中位數
    assert (s["NEAR"]["n_two"], s["NEAR"]["med_pts"]) == (2, 25.0) and s["MID"]["med_pts"] == 40.0 and s["FAR"]["med_pts"] == 60.0
    assert s["ATM"]["med_pct"] == pytest.approx(0.08, abs=1e-4)                                         # 價差 8 點 ÷ 中價 100
    assert s["STRADDLE"]["med_pts"] == 10.0 and s["STRADDLE"]["med_pct"] == pytest.approx(10 / 200, abs=1e-4)      # 4 + 6 ÷ (100 + 100)
    assert "ATM" in s and all(b in s for b in ("ATM", "NEAR", "MID", "FAR"))


def test_summary_ignores_one_sided_quotes_but_counts_them_as_listed():
    ch = chain_with({(49300.0, "C"): 5.0, (49300.0, "P"): 5.0})
    ch["rows"].append({"strike": 49350.0, "call": {"bid": 0.0, "ask": 90.0, "last": 0.0, "mid": 0.0, "iv": None, "delta": None, "spread": None, "spread_pct": None, "volume": 0.0}, "put": None})
    s = {x["bucket"]: x for x in sl.summarize_chain(ch)}
    assert s["ATM"]["n_two"] == 2 and s["ATM"]["n_total"] == 3                                          # 只有賣價的那檔列出來了、但不算進價差


def test_summary_without_any_two_sided_quote_has_empty_numbers_and_no_straddle():
    ch = chain_with({})  if False else None
    quotes = [{"strike": 49300.0, "right": "C", "bid": 0.0, "ask": 50.0, "close": 40.0}, {"strike": 49300.0, "right": "P", "bid": 0.0, "ask": 50.0, "close": 40.0}]
    ch = oc.build_chain(quotes, 9 / 365.0, F=49300.0)
    s = {x["bucket"]: x for x in sl.summarize_chain(ch)}
    assert s["ATM"]["n_two"] == 0 and s["ATM"]["med_pts"] is None and s["ATM"]["med_pct"] is None and "STRADDLE" not in s


def test_straddle_uses_the_strike_nearest_the_forward_that_has_both_sides():
    ch = chain_with({(49300.0, "C"): 4.0, (49400.0, "C"): 10.0, (49400.0, "P"): 12.0, (49500.0, "C"): 30.0, (49500.0, "P"): 30.0}, F=49320.0)
    s = {x["bucket"]: x for x in sl.summarize_chain(ch)}
    assert s["STRADDLE"]["med_pts"] == 22.0                                          # 49300 只有買權；最近且兩邊都有的是 49400


def test_p90_is_the_90th_percentile_of_the_percentage_spreads():
    sp = {(49300.0 + 10 * i, "C"): float(i + 1) for i in range(10)}                   # 價差 1~10 點（中價 100 → 1%~10%），全在 ATM 桶（距離 < 0.5%）
    ch = chain_with(sp)
    atm = next(x for x in sl.summarize_chain(ch) if x["bucket"] == "ATM")
    assert atm["n_two"] == 10 and atm["med_pct"] == pytest.approx(0.055, abs=1e-4) and atm["p90_pct"] == pytest.approx(0.10, abs=1e-4)


def test_snapshot_rows_carry_the_context():
    ch = chain_with({(49300.0, "C"): 4.0, (49300.0, "P"): 6.0})
    rows = sl.snapshot_rows(ch, "202610", "night", 1_760_000_000)
    assert {r["expiry"] for r in rows} == {"202610"} and {r["session"] for r in rows} == {"night"} and {r["ts"] for r in rows} == {1_760_000_000}
    assert rows[0]["dte"] == pytest.approx(9.0, abs=0.01) and rows[0]["F"] == 49300.0 and set(rows[0]) == set(sl.COLUMNS)


# ── 儲存 ──────────────────────────────────────────────────────────

def test_store_roundtrip_and_replace(tmp_path):
    st = sl.SpreadStore(tmp_path / "s.db")
    assert st.count() == 0 and st.rows() == [] and st.insert([]) == 0
    ch = chain_with({(49300.0, "C"): 4.0, (49300.0, "P"): 6.0})
    rows = sl.snapshot_rows(ch, "202610", "day", 1000)
    assert st.insert(rows) == len(rows) == st.count()
    st.insert(rows)                                                                  # 同一個 (ts, 到期月, 桶) 再寫一次是覆蓋、不是重複
    assert st.count() == len(rows)
    st.insert(sl.snapshot_rows(ch, "202610", "day", 2000))
    assert len(st.rows(since_ts=1500)) == len(rows) and {r["ts"] for r in st.rows()} == {1000, 2000}
    assert (tmp_path / "s.db").exists()


# ── 取樣 ──────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 12, 10, 10, 0)


class FakeBroker:
    def __init__(self, months=("202610", "202611", "202612"), fail=(), expiries=None, spreads=None):
        self.months, self.fail, self.calls = list(months), set(fail), []
        self.expiries = expiries or {"202610": "2026/10/21", "202611": "2026/11/18", "202612": "2026/12/16"}
        self.spreads = spreads or {}

    async def option_expiries(self, category="TXO"):
        return self.months

    async def option_chain(self, month, category="TXO", center=None, max_n=400):
        self.calls.append((month, center))
        if month in self.fail:
            raise ValueError("boom")
        sp = self.spreads.get(month, 5.0)
        q = []
        for k in range(48500, 50101, 100):
            for right in ("C", "P"):
                mid = max(abs(49300 - k), 0) + 150.0
                q.append({"strike": float(k), "right": right, "bid": mid - sp / 2, "ask": mid + sp / 2, "close": mid})
        return {"delivery_month": month, "delivery_date": self.expiries[month], "quotes": q}


def run(coro):
    return asyncio.run(coro)


def test_sample_once_stores_the_two_nearest_months(tmp_path):
    st, b = sl.SpreadStore(tmp_path / "s.db"), FakeBroker()
    n = run(sl.sample_once(b, 49300.0, st, NOW, "day"))
    assert n == st.count() > 0 and {r["expiry"] for r in st.rows()} == {"202610", "202611"}
    assert [c[0] for c in b.calls] == ["202610", "202611"] and all(c[1] == 49300.0 for c in b.calls)           # 中心用 TXF 現價
    r = next(x for x in st.rows() if x["expiry"] == "202610" and x["bucket"] == "STRADDLE")
    assert r["ts"] == int(NOW.timestamp()) and r["session"] == "day" and r["dte"] == pytest.approx(9.139, abs=0.01)


def test_one_failing_month_does_not_stop_the_other_and_still_uses_a_slot(tmp_path):
    st, b = sl.SpreadStore(tmp_path / "s.db"), FakeBroker(fail={"202610"})
    run(sl.sample_once(b, 49300.0, st, NOW, "day"))
    assert {r["expiry"] for r in st.rows()} == {"202611"} and [c[0] for c in b.calls] == ["202610", "202611"]     # 近月壞了：次月照存，不會一路往遠月試


def test_an_expired_month_that_is_still_listed_is_skipped_without_using_a_slot(tmp_path):
    st = sl.SpreadStore(tmp_path / "s.db")
    b = FakeBroker(expiries={"202610": "2026/10/12", "202611": "2026/11/18", "202612": "2026/12/16"})            # 近月今天 13:30 到期
    late = datetime(2026, 10, 12, 13, 40)
    run(sl.sample_once(b, 49300.0, st, late, "day"))
    assert {r["expiry"] for r in st.rows()} == {"202611", "202612"}


def test_sample_once_ignores_weird_month_codes(tmp_path):
    st, b = sl.SpreadStore(tmp_path / "s.db"), FakeBroker(months=("202610W2", "202610", "abc", "202611"), expiries={"202610": "2026/10/21", "202611": "2026/11/18"})
    run(sl.sample_once(b, 49300.0, st, NOW, "night"))
    assert {r["expiry"] for r in st.rows()} == {"202610", "202611"} and {r["session"] for r in st.rows()} == {"night"}


# ── 背景迴圈 ──────────────────────────────────────────────────────

class Hub:
    def last_price_by_prefix(self, p):
        return 49300.0 if p == "TXF" else None


def run_loop(times, broker, store, minutes=10):
    """依序餵「現在」給迴圈（每次睡醒就換下一個時間），跑完就取消。回傳睡了幾次與每次睡多久。"""
    seq = iter(times)
    sleeps = []
    clock = {"now": None}

    def now_fn():
        return clock["now"]

    async def sleep(sec):
        sleeps.append(sec)
        try:
            clock["now"] = next(seq)
        except StopIteration:
            raise asyncio.CancelledError

    async def go():
        clock["now"] = datetime(2026, 10, 12, 10, 3, 30)
        try:
            await sl.spread_log_loop(broker, Hub(), minutes=minutes, store=store, now_fn=now_fn, sleep=sleep)
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    return sleeps


def test_loop_samples_inside_sessions_and_skips_outside(tmp_path):
    st, b = sl.SpreadStore(tmp_path / "s.db"), FakeBroker()
    sleeps = run_loop([datetime(2026, 10, 12, 10, 10, 1),          # 日盤
                       datetime(2026, 10, 12, 14, 0, 1),           # 休市
                       datetime(2026, 10, 17, 11, 0, 1),           # 週六
                       datetime(2026, 10, 12, 21, 0, 1)], b, st)   # 夜盤
    assert {r["session"] for r in st.rows()} == {"day", "night"} and sorted({r["ts"] for r in st.rows()}) == [int(datetime(2026, 10, 12, 10, 10, 1).timestamp()), int(datetime(2026, 10, 12, 21, 0, 1).timestamp())]
    assert sleeps[0] == pytest.approx(6 * 60 + 30 + 1, abs=0.01)                                          # 10:03:30 → 睡到 10:10:00 再多 1 秒
    assert len(b.calls) == 4                                                                              # 兩次取樣 × 兩個月


def test_loop_survives_errors_and_keeps_going(tmp_path):
    st = sl.SpreadStore(tmp_path / "s.db")

    class Flaky(FakeBroker):
        n = 0

        async def option_expiries(self, category="TXO"):
            Flaky.n += 1
            if Flaky.n == 1:
                raise RuntimeError("Worker 尚未連線")
            if Flaky.n == 2:
                raise ValueError("unexpected")
            return self.months

    b = Flaky()
    run_loop([datetime(2026, 10, 12, 10, 10, 1), datetime(2026, 10, 12, 10, 20, 1), datetime(2026, 10, 12, 10, 30, 1)], b, st)
    assert Flaky.n == 3 and {r["ts"] for r in st.rows()} == {int(datetime(2026, 10, 12, 10, 30, 1).timestamp())}          # 前兩次失敗、第三次才存到


def test_loop_stops_cleanly_when_cancelled(tmp_path):
    st, b = sl.SpreadStore(tmp_path / "s.db"), FakeBroker()
    assert run_loop([], b, st) and st.count() == 0                                    # 第一次睡就被取消：沒有取樣、不丟例外


# ── 報表 ──────────────────────────────────────────────────────────

def test_report_summarises_by_session_and_hour(tmp_path):
    st = sl.SpreadStore(tmp_path / "s.db")
    for ts_, sess, sp in ((datetime(2026, 10, 12, 10, 10), "day", 5.0), (datetime(2026, 10, 12, 10, 20), "day", 7.0), (datetime(2026, 10, 12, 21, 0), "night", 20.0)):
        ch = chain_with({(49300.0, "C"): sp / 2, (49300.0, "P"): sp / 2, (49400.0, "C"): sp, (49200.0, "P"): sp})
        st.insert(sl.snapshot_rows(ch, "202610", sess, int(ts_.timestamp())))
    text = sl.report(st.rows())
    assert "取樣 3 次、1 天" in text and "日盤" in text and "夜盤" in text and "ATM 跨式" in text and "依小時" in text
    assert "日盤 10 點" in text and "夜盤 21 點" in text
    assert sl.report([]).startswith("還沒有資料")


def test_report_orders_the_night_hours_from_the_evening_through_the_morning(tmp_path):
    st = sl.SpreadStore(tmp_path / "s.db")
    ch = chain_with({(49300.0, "C"): 2.0, (49300.0, "P"): 2.0})
    for h, d in ((2, 13), (22, 12), (16, 12)):
        st.insert(sl.snapshot_rows(ch, "202610", "night", int(datetime(2026, 10, d, h, 0).timestamp())))
    text = sl.report(st.rows())
    assert text.index("夜盤 16 點") < text.index("夜盤 22 點") < text.index("夜盤 02 點")
