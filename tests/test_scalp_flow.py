"""scalp 的外/內盤訊號來源（flow_source）。

flow_source=0（預設）必須與舊算法逐事件完全相同（行為不變）；
flow_source=1 只算 TMF 真實成交：排除報價更新（volume=0）、重複回報、MXF/TXF 的事件，
且只在「新成交」那一刻判斷訊號。"""
from __future__ import annotations

import random
from collections import deque

from strategies.scalp import ScalpStrategy


def ev(code="TMFJ6", vol=1, total=1, tt=1) -> dict:
    return {"code": code, "volume": vol, "total_volume": total, "tick_type": tt, "close": 100.0}


def legacy_signal(buf: deque, window: int, thr: float, quote: dict) -> int:
    """舊版 ScalpStrategy._get_signal（momentum 模式）的逐字參考實作。"""
    tt = int(quote.get("tick_type", 0))
    if tt in (1, 2):
        buf.append(tt)
    if len(buf) < window:
        return 0
    buys = sum(1 for t in buf if t == 1)
    sells = sum(1 for t in buf if t == 2)
    total = buys + sells
    if total == 0:
        return 0
    if buys / total >= thr:
        return 1
    if sells / total >= thr:
        return -1
    return 0


def test_default_is_unchanged_from_the_legacy_algorithm():
    s = ScalpStrategy()
    s._apply_params({})                                          # 與實際啟動時一樣建立視窗
    assert s.flow_source == 0 and s.params["flow_source"] == 0
    ref, rng, total = deque(maxlen=s.momentum_window), random.Random(1), 0
    for _ in range(800):
        code = rng.choice(["TMFJ6", "MXFJ6", "TXFJ6"])
        vol = rng.choice([0, 0, 0, 1, 2])
        total += 1 if vol else 0
        q = ev(code, vol, total, rng.choice([0, 1, 1, 2, 2]))
        assert s._get_signal(q) == legacy_signal(ref, s.momentum_window, s.momentum_threshold, q)


def test_real_trades_mode_ignores_quote_updates_other_contracts_and_duplicates():
    s = ScalpStrategy()
    s._apply_params({"flow_source": 1})
    for _ in range(30):                                          # 報價更新（volume=0）帶著上一筆的方向：不是成交
        assert s._get_signal(ev(vol=0, total=0, tt=1)) == 0
    for i in range(30):                                          # MXF / TXF 的成交：不是 TMF
        assert s._get_signal(ev("TXFJ6" if i % 2 else "MXFJ6", 1, i + 1, 1)) == 0
    assert len(s._tick_buf) == 0

    total = 0
    for _ in range(19):                                          # 19 筆 TMF 外盤成交：視窗還沒滿
        total += 1
        assert s._get_signal(ev(vol=1, total=total, tt=1)) == 0
    total += 1
    assert s._get_signal(ev(vol=1, total=total, tt=1)) == 1      # 第 20 筆 → 外盤 100% → 做多
    assert len(s._tick_buf) == 20

    assert s._get_signal(ev(vol=1, total=total, tt=1)) == 0      # 重複回報同一筆（total_volume 沒增加）：不計
    assert s._get_signal(ev(vol=0, total=total, tt=1)) == 0      # 之後的報價更新也不會重複觸發同一個訊號
    assert len(s._tick_buf) == 20


def test_real_trades_mode_threshold_counts_trades_only():
    s = ScalpStrategy()
    s._apply_params({"flow_source": 1})
    out, total = [], 0
    for tt in [1] * 13 + [2] * 7:                                # 13 買 7 賣 = 0.65 → 剛好達門檻
        total += 1
        out.append(s._get_signal(ev(vol=1, total=total, tt=tt)))
    assert out[:19] == [0] * 19 and out[19] == 1
    s2 = ScalpStrategy()
    s2._apply_params({"flow_source": 1})
    out, total = [], 0
    for tt in [2] * 13 + [1] * 7:                                # 反過來 → 做空
        total += 1
        out.append(s2._get_signal(ev(vol=1, total=total, tt=tt)))
    assert out[19] == -1


def test_same_events_give_different_signals_in_the_two_modes():
    """報價更新灌水：舊算法在「只有 1 筆真實成交」時就能湊滿 20 筆視窗並觸發，新模式不會。"""
    old, new = ScalpStrategy(), ScalpStrategy()
    old._apply_params({})
    new._apply_params({"flow_source": 1})
    events = [ev(vol=1, total=1, tt=1)] + [ev(vol=0, total=1, tt=1) for _ in range(25)]
    assert [old._get_signal(q) for q in events].count(1) > 0
    assert [new._get_signal(q) for q in events].count(1) == 0


def test_params_roundtrip_and_validation():
    s = ScalpStrategy()
    assert "flow_source" in [p["key"] for p in s.param_schema]
    s._apply_params({"flow_source": 1})
    assert s.flow_source == 1 and s.params["flow_source"] == 1
    s._apply_params({"flow_source": 5})                          # 非法值 → 0（維持舊算法）
    assert s.flow_source == 0
    s._apply_params({})                                          # 沒帶參數 → 沿用目前值（與其他參數一致）
    assert s.flow_source == 0
