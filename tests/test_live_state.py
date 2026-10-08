"""盤中即時狀態（core/live_state.py）的單元測試。"""
from __future__ import annotations

import random
import time

import pytest

from core.live_state import FLOW_DOWN, FLOW_MARGIN, FLOW_UP, LiveState, flow_direction


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


# ── 買賣方向的遲滯（外盤占比在 60%/40% 附近不再閃爍）───────────────

def test_flow_direction_has_a_hysteresis_band():
    f = flow_direction                               # 門檻 0.60/0.40、遲滯帶 0.025
    assert (FLOW_UP, FLOW_DOWN, FLOW_MARGIN) == (0.60, 0.40, 0.025) and f(None) == 0
    assert (f(0.61, 0), f(0.625, 0), f(0.39, 0), f(0.375, 0)) == (0, 1, 0, -1)       # 中性：要超過門檻 2.5 個百分點才轉向
    assert (f(0.58, 1), f(0.575, 1), f(0.57, 1), f(0.37, 1)) == (1, 1, 0, -1)         # 已偏多：撐到 57.5%；跌破才解除；一口氣掉到 37.5% 以下直接轉空
    assert (f(0.42, -1), f(0.425, -1), f(0.43, -1), f(0.63, -1)) == (-1, -1, 0, 1)    # 已偏空：對稱


def test_empty_state_is_neutral_and_every_window_reports_a_direction():
    flow = LiveState().snapshot()["flow"]
    assert {k: v["dir"] for k, v in flow.items()} == {"20": 0, "100": 0, "300": 0}


def test_each_window_direction_follows_its_own_share():
    """各視窗的方向＝逐筆套用 flow_direction（視窗不足 n 筆時用手上全部）；用獨立的串列重算來對。"""
    rng, ls, t = random.Random(9), LiveState(), ts(10, 0)
    sides: list[int] = []
    ref = {20: 0, 100: 0, 300: 0}
    for i in range(450):
        side = 1 if rng.random() < 0.58 else 2
        trade(ls, side, 1, i + 1, t + i)
        sides.append(side)
        for n in ref:
            w = sides[-n:]
            ref[n] = flow_direction(sum(1 for s in w if s == 1) / len(w), ref[n])
        snap = ls.snapshot()["flow"]
        assert {n: snap[str(n)]["dir"] for n in ref} == ref


@pytest.mark.parametrize("win,ceiling", [("100", 0.5), ("20", 0.75)])
def test_direction_is_much_steadier_than_the_hard_threshold_on_a_noisy_series(win, ceiling):
    """外盤機率 60%（正好在門檻上）的雜訊序列：硬門檻一直翻，帶遲滯的方向翻得少很多。"""
    rng, ls, t = random.Random(5), LiveState(), ts(10, 0)
    hard, hyst = [], []
    for i in range(1500):
        trade(ls, 1 if rng.random() < 0.6 else 2, 1, i + 1, t + i)
        f = ls.snapshot()["flow"][win]
        hard.append(1 if f["share"] >= FLOW_UP else -1 if f["share"] <= FLOW_DOWN else 0)
        hyst.append(f["dir"])
    flips = lambda xs: sum(1 for a, b in zip(xs, xs[1:]) if a != b)  # noqa: E731
    assert flips(hard) > 40 and flips(hyst) < flips(hard) * ceiling
