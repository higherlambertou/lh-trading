"""進場市場狀態紀錄（需求文件/進場市場狀態紀錄_需求文件.md）。逐條對應驗收標準：

 1 每筆進場成交有且僅有一筆快照　　　2 缺值為 null 且 missing_reason 有值，不以 0 或預設值代替
 3 出場回填，損益可手算　　　　　　　4 啟用前後送出的訂單完全相同　　　5 沒有策略或風控讀取這些欄位
 6 寫入失敗不擋下單、有警示與記錄　　7 重啟、重複事件冪等，不重複也不遺失　　8 CSV 欄位完整、時區一致
 9 涵蓋：缺值、IV 歷史不足、部分成交、停損未成交後接力出場、重複事件"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

import core.entry_log as el
import core.trade_log as tl
from core.entry_log import EntrySync, capture, fetch_entries, strategy_type, to_csv
from core.trade_log import TradeLog

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_760_000_000.0                          # 固定的時間基準（避免跟 time.time() 扯上關係）

FULL_TAG = {"market_state": "TREND_NORMAL", "hurst_state": "TREND", "iv_state": "NORMAL", "direction": 1, "as_of": "2026-10-12",
            "snapshot": {"hurst_value": 0.36, "hurst_z": -1.1, "hurst_window": 60, "hurst_bar_period": "日K", "hurst_noise": 0.132,
                         "hurst_note": "", "iv_value": 18.5, "iv_percentile": 42.0, "iv_history_n": 80, "iv_min_history": 60, "iv_note": "",
                         "state_timestamp": T0 - 3600, "state_date": "2026-10-12", "market_state": "TREND_NORMAL"}}


@pytest.fixture(autouse=True)
def _fresh_alerts():
    el.alerts.count, el.alerts.last = 0, None
    yield


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(tl._SCHEMA)
    TradeLog._migrate(c)
    c.commit()
    yield c
    c.close()


def add_order(c, trade_id, *, mode="sim", strategy="scalp", reason="entry", contract="TMF", action="Buy", qty=1, ts=T0, tag=FULL_TAG,
              ctx=None, signal=48980.0, state=True):
    sj = json.dumps(capture(tag, ctx or {}), ensure_ascii=False) if state else None
    c.execute("INSERT INTO orders(ts, mode, trade_id, strategy, reason, contract, action, qty, signal_price, state_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (ts, mode, trade_id, strategy, reason, contract, action, qty, signal, sj))
    c.commit()


def add_deal(c, trade_id, price, qty, ts=T0 + 1, *, mode="sim", deal_ts=None, deal_seq=None):
    c.execute("INSERT INTO events(ts, mode, kind, trade_id, price, qty, deal_ts, deal_seq) VALUES(?,?,'deal',?,?,?,?,?)",
              (ts, mode, trade_id, price, qty, deal_ts, deal_seq))
    c.commit()


def rows(c, table):
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
    finally:
        c.row_factory = None


def new_sync(c):
    s = EntrySync(c)
    s.init()
    return s


# ── 1. 每筆進場成交有且僅有一筆快照 ─────────────────────────────────

def test_one_snapshot_per_entry_fill_with_the_state_at_order_time(conn):
    s = new_sync(conn)
    add_order(conn, "E1", ctx={"stop_price": 48880.0, "strategy_params": {"tp_pts": 20, "sl_pts": 100}}, qty=2)
    add_deal(conn, "E1", 48981.0, 2)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    assert (snap["trade_id"], snap["mode"], snap["contract"], snap["strategy_name"], snap["strategy_type"], snap["direction"]) == \
           ("E1", "sim", "TMF", "scalp", "other", "long")
    assert (snap["entry_price"], snap["lots"], snap["stop_price"], snap["signal_price"]) == (48981.0, 2, 48880.0, 48980.0)
    assert (snap["hurst_value"], snap["hurst_z"], snap["hurst_window"], snap["hurst_bar_period"], snap["hurst_noise"]) == (0.36, -1.1, 60, "日K", 0.132)
    assert (snap["iv_value"], snap["iv_percentile"], snap["state_date"], snap["market_state"]) == (18.5, 42.0, "2026-10-12", "TREND_NORMAL")
    assert snap["state_timestamp"] == T0 - 3600 and snap["missing_reason"] is None
    assert json.loads(snap["strategy_params"]) == {"tp_pts": 20, "sl_pts": 100}
    assert snap["entry_time"].endswith("+08:00") and snap["order_ts"] == T0 and snap["entry_ts"] == T0 + 1


def test_the_state_is_the_one_at_order_time_not_recomputed_at_fill_time(conn):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 500)                       # 幾分鐘後才成交
    s.step()
    assert rows(conn, "entry_snapshots")[0]["hurst_value"] == 0.36          # 取委託送出當下已有的值，不重新計算


def test_a_duplicated_fill_event_does_not_make_a_second_snapshot_or_double_the_position(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=1)
    add_deal(conn, "E1", 100.0, 1, deal_ts=T0 + 1.5, deal_seq="X1")
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2, deal_ts=T0 + 1.5, deal_seq="X1")          # 同一筆成交重複送達
    s.step()
    assert len(rows(conn, "entry_snapshots")) == 1
    assert sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 1                          # 部位簿只有 1 口
    add_order(conn, "X1", strategy="manual", reason="tp", action="Sell")
    add_deal(conn, "X1", 105.0, 1)
    s.step()
    assert len(rows(conn, "exit_fills")) == 1 and rows(conn, "exit_fills")[0]["pnl_points"] == 5.0     # 沒有因為重複而多平一口


def test_two_partial_fills_in_the_same_second_are_not_merged_when_the_broker_only_gives_whole_seconds(conn):
    """實測的 FuturesDeal 沒有交易所序號（exchange_seq 是空字串）；成交時間若只到秒，同一秒內相同價量的兩筆部分成交不能被當成重複。"""
    s = new_sync(conn)
    add_order(conn, "E1", qty=2)
    add_deal(conn, "E1", 100.0, 1, deal_ts=1_760_000_001.0)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2, deal_ts=1_760_000_001.0)
    s.step()
    assert len(rows(conn, "entry_snapshots")) == 1 and sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 2
    assert fetch_entries(_dump(conn))[0]["filled_lots"] == 2 and el.alerts.count == 0


def test_a_duplicate_without_any_identifier_is_caught_by_the_order_quantity(conn):
    """累計成交不可能超過委託口數：沒有識別資訊時，多出來的那筆一定是重複送達，不計入部位，並通報。"""
    s = new_sync(conn)
    add_order(conn, "E1", qty=1)
    add_deal(conn, "E1", 100.0, 1)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2)                              # 同一筆又送了一次
    s.step()
    assert sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 1 and len(rows(conn, "entry_snapshots")) == 1
    assert el.alerts.count == 1 and el.alerts.last["where"] == "overfill"
    assert [e["where_"] for e in rows(conn, "entry_log_errors")] == ["overfill"]
    add_order(conn, "X1", reason="tp", action="Sell", ts=T0 + 9)
    add_deal(conn, "X1", 104.0, 1, ts=T0 + 10)
    s.step()
    assert not any(s.book.values()) and fetch_entries(_dump(conn))[0]["filled_lots"] == 1          # 一進一出後空手，不會留下多出來的 1 口


def test_an_overfill_is_clamped_to_the_remaining_quantity(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=3)
    add_deal(conn, "E1", 100.0, 2)
    add_deal(conn, "E1", 101.0, 2, ts=T0 + 2)                              # 只剩 1 口可成交，卻回報 2 口
    s.step()
    assert sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 3
    assert el.alerts.count == 1 and "多出的 1 口" in el.alerts.last["error"]


def test_an_old_problem_is_reported_once_not_again_at_every_restart(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=1)
    add_deal(conn, "E1", 100.0, 1)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2)
    s.step()
    assert el.alerts.count == 1
    s2 = new_sync(conn)                                                    # 重啟：重放時又遇到同一筆
    assert el.alerts.count == 1 and len(rows(conn, "entry_log_errors")) == 1
    assert sum(lot["qty"] for lot in s2.book[("sim", "TMF")]) == 1         # 部位簿一樣正確


def test_the_exported_filled_lots_never_exceed_the_order(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=1)
    add_deal(conn, "E1", 100.0, 1)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2)
    p = _dump(conn)                                                        # 還沒處理，直接從成交明細匯出
    assert fetch_entries(p) == []                                          # 沒有快照就沒有列
    s.step()
    (e,) = fetch_entries(_dump(conn))
    assert (e["order_lots"], e["filled_lots"]) == (1, 1)


def test_a_deal_without_a_trade_id_is_skipped_quietly(conn):
    s = new_sync(conn)
    add_deal(conn, "", 100.0, 1)                                          # 解析壞掉那段時期留下的殘渣
    add_order(conn, "E1", ts=T0 + 5)
    add_deal(conn, "E1", 101.0, 1, ts=T0 + 6)
    assert s.step() == 1 and el.alerts.count == 0 and [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["E1"]


@pytest.mark.parametrize("ids", [
    {"deal_ts": T0 + 1.5, "deal_seq": "S1"},             # 成交時間（帶小數）＋交易所序號
    {"deal_ts": None, "deal_seq": "S1"},                 # 只有交易所序號
    {"deal_ts": T0 + 1.5, "deal_seq": None},             # 只有帶小數的成交時間
])
def test_a_resent_fill_is_merged_by_its_ids_even_when_the_order_still_has_room(conn, ids):
    """委託 3 口、已成交 A、B 兩筆，A 又送了一次：總數 3 口沒超過委託，只能靠識別資訊認出重複。"""
    s = new_sync(conn)
    add_order(conn, "E1", qty=3)
    other = {"deal_ts": ids["deal_ts"] + 0.5 if ids["deal_ts"] else None, "deal_seq": "S2" if ids["deal_seq"] else None}
    add_deal(conn, "E1", 100.0, 1, **ids)                                  # A
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2, **other)                     # B（價量相同、但是另一筆）
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 3, **ids)                       # A 又送了一次
    s.step()
    assert sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 2 and el.alerts.count == 0
    assert fetch_entries(_dump(conn))[0]["filled_lots"] == 2


def test_two_identical_partial_fills_without_identifiers_are_kept_as_two(conn):
    """沒有券商給的時間／序號就無從辨識：寧可兩筆都記，也不要把兩筆相同價量的部分成交併成一筆。"""
    s = new_sync(conn)
    add_order(conn, "E1", qty=2)
    add_deal(conn, "E1", 100.0, 1)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 2)
    s.step()
    assert len(rows(conn, "entry_snapshots")) == 1 and sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 2


# ── 2. 缺值：null + missing_reason，不補 0 ───────────────────────────

def test_no_market_state_yet_means_nulls_and_a_reason(conn):
    s = new_sync(conn)
    add_order(conn, "E1", tag={})                                       # 服務剛啟動、今日狀態還沒算過
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    for col in ("hurst_value", "hurst_z", "hurst_window", "hurst_bar_period", "hurst_noise", "iv_value", "iv_percentile",
                "state_timestamp", "state_date", "market_state"):
        assert snap[col] is None, col                                    # 是 NULL，不是 0、不是空字串、不是上一次的值
    assert "尚未產生" in snap["missing_reason"]


def test_iv_history_too_short_keeps_iv_value_but_nulls_the_percentile(conn):
    s = new_sync(conn)
    tag = {"snapshot": {**FULL_TAG["snapshot"], "iv_percentile": None, "iv_history_n": 3, "iv_min_history": 60}}
    add_order(conn, "E1", tag=tag)
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    assert snap["iv_percentile"] is None and snap["iv_value"] == 18.5 and snap["hurst_value"] == 0.36
    assert "IV 百分位：歷史不足（3/60 天）" in snap["missing_reason"]


def test_hurst_missing_carries_the_reason_from_the_panel(conn):
    s = new_sync(conn)
    tag = {"snapshot": {**FULL_TAG["snapshot"], "hurst_value": None, "hurst_z": None, "hurst_noise": None, "hurst_note": "資料不足：12 根日K"}}
    add_order(conn, "E1", tag=tag)
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    assert snap["hurst_value"] is None and snap["iv_value"] == 18.5 and "Hurst：資料不足：12 根日K" in snap["missing_reason"]


def test_an_order_recorded_without_any_state_json_still_gets_a_snapshot_with_a_reason(conn):
    s = new_sync(conn)
    add_order(conn, "E1", state=False)
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    assert snap["hurst_value"] is None and "沒有記到市場狀態" in snap["missing_reason"]


def test_capture_copies_values_verbatim():
    c = capture(FULL_TAG, {"stop_price": 1.5, "attempt": 2})
    assert c["market"]["hurst_value"] == 0.36 and c["market"]["iv_percentile"] == 42.0 and c["missing_reason"] is None
    assert c["stop_price"] == 1.5 and c["attempt"] == 2


# ── 3. 出場回填 ───────────────────────────────────────────────────

def test_exit_fills_back_pnl_that_can_be_checked_by_hand(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=2)
    add_deal(conn, "E1", 100.0, 2)
    add_order(conn, "X1", reason="tp", action="Sell", qty=2, ts=T0 + 60)
    add_deal(conn, "X1", 110.0, 2, ts=T0 + 61)
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert (x["trade_id"], x["exit_order_id"], x["lots"], x["entry_price"], x["exit_price"]) == ("E1", "X1", 2, 100.0, 110.0)
    assert (x["pnl_points"], x["pnl_ntd"], x["exit_reason"]) == (10.0, 200.0, "take_profit")        # 10 點 × 2 口 × 10 元
    (e,) = fetch_entries(conn_path := _dump(conn))
    assert (e["status"], e["exit_lots"], e["exit_price_avg"], e["pnl_points"], e["pnl_ntd"], e["exit_reason"]) == ("closed", 2, 110.0, 10.0, 200.0, "take_profit")
    assert e["exit_time"].endswith("+08:00") and conn_path.exists()


def _dump(conn, tmp=[None]):
    """把記憶體資料庫另存成檔案，讓 fetch_entries（唯讀、自己開連線）讀得到。"""
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="entrylog_"))
    p = d / "t.db"
    dst = sqlite3.connect(p)
    conn.backup(dst)
    dst.close()
    return p


def test_short_trades_and_losses(conn):
    s = new_sync(conn)
    add_order(conn, "E1", action="Sell", qty=1)
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "X1", reason="sl", action="Buy", ts=T0 + 5)
    add_deal(conn, "X1", 112.0, 1, ts=T0 + 6)
    s.step()
    (snap,) = rows(conn, "entry_snapshots")
    (x,) = rows(conn, "exit_fills")
    assert snap["direction"] == "short" and x["pnl_points"] == -12.0 and x["pnl_ntd"] == -120.0 and x["exit_reason"] == "stop_loss"


def test_partial_entry_fills_and_a_split_exit_are_matched_fifo(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=3)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 1)
    add_deal(conn, "E1", 101.0, 2, ts=T0 + 2)                          # 部分成交：同一張委託分兩次成交
    add_order(conn, "X1", reason="tp", action="Sell", qty=3, ts=T0 + 9)
    add_deal(conn, "X1", 105.0, 3, ts=T0 + 10)
    s.step()
    assert len(rows(conn, "entry_snapshots")) == 1                         # 仍然是一筆交易
    xs = rows(conn, "exit_fills")
    assert [(x["lots"], x["entry_price"], x["pnl_points"], x["pnl_ntd"]) for x in xs] == [(1, 100.0, 5.0, 50.0), (2, 101.0, 4.0, 80.0)]
    (e,) = fetch_entries(_dump(conn))
    assert (e["entry_price"], round(e["entry_price_avg"], 4), e["filled_lots"], e["order_lots"]) == (100.0, 100.6667, 3, 3)
    assert (e["status"], e["exit_lots"], e["pnl_ntd"]) == ("closed", 3, 130.0)


def test_a_partial_exit_leaves_the_trade_partial_until_the_rest_is_closed(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=2)
    add_deal(conn, "E1", 100.0, 2)
    add_order(conn, "X1", reason="tp", action="Sell", ts=T0 + 5)
    add_deal(conn, "X1", 104.0, 1, ts=T0 + 6)
    s.step()
    assert fetch_entries(_dump(conn))[0]["status"] == "partial"
    add_order(conn, "X2", reason="sl", action="Sell", ts=T0 + 8)
    add_deal(conn, "X2", 98.0, 1, ts=T0 + 9)
    s.step()
    (e,) = fetch_entries(_dump(conn))
    assert e["status"] == "closed" and e["pnl_points"] == 2.0 and e["exit_reason"] == "take_profit,stop_loss"


@pytest.mark.parametrize("reason,attempt,expected", [
    ("tp", None, "take_profit"), ("sl", None, "stop_loss"), ("sl", 1, "stop_loss"),
    ("sl", 2, "stop_loss_relay"), ("sl", 5, "stop_loss_relay"),                    # 停損單沒一次成交、重送後才出場
    ("trail", None, "trail_stop"), ("revert_exit", None, "revert_target"), ("reverse_close", None, "reversal")])
def test_exit_reasons(conn, reason, attempt, expected):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "X1", reason=reason, action="Sell", strategy="manual_monitor", ts=T0 + 5, ctx={"attempt": attempt} if attempt else None)
    add_deal(conn, "X1", 99.0, 1, ts=T0 + 6)
    s.step()
    assert rows(conn, "exit_fills")[0]["exit_reason"] == expected


def test_a_stop_that_needed_a_second_order_is_a_relay_even_without_an_attempt_counter(conn):
    """策略自己重送停損時沒有「第幾次」的計數；改看委託紀錄：這筆進場之後、這張出場單之前，同一個策略還送過別張 sl（送單失敗的 trade_id 是空的）。"""
    s = new_sync(conn)
    add_order(conn, "E1", strategy="ma_cross")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 5)                    # 第一張停損：送單就失敗了
    add_order(conn, "X1", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 6)                  # 第二張才成交
    add_deal(conn, "X1", 95.0, 1, ts=T0 + 7)
    s.step()
    assert rows(conn, "exit_fills")[0]["exit_reason"] == "stop_loss_relay"


def test_only_a_stop_can_be_a_relay_a_take_profit_after_a_failed_stop_stays_a_take_profit(conn):
    s = new_sync(conn)
    add_order(conn, "E1", strategy="ma_cross")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 5)                    # 停損單送單失敗
    add_order(conn, "X1", strategy="ma_cross", reason="tp", action="Sell", ts=T0 + 6)                  # 結果是停利成交
    add_deal(conn, "X1", 110.0, 1, ts=T0 + 7)
    s.step()
    assert rows(conn, "exit_fills")[0]["exit_reason"] == "take_profit"


def test_an_earlier_trades_stop_does_not_make_the_next_clean_stop_a_relay(conn):
    s = new_sync(conn)
    add_order(conn, "E0", strategy="ma_cross", ts=T0 - 100)
    add_deal(conn, "E0", 100.0, 1, ts=T0 - 99)
    add_order(conn, "X0", strategy="ma_cross", reason="sl", action="Sell", ts=T0 - 90)
    add_deal(conn, "X0", 99.0, 1, ts=T0 - 89)
    add_order(conn, "E1", strategy="ma_cross")                                                       # 新的一筆
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "X1", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 6)
    add_deal(conn, "X1", 97.0, 1, ts=T0 + 7)
    add_order(conn, "Z1", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 8)                # 出場之後才送的 sl（沒成交）：不算在先前的出場頭上
    add_order(conn, "E2", strategy="rsi", ts=T0 + 20)
    add_deal(conn, "E2", 100.0, 1, ts=T0 + 21)
    add_order(conn, "Y1", strategy="ma_cross", reason="sl", action="Sell", ts=T0 + 25)               # 別的策略在這段時間送的 sl：不算
    add_order(conn, "X2", strategy="rsi", reason="sl", action="Sell", ts=T0 + 30)
    add_deal(conn, "X2", 98.0, 1, ts=T0 + 31)
    s.step()
    assert [x["exit_reason"] for x in rows(conn, "exit_fills")] == ["stop_loss", "stop_loss", "stop_loss"]


def test_a_zero_lot_in_the_book_cannot_make_the_sync_loop_forever(conn):
    """部位簿裡若混進 0 口的部位（例如存壞的舊部位紀錄）：不能卡住（min() 會算出 0 口），也不能寫出一列「平了 0 口」的出場。"""
    import signal

    class Stuck(BaseException):                                          # 不是 Exception：不會被 step() 裡的 except 吞掉
        pass

    def on_alarm(*_):
        raise Stuck("同步卡在迴圈裡")

    conn.executescript(el.SCHEMA)
    conn.execute("INSERT INTO entry_log_meta(key, value) VALUES('legacy:sim:TMF:0', ?)", (json.dumps({"side": 1, "qty": 0, "price": 100.0}),))
    conn.commit()
    s = EntrySync(conn)
    s.init()
    add_order(conn, "M1", strategy="manual", reason="manual", action="Sell")
    add_deal(conn, "M1", 99.0, 1)
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        s.step()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["M1"]                         # 0 口的部位被丟掉，這筆賣單是新的空單進場
    assert rows(conn, "exit_fills") == []                                                           # 沒有「平了 0 口」的出場列


def test_a_manual_close_is_labelled_manual_and_a_manual_order_that_flips_opens_a_new_trade(conn):
    s = new_sync(conn)
    add_order(conn, "M1", strategy="manual", reason="manual", action="Buy", qty=1)
    add_deal(conn, "M1", 100.0, 1)
    add_order(conn, "M2", strategy="manual", reason="manual", action="Sell", qty=2, ts=T0 + 30)
    add_deal(conn, "M2", 103.0, 2, ts=T0 + 31)                           # 賣 2 口：平掉多單 1 口＋反向開 1 口
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert x["trade_id"] == "M1" and x["exit_reason"] == "manual" and x["pnl_points"] == 3.0
    snaps = rows(conn, "entry_snapshots")
    assert [(r["trade_id"], r["direction"], r["strategy_type"]) for r in snaps] == [("M1", "long", "other"), ("M2", "short", "other")]


@pytest.mark.parametrize("contract,point", [("TMF", 10), ("MXF", 50), ("TXF", 200)])
def test_pnl_in_ntd_uses_each_products_point_value(conn, contract, point):
    """小台、大台（手動下單、當沖）也要記進來，損益的元換算用各自的每點價值。"""
    s = new_sync(conn)
    add_order(conn, "E1", strategy="manual", reason="manual", contract=contract, qty=2)
    add_deal(conn, "E1", 100.0, 2)
    add_order(conn, "X1", strategy="manual", reason="manual", contract=contract, action="Sell", qty=2, ts=T0 + 5)
    add_deal(conn, "X1", 103.0, 2, ts=T0 + 6)
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert (x["pnl_points"], x["pnl_ntd"]) == (3.0, 3.0 * 2 * point)


def test_a_stop_without_any_known_entry_is_recorded_unmatched_and_opens_nothing(conn):
    s = new_sync(conn)
    add_order(conn, "X1", reason="sl", action="Sell")
    add_deal(conn, "X1", 99.0, 1)
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert x["trade_id"] == "" and x["pnl_points"] is None and "沒有對應的進場快照" in x["note"]
    assert rows(conn, "entry_snapshots") == [] and not s.book[("sim", "TMF")]


# ── 啟用前就存在的部位 ────────────────────────────────────────────

def test_a_position_that_existed_before_logging_is_not_mistaken_for_a_new_entry(conn):
    s = new_sync(conn)
    s.reconcile("sim", [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 48981.0}])
    add_order(conn, "M1", strategy="manual", reason="manual", action="Sell")
    add_deal(conn, "M1", 49000.0, 1)
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert x["trade_id"] == "" and x["pnl_points"] == 19.0 and "券商持倉均價" in x["note"]
    assert rows(conn, "entry_snapshots") == []                            # 沒有憑空多出一筆「空單進場」


def test_a_mismatch_between_the_broker_and_the_book_is_reported_not_papered_over(conn):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    s.reconcile("sim", [{"code": "TMFJ6", "direction": "Buy", "quantity": 3, "price": 100.0}])
    assert el.alerts.count == 1 and "券商持倉 +3" in el.alerts.last["error"]
    assert sum(lot["qty"] for lot in s.book[("sim", "TMF")]) == 1          # 不去調整


def test_a_legacy_position_closed_manually_is_not_turned_into_a_new_entry_after_a_restart(conn):
    """啟動時補的舊部位若沒存下來，重啟重放時帳上是空的，手動平倉（reason=manual）會被當成新的空單進場。"""
    s = new_sync(conn)
    s.reconcile("sim", [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 48981.0}])
    add_order(conn, "M1", strategy="manual", reason="manual", action="Sell")
    add_deal(conn, "M1", 49000.0, 1)
    s.step()
    exits, snaps = rows(conn, "exit_fills"), rows(conn, "entry_snapshots")
    assert snaps == [] and len(exits) == 1 and exits[0]["pnl_points"] == 19.0
    s2 = new_sync(conn)                                                  # 重啟
    s2.reconcile("sim", [])                                              # 券商已經空手
    assert rows(conn, "entry_snapshots") == [] and rows(conn, "exit_fills") == exits
    assert not any(s2.book[k] for k in s2.book) and el.alerts.count == 0     # 沒有幻影空單，也沒有誤報


def test_a_legacy_lot_is_put_back_at_the_same_point_of_the_replay(conn):
    """舊部位是在「處理到某筆成交之後」才補的；重放時要在同一個位置放回去，之前的成交不能受它影響。"""
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "X1", reason="tp", action="Sell", ts=T0 + 5)
    add_deal(conn, "X1", 103.0, 1, ts=T0 + 6)
    s.step()
    s.reconcile("sim", [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 90.0}])    # 券商有一口帳上沒有的（外部來源）
    add_order(conn, "M1", strategy="manual", reason="manual", action="Sell", ts=T0 + 20)
    add_deal(conn, "M1", 95.0, 1, ts=T0 + 21)
    s.step()
    before = (rows(conn, "entry_snapshots"), rows(conn, "exit_fills"))
    assert [x["pnl_points"] for x in before[1]] == [3.0, 5.0] and [x["trade_id"] for x in before[1]] == ["E1", ""]
    new_sync(conn)                                                       # 重啟重放
    assert (rows(conn, "entry_snapshots"), rows(conn, "exit_fills")) == before


def test_orders_are_matched_by_product_so_full_contract_codes_work(conn):
    s = new_sync(conn)
    add_order(conn, "E1", contract="TMFJ6")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "X1", contract="TMF", reason="tp", action="Sell", ts=T0 + 5)             # 進場用完整代碼、出場用 TMF
    add_deal(conn, "X1", 104.0, 1, ts=T0 + 6)
    s.step()
    (x,) = rows(conn, "exit_fills")
    assert x["trade_id"] == "E1" and x["pnl_points"] == 4.0 and x["pnl_ntd"] == 40.0
    assert rows(conn, "entry_snapshots")[0]["contract"] == "TMFJ6"
    assert el.product("txo20000j6") is None and el.product("TXFJ6") == "TXF" and el.product("") is None


def test_each_mode_only_handles_its_own_fills_and_starts_from_its_own_point(conn):
    """sim、live 是兩個行程、共用同一個資料庫：各自只處理自己的成交，起點也各自記。"""
    add_order(conn, "L0", mode="live")
    add_deal(conn, "L0", 100.0, 1, mode="live")                           # live 在 sim 啟用前就有的成交
    sim = EntrySync(conn, "sim")
    sim.init()
    add_order(conn, "S1", mode="sim")
    add_deal(conn, "S1", 100.0, 1, mode="sim")
    add_order(conn, "L1", mode="live", ts=T0 + 1)
    add_deal(conn, "L1", 101.0, 1, mode="live", ts=T0 + 2)
    sim.step()
    assert [(r["mode"], r["trade_id"]) for r in rows(conn, "entry_snapshots")] == [("sim", "S1")]
    live = EntrySync(conn, "live")
    live.init()                                                          # live 的起點是它自己啟用的時候：L0、L1 都在起點之前
    live.step()
    assert [(r["mode"], r["trade_id"]) for r in rows(conn, "entry_snapshots")] == [("sim", "S1")]
    metas = dict(conn.execute("SELECT key, value FROM entry_log_meta WHERE key LIKE 'start_event_id%'").fetchall())
    assert set(metas) == {"start_event_id:sim", "start_event_id:live"} and int(metas["start_event_id:sim"]) < int(metas["start_event_id:live"])
    add_order(conn, "L2", mode="live", ts=T0 + 5)
    add_deal(conn, "L2", 102.0, 1, mode="live", ts=T0 + 6)
    live.step()
    sim.step()
    assert sorted((r["mode"], r["trade_id"]) for r in rows(conn, "entry_snapshots")) == [("live", "L2"), ("sim", "S1")]
    assert sum(lot["qty"] for lot in live.book[("live", "TMF")]) == 1 and ("live", "TMF") not in sim.book


def test_history_before_the_feature_started_is_not_backfilled(conn):
    add_order(conn, "OLD", ts=T0 - 9999)
    add_deal(conn, "OLD", 100.0, 1, ts=T0 - 9998)
    s = new_sync(conn)                                                   # 第一次啟用：起點 = 現有最後一筆成交回報
    s.step()
    assert rows(conn, "entry_snapshots") == []
    add_order(conn, "NEW")
    add_deal(conn, "NEW", 101.0, 1)
    s.step()
    assert [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["NEW"]


def test_options_and_unknown_contracts_are_ignored(conn):
    s = new_sync(conn)
    add_order(conn, "O1", contract="TXO20000J6")
    add_deal(conn, "O1", 150.0, 1)
    s.step()
    assert rows(conn, "entry_snapshots") == [] and rows(conn, "exit_fills") == []


# ── 7. 重啟與重複觸發：冪等 ───────────────────────────────────────

def test_restart_replays_from_the_start_without_duplicates_or_losses(conn):
    s = new_sync(conn)
    add_order(conn, "E1", qty=2)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 1)
    add_deal(conn, "E1", 100.5, 1, ts=T0 + 2)
    add_order(conn, "X1", reason="tp", action="Sell", ts=T0 + 9)
    add_deal(conn, "X1", 104.0, 1, ts=T0 + 10)
    s.step()
    before = (rows(conn, "entry_snapshots"), rows(conn, "exit_fills"))
    s2 = new_sync(conn)                                                  # 重啟：全新的物件、同一個資料庫
    s2.step()
    assert (rows(conn, "entry_snapshots"), rows(conn, "exit_fills")) == before
    assert sum(lot["qty"] for lot in s2.book[("sim", "TMF")]) == 1       # 部位簿重建正確：還剩 1 口
    add_order(conn, "X2", reason="sl", action="Sell", ts=T0 + 20)
    add_deal(conn, "X2", 99.0, 1, ts=T0 + 21)
    s2.step()
    assert len(rows(conn, "exit_fills")) == 2 and rows(conn, "exit_fills")[1]["entry_price"] == 100.5      # 重啟後接著算，不遺失


def test_running_the_same_step_twice_changes_nothing(conn):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    snap = rows(conn, "entry_snapshots")
    s.last_id = 0                                                        # 模擬指標遺失：同一批成交回報再處理一次
    s.seen.clear()
    s.book.clear()
    s.step()
    assert rows(conn, "entry_snapshots") == snap


# ── 6. 失敗：不阻擋下單、有警示、不遺失 ─────────────────────────────

def test_a_failed_write_is_alerted_and_retried_not_skipped(conn, monkeypatch, caplog):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    add_order(conn, "E2", ts=T0 + 5)
    add_deal(conn, "E2", 101.0, 1, ts=T0 + 6)
    real = s._insert_snapshot
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(s, "_insert_snapshot", flaky)
    with caplog.at_level("ERROR"):
        s.step()
    assert el.alerts.count == 1 and "database is locked" in el.alerts.last["error"] and "ENTRYLOG ALERT" in caplog.text
    assert rows(conn, "entry_snapshots") == []                            # 第一筆失敗 → 後面的也先不做（順序有關）
    assert not s.book.get(("sim", "TMF"))                                 # 資料庫回滾了，記憶體的部位簿也要退回去
    s.step()                                                             # 下一輪重試：兩筆都補上，沒有遺失
    assert [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["E1", "E2"]
    assert [(lot["trade_id"], lot["qty"]) for lot in s.book[("sim", "TMF")]] == [("E1", 1), ("E2", 1)]     # 每口只算一次，不會變成兩口


def test_a_retry_after_a_failure_does_not_leave_a_phantom_lot_that_steals_the_next_exit(conn, monkeypatch):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    real = s._insert_snapshot
    calls = {"n": 0}

    def once_broken(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*a, **k)

    monkeypatch.setattr(s, "_insert_snapshot", once_broken)
    s.step()
    s.step()
    add_order(conn, "X1", reason="tp", action="Sell", ts=T0 + 5)
    add_deal(conn, "X1", 104.0, 1, ts=T0 + 6)
    s.step()
    assert not any(s.book.values())                                      # 一進一出之後是空手；多出來的幻影口數會讓這裡留下 1 口
    (e,) = fetch_entries(_dump(conn))
    assert e["status"] == "closed"


def test_a_poison_event_is_given_up_after_a_few_tries_and_does_not_block_the_rest(conn, monkeypatch):
    s = new_sync(conn)
    add_order(conn, "BAD")
    add_deal(conn, "BAD", 100.0, 1)
    add_order(conn, "GOOD", ts=T0 + 5)
    add_deal(conn, "GOOD", 101.0, 1, ts=T0 + 6)
    real = s._insert_snapshot

    def poison(ev, od, *a, **k):
        if ev["trade_id"] == "BAD":
            raise ValueError("bad row")
        return real(ev, od, *a, **k)

    monkeypatch.setattr(s, "_insert_snapshot", poison)
    for _ in range(el.MAX_RETRY + 1):
        s.step()
    assert [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["GOOD"]
    assert any(e["where_"] == "giveup" for e in rows(conn, "entry_log_errors"))


def test_a_deal_whose_order_never_shows_up_is_reported_after_the_wait(conn):
    s = new_sync(conn)
    add_deal(conn, "GHOST", 100.0, 1, ts=T0)
    assert s.step(now=T0 + 5) == 0 and s.last_id == 0                      # 委託紀錄可能還在佇列裡：先等
    add_order(conn, "E2", ts=T0 + 6)
    add_deal(conn, "E2", 101.0, 1, ts=T0 + 7)
    s.step(now=T0 + el.ORDER_WAIT_SEC + 5)                                 # 等夠久了：通報並略過，後面的照常處理
    assert [r["trade_id"] for r in rows(conn, "entry_snapshots")] == ["E2"]
    assert any(e["where_"] == "orphan_deal" for e in rows(conn, "entry_log_errors")) and el.alerts.count >= 1


def test_the_deal_arriving_before_its_order_row_is_picked_up_on_the_next_round(conn):
    s = new_sync(conn)
    add_deal(conn, "E1", 100.0, 1, ts=T0)                                  # 成交回報比委託紀錄先到（early-fill）
    assert s.step(now=T0 + 1) == 0
    add_order(conn, "E1", ts=T0 - 0.2)
    assert s.step(now=T0 + 2) == 1 and len(rows(conn, "entry_snapshots")) == 1


# ── 端到端（真的 writer thread）：下單不受影響、失敗有警示 ────────────

@pytest.fixture
def live_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    log = TradeLog(tmp_path / "trade_log.db")
    log.enabled = True
    log.start()
    yield log
    log.stop()


def test_end_to_end_through_the_writer_thread(live_log, tmp_path):
    live_log.set_market_tag(FULL_TAG)
    with live_log.context(strategy="ma_cross", reason="entry", signal_price=48980.0, stop_price=48900.0, strategy_params={"short_period": 5}):
        live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 48981.0, "quantity": 1, "deal_ts": T0 + 1.2, "deal_seq": "S1"})
    with live_log.context(strategy="ma_cross", reason="sl"):
        live_log.record_order(contract="TMF", action="Sell", qty=1, trade_id="X1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "X1", "price": 48900.0, "quantity": 1, "deal_ts": T0 + 9.9, "deal_seq": "S2"})
    live_log.stop()
    (e,) = fetch_entries(live_log.path, mode="sim")
    assert (e["trade_id"], e["strategy_name"], e["strategy_type"], e["direction"], e["stop_price"], e["hurst_value"]) == \
           ("E1", "ma_cross", "trend", "long", 48900.0, 0.36)
    assert (e["status"], e["pnl_points"], e["pnl_ntd"], e["exit_reason"]) == ("closed", -81.0, -810.0, "stop_loss")
    assert json.loads(e["strategy_params"]) == {"short_period": 5}
    st = live_log.entry_status()
    assert st["snapshots"] == 1 and st["exits"] == 1 and st["alerts"] == 0 and st["pending_deals"] == 0
    assert st["start_event_id"] == 0 and st["enabled"] is True


def test_reconcile_sees_fills_that_are_still_waiting_in_the_queue(live_log):
    """對帳前要先把佇列裡的委託與成交寫進去、處理完；否則帳上是空的，會把剛成交的部位當成「舊部位」。"""
    live_log.set_market_tag(FULL_TAG)
    live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 100.0, "quantity": 1})
    live_log.record_positions([{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 100.0}])
    live_log.stop()
    assert el.alerts.count == 0
    with sqlite3.connect(live_log.path) as c:
        assert c.execute("SELECT COUNT(*) FROM entry_log_meta WHERE key LIKE 'legacy:%'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM entry_snapshots").fetchone()[0] == 1


def test_positions_given_at_startup_reach_the_reconcile(live_log):
    live_log.record_positions([{"code": "TMFJ6", "direction": "Sell", "quantity": 2, "price": 48990.0},
                               {"code": "TXO20000J6", "direction": "Buy", "quantity": 1, "price": 100.0}])      # 選擇權不管
    live_log.stop()
    with sqlite3.connect(live_log.path) as c:
        (rec,) = c.execute("SELECT key, value FROM entry_log_meta WHERE key LIKE 'legacy:%'").fetchall()
    assert rec[0] == "legacy:sim:TMF:0" and json.loads(rec[1]) == {"side": -1, "qty": 2, "price": 48990.0}


def test_status_counts_only_futures_deals_of_its_own_mode_that_were_not_processed(conn):
    s = EntrySync(conn, "sim")
    s.init()
    add_order(conn, "O1", contract="TXO20000J6")
    add_deal(conn, "O1", 150.0, 1)                                       # 選擇權不處理，也不該算成「待處理」
    add_order(conn, "L1", mode="live")
    add_deal(conn, "L1", 100.0, 1, mode="live")                          # 別的模式的
    add_order(conn, "E1", ts=T0 + 5)
    add_deal(conn, "E1", 100.0, 1, ts=T0 + 6)                            # 還沒 step → 待處理
    p = _dump(conn)
    assert el.status(p, "sim")["pending_deals"] == 1 and el.status(p, "live")["pending_deals"] == 0 == el.status(p, "sim")["errors_logged"]
    s.step()
    assert el.status(_dump(conn), "sim")["pending_deals"] == 0


def test_a_fill_delivered_twice_by_the_broker_is_counted_once_end_to_end(live_log):
    live_log.set_market_tag(FULL_TAG)
    live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")
    deal = {"state": "FuturesDeal", "trade_id": "E1", "price": 48981.0, "quantity": 1, "deal_ts": T0 + 1.2, "deal_seq": "S1"}
    live_log.record_event(deal)
    live_log.record_event(dict(deal))                                     # 券商重送同一筆成交回報
    live_log.record_order(contract="TMF", action="Sell", qty=1, trade_id="X1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "X1", "price": 48990.0, "quantity": 1, "deal_ts": T0 + 5.5, "deal_seq": "S2"})
    live_log.stop()
    (e,) = fetch_entries(live_log.path, mode="sim")
    assert (e["filled_lots"], e["status"], e["pnl_points"]) == (1, "closed", 9.0)
    assert e["entry_time"] == el.iso_taipei(T0 + 1.2) and e["exit_time"] == el.iso_taipei(T0 + 5.5)      # 用券商給的成交時間，不是我們收到的時間
    with sqlite3.connect(live_log.path) as c:
        assert c.execute("SELECT COUNT(*) FROM exit_fills").fetchone()[0] == 1


def test_a_duplicate_is_recognised_by_the_exchange_sequence_alone(live_log):
    """券商只給序號、沒給成交時間時，靠序號辨識重複（委託 3 口、還有空間，所以不是靠「累計不超過委託口數」擋掉的）。"""
    live_log.set_market_tag(FULL_TAG)
    live_log.record_order(contract="TMF", action="Buy", qty=3, trade_id="E1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 48981.0, "quantity": 1, "deal_seq": "S1"})
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 48981.0, "quantity": 1, "deal_seq": "S2"})
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 48981.0, "quantity": 1, "deal_seq": "S1"})      # S1 又送了一次
    live_log.stop()
    (e,) = fetch_entries(live_log.path, mode="sim")
    assert e["filled_lots"] == 2


def test_entries_show_up_while_running_without_waiting_for_shutdown(live_log):
    live_log.set_market_tag(FULL_TAG)
    live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 100.0, "quantity": 1})
    deadline = time.time() + 8
    got: list = []
    while time.time() < deadline and not got:
        got = fetch_entries(live_log.path, mode="sim")
        time.sleep(0.1)
    assert [e["trade_id"] for e in got] == ["E1"]                          # 沒有呼叫 stop()，執行中的定期同步就把它寫出來


def test_a_failing_capture_never_blocks_or_loses_the_order_row(live_log, monkeypatch):
    monkeypatch.setattr(el, "capture", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")       # 不會丟例外
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 100.0, "quantity": 1})
    live_log.stop()
    assert el.alerts.count == 1 and "boom" in el.alerts.last["error"] and el.alerts.last["where"] == "capture"
    with sqlite3.connect(live_log.path) as c:
        assert c.execute("SELECT trade_id, state_json FROM orders").fetchall() == [("E1", None)]       # 委託照記
        assert c.execute("SELECT missing_reason FROM entry_snapshots").fetchone()[0].startswith("這張進場委託沒有記到市場狀態")


def test_existing_databases_are_migrated_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    p = tmp_path / "old.db"
    with sqlite3.connect(p) as c:                                         # 舊版的資料表（沒有 state_json／deal_ts／deal_seq）
        c.executescript("""CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL,
            trade_id TEXT NOT NULL DEFAULT '', strategy TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', contract TEXT, action TEXT, qty INTEGER,
            price_type TEXT, order_type TEXT, limit_price REAL, octype TEXT, signal_price REAL, ref_price REAL, market_state TEXT, hurst_state TEXT,
            iv_state TEXT, direction INTEGER, tag_date TEXT, status TEXT, error TEXT, latency_ms REAL);
            CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL,
            trade_id TEXT NOT NULL DEFAULT '', price REAL, qty INTEGER, op_type TEXT, op_code TEXT, op_msg TEXT, raw_state TEXT);
            INSERT INTO orders(ts, mode, trade_id, contract, action, qty) VALUES(1,'sim','OLD','TMF','Buy',1);
            INSERT INTO events(ts, mode, kind, trade_id, price, qty) VALUES(2,'sim','deal','OLD',100,1);""")
    log = TradeLog(p)
    log.enabled = True
    log.start()
    log.set_market_tag(FULL_TAG)
    log.record_order(contract="TMF", action="Buy", qty=1, trade_id="NEW", status="Submitted")
    log.record_event({"state": "FuturesDeal", "trade_id": "NEW", "price": 101.0, "quantity": 1})
    log.stop()
    with sqlite3.connect(p) as c:
        assert {r[1] for r in c.execute("PRAGMA table_info(orders)")} >= {"state_json"}
        assert {r[1] for r in c.execute("PRAGMA table_info(events)")} >= {"deal_ts", "deal_seq"}
        assert [r[0] for r in c.execute("SELECT trade_id FROM entry_snapshots")] == ["NEW"]         # 舊的成交不回補


def test_two_processes_migrating_the_shared_database_at_the_same_time_do_not_break_each_other(conn):
    """sim、live 同時啟動：另一個行程剛好把欄位加好，這邊再加會得到「duplicate column」——那不是錯誤。"""
    class Blind:                                                          # 看不到欄位（PRAGMA 查不到），所以會去 ALTER 已經存在的欄位
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            return iter(()) if sql.startswith("PRAGMA table_info") else self.c.execute(sql, *a)

    TradeLog._migrate(Blind(conn))                                       # 不能丟例外
    assert {r[1] for r in conn.execute("PRAGMA table_info(orders)")} >= {"state_json"}


def test_a_real_migration_error_is_not_swallowed(conn):
    class Broken:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            if sql.startswith("PRAGMA table_info"):
                return iter(())
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        TradeLog._migrate(Broken(conn))


# ── 4. 啟用前後送出的訂單完全相同 ──────────────────────────────────

def drive_strategy(ctxs: list | None = None):
    """用真的 BaseStrategy.place_order → broker.place_order 的路徑跑一串進出場；回傳送給 worker 的所有指令。
    ctxs 給了就把每張單送出當下的 trade_log 脈絡也收進去。"""
    import strategies.base as base
    from core.broker import BrokerClient
    from datetime import datetime
    from strategies.ma_cross import MACrossStrategy
    sent: list = []

    async def fake_acall(self, method, **kw):
        if ctxs is not None:
            ctxs.append(dict(tl._CTX.get() or {}))
        sent.append((method, kw))
        return {"trade_id": f"T{len(sent)}", "status": "Submitted"}

    async def margin_ok(self):
        return True

    orig_acall, orig_margin = BrokerClient._acall, base.BaseStrategy._margin_ok
    BrokerClient._acall, base.BaseStrategy._margin_ok = fake_acall, margin_ok
    try:
        s = MACrossStrategy()
        s.short_period, s.long_period, s.stop_loss_pts = 1, 3, 3
        s._apply_params({"short_period": 1, "long_period": 3})
        now = datetime(2026, 10, 12, 10, 0)
        s._now = lambda: now
        s._roll_risk_day()

        async def go():
            for p in (10, 10, 10, 12, 14, 12, 10, 8, 6, 5, 4, 9, 13):
                await s._on_quote_async({"code": "TMFJ6", "close": float(p), "tick_type": 0, "volume": 0, "total_volume": 0})
        asyncio.run(go())
    finally:
        BrokerClient._acall, base.BaseStrategy._margin_ok = orig_acall, orig_margin
    return sent


def test_the_orders_sent_are_identical_with_the_feature_on_off_or_failing(monkeypatch):
    from core.trade_log import trade_log
    monkeypatch.setattr(trade_log, "enabled", True)
    monkeypatch.setattr(trade_log, "_tag", FULL_TAG)
    while not trade_log._q.empty():
        trade_log._q.get_nowait()
    on = drive_strategy()
    queued = trade_log._q.qsize()
    monkeypatch.setattr(trade_log, "enabled", False)
    off = drive_strategy()
    monkeypatch.setattr(trade_log, "enabled", True)
    monkeypatch.setattr(el, "capture", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("capture broken")))
    broken = drive_strategy()
    assert len(on) >= 5 and on == off == broken                            # 送給 worker 的指令一模一樣
    assert queued >= len(on)                                               # 且「開」的那次確實有在記（不是測了個沒開的功能）
    while not trade_log._q.empty():
        trade_log._q.get_nowait()


def test_strategy_entries_carry_stop_price_and_params_exits_do_not(monkeypatch):
    ctxs: list = []
    sent = drive_strategy(ctxs)
    assert len(ctxs) == len(sent) >= 5
    entries = [c for c in ctxs if c.get("reason") == "entry"]
    exits = [c for c in ctxs if c.get("reason") != "entry"]
    assert entries and exits
    for c, (_, kw) in zip(ctxs, sent):
        if c.get("reason") == "entry":
            sign = 1 if kw["action"] == "Buy" else -1
            assert c["strategy"] == "ma_cross" and c["signal_price"] is not None
            assert c["stop_price"] == c["signal_price"] - sign * 3             # 停損價 = 進場參考價 ∓ 停損點數
            assert c["strategy_params"]["short_period"] == 1 and c["strategy_params"]["long_period"] == 3
        else:
            assert "stop_price" not in c and "strategy_params" not in c       # 出場單不帶進場快照的脈絡


def test_scalp_entry_context_carries_the_stop_price_and_params(monkeypatch):
    import strategies.scalp as scalp_mod
    seen: list = []

    async def place_order(**kw):
        seen.append({**dict(tl._CTX.get() or {}), "kw": kw})
        return {"trade_id": f"T{len(seen)}", "status": "PendingSubmit"}

    async def empty(*a, **k):
        return []

    for name, fn in (("place_order", place_order), ("list_trades_with_status", empty), ("list_positions", empty)):
        monkeypatch.setattr(scalp_mod.broker, name, fn)
    monkeypatch.setattr(scalp_mod, "ENTRY_POSITION_CHECK", True)
    s = scalp_mod.ScalpStrategy()
    s.tp_pts, s.sl_pts, s.max_qty = 20, 60, 1
    s.state.last_price = 48980.0
    asyncio.run(s._do_enter(48980.0, 1))
    (c,) = seen
    assert c["reason"] == "entry" and c["strategy"] == "scalp" and c["signal_price"] == 48980.0
    assert c["stop_price"] == c["kw"]["price"] - 60                              # 多單：掛單價 − 停損點數
    assert c["strategy_params"]["tp_pts"] == 20 and c["strategy_params"]["sl_pts"] == 60
    s._phase = "idle"
    s.sl_pts = 0                                                                 # 沒設停損 → 停損價是 null，不是 0
    seen.clear()
    asyncio.run(s._do_enter(48980.0, -1))
    assert seen[0]["stop_price"] is None


def test_manual_order_context_has_the_stop_price(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes_order as ro
    seen: list = []

    async def place_order(**kw):
        seen.append({**dict(tl._CTX.get() or {}), "kw": kw})
        return {"trade_id": "M1", "status": "PendingSubmit"}

    monkeypatch.setattr(ro.broker, "place_order", place_order)
    monkeypatch.setattr(ro.quote_hub, "last_price_by_prefix", lambda c: 48980.0)
    monkeypatch.setattr(ro.manual_monitor, "add", lambda **k: "w1")
    app = FastAPI()
    app.include_router(ro.router, prefix="/api/order")
    c = TestClient(app)
    r = c.post("/api/order/place", json={"contract": "TMF", "action": "Buy", "quantity": 1, "price_type": "MKT", "order_type": "IOC",
                                          "stop_loss_pts": 30, "take_profit_pts": 0})
    assert r.status_code == 200, r.text
    r = c.post("/api/order/place", json={"contract": "TMF", "action": "Sell", "quantity": 1, "price_type": "LMT", "order_type": "ROD",
                                          "price": 49000, "stop_loss_pts": 30, "take_profit_pts": 0})
    assert r.status_code == 200, r.text
    r = c.post("/api/order/place", json={"contract": "TMF", "action": "Buy", "quantity": 1, "price_type": "MKT", "order_type": "IOC",
                                          "stop_loss_pts": 0, "take_profit_pts": 0})
    assert [(x["strategy"], x["reason"], x["stop_price"]) for x in seen] == \
           [("manual", "manual", 48950.0), ("manual", "manual", 49030.0), ("manual", "manual", None)]    # 市價用最新價、限價用委託價；沒設停損 → null


def test_manual_monitor_close_orders_carry_the_attempt_number(monkeypatch):
    from core.broker import broker
    from core.manual_monitor import ManualOrderMonitor, ManualWatch
    from core.quote_hub import quote_hub
    seen: list = []

    async def place_option_order(**kw):
        seen.append(dict(tl._CTX.get() or {}))
        return {"trade_id": f"O{len(seen)}", "status": "PendingSubmit"}

    monkeypatch.setattr(broker, "place_option_order", place_option_order)
    monkeypatch.setattr(broker, "subscribe_option_sync", lambda *a, **k: None)
    monkeypatch.setitem(quote_hub._last_price, "TXO49800J6", 740.0)
    mm = ManualOrderMonitor()
    t = [1000.0]
    mm._clock = lambda: t[0]
    w = ManualWatch(id="w", contract="TXO", direction=1, quantity=1, entry_price=800.0, stop_loss_pts=50, take_profit_pts=0, is_option=True,
                    match_code="TXO49800J6", delivery_month="202610", strike_price=49800, option_right="C")
    mm._watches["w"] = w
    pos = [{"code": "TXO49800J6", "direction": "Buy", "quantity": 1, "price": 800.0, "last_price": 740.0}]
    asyncio.run(mm._check(w, pos, {}, {}))
    t[0] += 30
    w.retry_after = 0
    asyncio.run(mm._check(w, pos, {}, {}))
    assert [(c["strategy"], c["reason"], c["attempt"]) for c in seen[:2]] == [("manual_monitor", "sl", 1), ("manual_monitor", "sl", 2)]


# ── 5. 沒有任何策略或風控模組讀取這些欄位 ───────────────────────────

FORBIDDEN = ("entry_log", "entry_snapshots", "exit_fills", "state_json", "missing_reason", "hurst_value", "hurst_noise",
             "iv_percentile", "fetch_entries", "entry_status")


def test_no_strategy_or_risk_module_reads_the_recorded_market_state():
    files = sorted((ROOT / "strategies").glob("*.py")) + [ROOT / "api" / "routes_strategy.py", ROOT / "core" / "manual_monitor.py",
                                                         ROOT / "core" / "ruin.py", ROOT / "api" / "routes_risk.py", ROOT / "core" / "broker_guard.py"]
    offenders = [f"{f.name}: {w}" for f in files for w in FORBIDDEN if w in f.read_text(encoding="utf-8")]
    assert len(files) >= 12 and not offenders, offenders


def test_the_recorded_fields_only_flow_into_the_log(monkeypatch):
    """脈絡（stop_price、strategy_params）只被 trade_log.context 收走：策略拿不到任何從紀錄讀回來的東西。"""
    src = (ROOT / "strategies" / "base.py").read_text(encoding="utf-8")
    assert "trade_log.context" in src and "trade_log." not in re.sub(r"trade_log\.context", "", src)


# ── 策略類型由設定明確標示 ───────────────────────────────────────────

def test_every_strategy_has_an_explicitly_defined_type():
    from api.routes_strategy import strategy_engine
    names = set(strategy_engine.strategies) | {"manual", "manual_monitor"}
    assert names <= set(el.STRATEGY_TYPES), names - set(el.STRATEGY_TYPES)
    assert {el.STRATEGY_TYPES[n] for n in names} <= {"trend", "mean_reversion", "other"}
    assert strategy_type("ma_cross") == "trend" and strategy_type("rsi") == "mean_reversion" and strategy_type("scalp") == "other"
    assert strategy_type("something_new") == "other" and strategy_type("") == "other"                 # 沒定義的不猜


def test_the_market_tag_set_by_daily_summary_carries_the_snapshot(monkeypatch):
    from core.daily_summary import market_state
    from core.trade_log import trade_log
    monkeypatch.setattr(trade_log, "_tag", {})
    s = {"date": "2026-10-12", "state": "TREND_NORMAL", "direction": 1,
         "hurst": {"value": 0.36, "z": -1.1, "se": 0.132, "window": 60, "state": "TREND", "note": ""},
         "iv": {"value": 18.5, "percentile": None, "history_n": 3, "min_history": 60, "state": "UNKNOWN", "note": ""}}
    before = time.time()
    market_state._set_summary(s)
    snap = trade_log._tag["snapshot"]
    assert (snap["hurst_value"], snap["hurst_z"], snap["hurst_noise"], snap["hurst_window"], snap["iv_value"], snap["iv_percentile"]) == \
           (0.36, -1.1, 0.132, 60, 18.5, None)
    assert snap["state_timestamp"] >= before and snap["state_date"] == "2026-10-12" and snap["market_state"] == "TREND_NORMAL"
    assert (snap["iv_history_n"], snap["iv_min_history"]) == (3, 60)
    s["iv"].update(percentile=42.0, history_n=80)                           # IV 歷史夠了：百分位要原樣帶進去
    market_state._set_summary(s)
    assert trade_log._tag["snapshot"]["iv_percentile"] == 42.0
    market_state.summary = None


def test_the_snapshot_says_which_bar_period_the_hurst_was_computed_on(monkeypatch):
    import dataclasses
    from core.daily_summary import market_state
    from core.trade_log import trade_log
    monkeypatch.setattr(trade_log, "_tag", {})
    s = {"date": "2026-10-12", "state": "TREND_NORMAL", "direction": 1, "hurst": {"value": 0.36, "state": "TREND"}, "iv": {"state": "UNKNOWN"}}
    for freq, label in (("D", "日K"), ("5m", "5 分K")):
        monkeypatch.setattr(market_state, "cfg", dataclasses.replace(market_state.cfg, hurst_freq=freq))
        market_state._set_summary(s)
        assert trade_log._tag["snapshot"]["hurst_bar_period"] == label
    market_state.summary = None


# ── 8. 匯出 ─────────────────────────────────────────────────────────

def test_csv_has_every_column_nulls_are_empty_and_times_are_taipei(conn):
    s = new_sync(conn)
    add_order(conn, "E1", tag={}, qty=1)                                  # 缺值的
    add_deal(conn, "E1", 100.0, 1, ts=1_760_000_000.0)
    add_order(conn, "E2", ts=T0 + 5)
    add_deal(conn, "E2", 101.0, 1, ts=T0 + 6)
    s.step()
    text = to_csv(fetch_entries(_dump(conn)))
    got = list(csv.DictReader(io.StringIO(text)))
    assert list(got[0].keys()) == list(el.CSV_FIELDS) and len(got) == 2
    e1 = next(r for r in got if r["trade_id"] == "E1")
    assert e1["hurst_value"] == "" and e1["iv_percentile"] == "" and e1["missing_reason"] != ""        # null 是空欄，不是 0
    assert e1["entry_time"] == "2025-10-09T16:53:20+08:00" and e1["status"] == "open"                      # 1_760_000_000 = 2025-10-09 08:53:20 UTC
    assert all(r["entry_time"].endswith("+08:00") for r in got)
    e2 = next(r for r in got if r["trade_id"] == "E2")
    assert e2["state_timestamp"].endswith("+08:00") and e2["state_age_sec"] == "3606"


def test_fetch_and_status_on_a_database_without_the_feature(tmp_path):
    assert fetch_entries(tmp_path / "nope.db") == [] and el.status(tmp_path / "nope.db")["snapshots"] == 0
    p = tmp_path / "plain.db"
    sqlite3.connect(p).close()
    assert fetch_entries(p) == [] and el.status(p)["snapshots"] == 0


def test_corrections_are_new_rows_pointing_at_the_original(conn, tmp_path):
    s = new_sync(conn)
    add_order(conn, "E1")
    add_deal(conn, "E1", 100.0, 1)
    s.step()
    p = _dump(conn)
    before = rows(sqlite3.connect(p), "entry_snapshots")
    cid = el.add_correction(p, "sim", "E1", "strategy_type", "mean_reversion", "分類改了")
    assert cid >= 1
    after = rows(sqlite3.connect(p), "entry_snapshots")
    assert before == after                                                 # 歷史列不動
    (e,) = fetch_entries(p)
    corr = json.loads(e["corrections"])
    assert corr == [{"field": "strategy_type", "old_value": "other", "new_value": "mean_reversion", "reason": "分類改了",
                     "created_ts": corr[0]["created_ts"]}] and e["strategy_type"] == "other"


def test_the_api_exports_csv_with_a_bom_and_a_status_endpoint(live_log, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes_tradelog as rt
    monkeypatch.setattr(rt, "trade_log", live_log)
    live_log.set_market_tag(FULL_TAG)
    live_log.record_order(contract="TMF", action="Buy", qty=1, trade_id="E1", status="Submitted")
    live_log.record_event({"state": "FuturesDeal", "trade_id": "E1", "price": 100.0, "quantity": 1})
    live_log.stop()
    app = FastAPI()
    app.include_router(rt.router, prefix="/api/tradelog")
    c = TestClient(app)
    r = c.get("/api/tradelog/entries.csv")
    assert r.status_code == 200 and r.text.startswith("﻿") and "trade_id" in r.text.splitlines()[0] and "E1" in r.text
    assert "text/csv" in r.headers["content-type"] and "attachment" in r.headers["content-disposition"]
    assert c.get("/api/tradelog/entries").json()[0]["trade_id"] == "E1"
    st = c.get("/api/tradelog/entries/status").json()
    assert st["snapshots"] == 1 and st["alerts"] == 0 and st["enabled"] is True


def test_worker_events_carry_the_broker_deal_time_and_sequence():
    from core.shioaji_worker import extract_order_event

    class S:
        name = "FuturesDeal"
    ev = extract_order_event(S(), {"trade_id": "E1", "price": 100.0, "quantity": 1, "ts": 1760000000.5, "exchange_seq": "000123"})
    assert ev["deal_ts"] == 1760000000.5 and ev["deal_seq"] == "000123"
    ev = extract_order_event(S(), {"trade_id": "E1", "price": 100.0, "quantity": 1})
    assert ev["deal_ts"] == 0.0 and ev["deal_seq"] == ""
