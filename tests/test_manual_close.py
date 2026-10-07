"""手動停損/停利的平倉確認與重送（core/manual_monitor.py）。

修正前：觸發 → 送一張 IOC → 立刻移除監看，完全不確認成交；IOC 沒成交時部位就失去停損保護。
修正後：送出後不移除，確認結果——全數成交才移除、沒成交才重送（有冷卻、有次數上限、口數不超過
這個監看剩下的量與當下部位），避免重複平倉變成反向開倉。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from core.broker import broker
from core.manual_monitor import (
    CLOSE_FILLED_GRACE, CLOSE_LIVE_TIMEOUT, CLOSE_RETRY_MIN_GAP, CLOSE_UNKNOWN_TIMEOUT,
    MAX_CLOSE_ATTEMPTS, ManualOrderMonitor, ManualWatch,
)
from core.quote_hub import quote_hub

CODE = "TXO49800J6"


def pos(qty: int = 1, code: str = CODE, last: float = 740.0) -> dict:
    return {"code": code, "direction": "Buy", "quantity": qty, "price": 800.0, "last_price": last}


class Env:
    """假時鐘 + 假 broker：記錄送出的單，可讓下一次送單丟例外。"""

    def __init__(self, monkeypatch) -> None:
        self.t = 1000.0
        self.sent: list[tuple[str, dict]] = []
        self.fail_next = 0
        self.mm = ManualOrderMonitor()
        self.mm._clock = lambda: self.t
        env = self

        async def place_option_order(**kw):
            if env.fail_next:
                env.fail_next -= 1
                raise asyncio.TimeoutError()
            env.sent.append(("opt", kw))
            return {"trade_id": f"O{len(env.sent)}", "status": "PendingSubmit"}

        async def place_order(**kw):
            env.sent.append(("fut", kw))
            return {"trade_id": f"F{len(env.sent)}", "status": "PendingSubmit"}

        monkeypatch.setattr(broker, "place_option_order", place_option_order)
        monkeypatch.setattr(broker, "place_order", place_order)
        monkeypatch.setattr(broker, "subscribe_option_sync", lambda *a, **k: None)
        monkeypatch.setitem(quote_hub._last_price, CODE, 740.0)        # 進場 800、停損 50 點 → 740 觸發

    def option_watch(self, qty: int = 1, **kw) -> ManualWatch:
        w = ManualWatch(id="w", contract="TXO", direction=1, quantity=qty, entry_price=800.0,
                        stop_loss_pts=50, take_profit_pts=0, is_option=True, match_code=CODE,
                        delivery_month="202610", strike_price=49800, option_right="C", **kw)
        self.mm._watches["w"] = w
        return w

    def check(self, w: ManualWatch, positions, status=None, fills=None) -> None:
        asyncio.run(self.mm._check(w, positions, status or {}, fills or {}))

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


def test_ioc_not_filled_keeps_the_watch_and_retries_with_a_wider_limit(env):
    w = env.option_watch()
    env.check(w, [pos()])                                      # 觸發停損 → 送第 1 張
    assert len(env.sent) == 1 and "w" in env.mm._watches       # 修正前：這裡監看已經被移除
    first = env.sent[0][1]
    assert (first["order_type"], first["quantity"], first["price"], first["action"]) == ("IOC", 1, 740.0, "Sell")

    env.advance(1)
    env.check(w, [pos()], {"O1": "Cancelled"}, {"O1": 0})      # IOC 沒成交
    assert len(env.sent) == 1                                  # 冷卻中，不立刻重送

    env.advance(CLOSE_RETRY_MIN_GAP + 1)
    env.check(w, [pos()])                                      # 過了冷卻 → 重送，限價放寬
    assert len(env.sent) == 2 and w.close_attempts == 2
    assert env.sent[1][1]["price"] < first["price"] and env.sent[1][1]["quantity"] == 1

    env.check(w, [])                                           # 部位真的平了 → 監看才移除
    assert "w" not in env.mm._watches


def test_full_fill_removes_the_watch_but_never_touches_other_lots(env):
    w = env.option_watch(qty=1)
    env.check(w, [pos(2)])                                     # 持倉 2 口（另一口屬於別的單），這個監看只管 1 口
    assert env.sent[0][1]["quantity"] == 1
    env.advance(1)
    env.check(w, [pos(1)], {"O1": "Filled"}, {"O1": 1})        # 平掉了自己的 1 口，持倉還剩別人的 1 口
    assert "w" not in env.mm._watches and len(env.sent) == 1   # 不能因為「部位還在」就去平別人的


def test_filled_status_without_fill_info_counts_as_fully_filled(env):
    w = env.option_watch()
    env.check(w, [pos()])
    env.advance(1)
    env.check(w, [pos()], {"O1": "Filled"}, {})                # 持倉資料還沒刷新、也沒帶成交口數
    assert "w" not in env.mm._watches and len(env.sent) == 1   # 不可重送（否則會反向開倉）


def test_partial_fill_retries_only_the_remaining_lots(env):
    w = env.option_watch(qty=2)
    env.check(w, [pos(2)])
    assert env.sent[0][1]["quantity"] == 2
    env.advance(1)
    env.check(w, [pos(1)], {"O1": "Cancelled"}, {"O1": 1})     # IOC 只成交 1 口、剩下取消
    assert w.remaining == 1 and len(env.sent) == 1
    env.advance(CLOSE_FILLED_GRACE + 1)                        # 有成交 → 等持倉刷新久一點再補平
    env.check(w, [pos(1)])
    assert len(env.sent) == 2 and env.sent[1][1]["quantity"] == 1


def test_waits_while_the_order_is_still_working_then_times_out(env):
    w = env.option_watch()
    env.check(w, [pos()])
    for dt in (1, 4, CLOSE_LIVE_TIMEOUT - 6):                  # 累計 1、5、(逾時前 1 秒)：仍在處理中，不重送
        env.advance(dt)
        env.check(w, [pos()], {"O1": "Submitted"}, {"O1": 0})
        assert len(env.sent) == 1
    env.advance(2)                                             # 超過逾時：IOC 不該處理這麼久 → 視為沒成交
    env.check(w, [pos()], {"O1": "Submitted"}, {"O1": 0})
    assert len(env.sent) == 2


def test_exception_while_sending_has_unknown_outcome_so_it_waits(env):
    w = env.option_watch()
    env.fail_next = 1
    env.check(w, [pos()])                                      # 送單逾時：單可能已送達券商
    assert env.sent == [] and w.close_attempts == 1 and w.close_order_id == ""
    env.advance(CLOSE_UNKNOWN_TIMEOUT - 5)
    env.check(w, [pos()])
    assert env.sent == []                                      # 結果不明期間不重送（以前每秒重送一次）
    env.advance(6)
    env.check(w, [pos()])
    assert len(env.sent) == 1


def test_unknown_order_status_times_out(env):
    w = env.option_watch()
    env.check(w, [pos()])
    env.advance(CLOSE_UNKNOWN_TIMEOUT - 1)
    env.check(w, [pos()])                                      # 查不到委託狀態
    assert len(env.sent) == 1
    env.advance(2)
    env.check(w, [pos()])
    assert len(env.sent) == 2


def test_gives_up_after_max_attempts_and_says_so(env, caplog):
    w = env.option_watch()
    w.close_attempts = MAX_CLOSE_ATTEMPTS
    with caplog.at_level(logging.ERROR):
        env.check(w, [pos()])
        env.check(w, [pos()])
    assert env.sent == [] and w.close_gave_up is True
    assert sum("不再自動重送" in r.message for r in caplog.records) == 1     # 只喊一次
    assert "w" in env.mm._watches                                           # 監看保留：人工平倉後仍會自動移除


def test_no_resend_once_price_is_back_inside_the_stop(env, monkeypatch):
    w = env.option_watch()
    env.check(w, [pos()])
    env.advance(1)
    env.check(w, [pos()], {"O1": "Cancelled"}, {"O1": 0})
    monkeypatch.setitem(quote_hub._last_price, CODE, 790.0)                 # 價格回到停損線內
    env.advance(10)
    env.check(w, [pos(last=790.0)])
    assert len(env.sent) == 1


def test_futures_stop_is_market_ioc_and_also_confirmed(env):
    w = ManualWatch(id="f", contract="TMF", direction=1, quantity=1, entry_price=100.0,
                    stop_loss_pts=10, take_profit_pts=0, match_code="TMF")
    env.mm._watches["f"] = w
    p = {"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 100.0, "last_price": 85.0}
    env.check(w, [p])
    kind, kw = env.sent[0]
    assert kind == "fut" and (kw["price_type"], kw["order_type"], kw["quantity"]) == ("MKT", "IOC", 1)
    assert "f" in env.mm._watches                              # 不再送出就移除
    env.advance(1)
    env.check(w, [p], {"F1": "Filled"}, {"F1": 1})
    assert "f" not in env.mm._watches


def test_normal_path_sends_exactly_one_order(env):
    w = env.option_watch()
    env.check(w, [pos()])
    env.advance(1)
    env.check(w, [])                                           # 下一輪持倉查詢已看不到部位
    assert len(env.sent) == 1 and "w" not in env.mm._watches


def test_order_status_is_polled_only_when_needed(env):
    w = env.option_watch()
    w.seen = False
    assert env.mm._needs_order_status() is True                # 開倉單還在等成交
    w.seen = True
    assert env.mm._needs_order_status() is False
    w.close_attempts = 1
    assert env.mm._needs_order_status() is True                # 平倉單送出後要確認
    status, fills = ManualOrderMonitor._index_trades(
        [{"id": "a", "status": "Filled", "deal_quantity": 2}, {"id": "", "status": "x"}, {"id": "b", "status": "Cancelled"}])
    assert status == {"a": "Filled", "b": "Cancelled"} and fills == {"a": 2, "b": 0}


def test_list_watches_exposes_close_progress(env):
    w = env.option_watch()
    env.check(w, [pos()])
    row = env.mm.list_watches()[0]
    assert row["close_attempts"] == 1 and row["close_gave_up"] is False
