"""盤中即時狀態（core/live_state.py）的單元測試。"""
from __future__ import annotations

import time

import pytest

from core.live_state import LiveState


def ts(h: int, m: int, s: int = 0, day: int = 7) -> float:
    return time.mktime((2026, 10, day, h, m, s, 0, 0, -1))


def trade(ls: LiveState, side: int, size: int, total: int, t: float, code: str = "TMFJ6", price: float = 100.0):
    ls.feed(code, price, size, total, side, t)


def test_counts_only_real_trades_and_dedups_by_total_volume():
    ls, t = LiveState(), ts(10, 0)
    trade(ls, 1, 1, 100, t)                        # 真實成交：外盤 1 口
    trade(ls, 1, 2, 102, t + 1)                    # 外盤 2 口
    trade(ls, 2, 1, 103, t + 2)                    # 內盤 1 口
    for i in range(10):                            # 報價更新（volume=0）帶著上一筆的 tick_type：不是成交，不計
        trade(ls, 2, 0, 103, t + 3 + i)
    trade(ls, 2, 1, 103, t + 20)                   # 重複回報同一筆（total_volume 沒增加）：不計
    trade(ls, 0, 1, 104, t + 21)                   # 成交但無法判斷內外盤：不計入比例
    f = ls.snapshot()["flow"]["100"]
    assert (f["n"], f["buy"], f["sell"]) == (3, 2, 1)
    assert f["share"] == pytest.approx(2 / 3, abs=1e-3)
    assert f["vol_share"] == pytest.approx(3 / 4, abs=1e-3)        # 外盤 1+2=3 口、內盤 1 口


def test_windows_and_volume_weighting():
    ls, t, total = LiveState(), ts(10, 0), 0
    for i in range(10):                            # 先 10 筆內盤（1 口）
        total += 1
        trade(ls, 2, 1, total, t + i)
    for i in range(20):                            # 再 20 筆外盤（2 口）
        total += 2
        trade(ls, 1, 2, total, t + 10 + i)
    flow = ls.snapshot()["flow"]
    assert flow["20"]["share"] == 1.0 and flow["20"]["n"] == 20            # 最近 20 筆全是外盤
    assert flow["100"]["n"] == 30 and flow["100"]["share"] == pytest.approx(20 / 30, abs=1e-3)
    assert flow["100"]["vol_share"] == pytest.approx(40 / 50, abs=1e-3)    # 外盤 40 口 vs 內盤 10 口
    assert flow["20"]["span_sec"] == 19                                    # 這個視窗涵蓋 19 秒


def test_window_keeps_only_the_latest_300_trades():
    ls, t = LiveState(), ts(10, 0)
    for i in range(350):
        trade(ls, 1 if i < 50 else 2, 1, i + 1, t + i)
    f = ls.snapshot()["flow"]["300"]
    assert f["n"] == 300 and f["share"] == 0.0                             # 前 50 筆外盤已被擠出


def test_session_range_only_counts_day_session_and_resets_each_day():
    ls = LiveState()
    for h, m, p in [(8, 44, 1000), (8, 45, 100), (10, 0, 120), (13, 45, 90), (13, 46, 500), (15, 30, 700)]:
        ls.feed("TMFJ6", p, 0, 0, 0, ts(h, m))                              # 日盤 08:45~13:45 以外都不計
    s = ls.snapshot()
    assert (s["high"], s["low"], s["range"]) == (120, 90, 30)
    assert s["session_day"] == "2026-10-07" and s["since"] == "08:45:00" and s["partial"] is False
    assert s["last"] == 700                                                 # 最新價不受時段限制
    ls.feed("TMFJ6", 200, 0, 0, 0, ts(8, 46, day=8))                        # 隔天第一筆日盤 tick → 重置
    s = ls.snapshot()
    assert (s["high"], s["low"], s["session_day"]) == (200, 200, "2026-10-08")


def test_late_start_is_flagged_partial():
    ls = LiveState()
    ls.feed("TMFJ6", 100, 0, 0, 0, ts(10, 32))                              # 重啟後 10:32 才收到第一筆
    s = ls.snapshot()
    assert s["partial"] is True and s["since"] == "10:32:00"


def test_other_contracts_and_zero_price_are_ignored():
    ls, t = LiveState(), ts(10, 0)
    trade(ls, 1, 5, 10, t, code="TXFJ6")
    trade(ls, 1, 5, 10, t, code="MXFJ6")
    trade(ls, 1, 5, 10, t, price=0.0)
    s = ls.snapshot()
    assert s["last"] is None and s["flow"]["100"]["n"] == 0 and s["range"] is None
    assert s["flow"]["100"]["share"] is None                               # 沒資料不是 0%


def test_total_volume_reset_counts_as_new_trade():
    ls, t = LiveState(), ts(10, 0)
    trade(ls, 1, 1, 100, t)
    trade(ls, 1, 1, 100, t + 1)                    # 同一個 total_volume → 重複
    trade(ls, 2, 1, 5, t + 2)                      # total_volume 變小（換盤/換月）→ 視為新成交
    assert ls.snapshot()["flow"]["100"]["n"] == 2


def test_missing_total_volume_still_counts_by_volume():
    ls, t = LiveState(), ts(10, 0)
    for i in range(3):
        trade(ls, 1, 1, 0, t + i)                  # 沒有 total_volume 欄位時，只用 volume>0 判定
    assert ls.snapshot()["flow"]["100"]["n"] == 3
