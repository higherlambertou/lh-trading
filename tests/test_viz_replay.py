"""core/viz_replay.py：市場指標視覺化的歷史回放與統計驗證。

重點：① 視覺編碼連續、不在門檻上突變 ② 「協調」的定義與盤前判斷同一套 ③ 不偷看未來
④ 驗證流程本身可信——真的有效應時測得出來（正向對照）、純隨機時不會亂報（負向對照）。"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

import core.viz_replay as vr
from core.market_store import MarketStore


# ── 視覺編碼 ─────────────────────────────────────────────────────

def test_neutral_band_is_gray_and_color_fades_in_continuously_past_it():
    gray = vr.hurst_color(0.52, 80)
    assert gray["kind"] == "neutral" and gray["sat"] == 0 and gray["css"] == "hsl(0, 0%, 46%)"
    just_in, just_out = vr.hurst_color(0.5499, 80), vr.hurst_color(0.5501, 80)
    assert abs(just_out["sat"] - just_in["sat"]) < 0.02                      # 門檻兩側幾乎同色：不會在 0.55 上忽冷忽熱
    assert vr.hurst_color(0.62, 80)["strength"] == 1.0 and vr.hurst_color(0.9, 80)["strength"] == 1.0   # 到 full_at 全色，不再變


def test_warm_for_trend_cool_for_revert_and_hue_moves_with_strength():
    t1, t2 = vr.hurst_color(0.57, 100), vr.hurst_color(0.60, 100)
    assert t1["kind"] == t2["kind"] == "trend" and 0 <= t2["hue"] < t1["hue"] <= 40          # 越強越紅
    r1, r2 = vr.hurst_color(0.43, 100), vr.hurst_color(0.40, 100)
    assert r1["kind"] == r2["kind"] == "revert" and 170 <= r1["hue"] < r2["hue"] <= 220      # 越強越藍
    assert vr.hurst_color(None, 50)["kind"] == "none"


def test_iv_percentile_drives_saturation_and_missing_iv_is_muted():
    low, high = vr.hurst_color(0.60, 10), vr.hurst_color(0.60, 95)
    assert high["sat"] > low["sat"] and high["iv_known"] and low["iv_known"]
    none = vr.hurst_color(0.60, None)
    assert none["iv_known"] is False and none["sat"] == pytest.approx(0.25)                 # 沒有 IV：固定淡色，不假裝知道
    custom = vr.hurst_color(0.58, 50, neutral_band=0.02, full_at=0.06)
    assert custom["strength"] == 1.0                                                          # 可調：中性帶與全色點


# ── 預期方向（與 scalp market_bias=2 同一套）──────────────────────

@pytest.mark.parametrize("hs,direction,iv,want", [
    ("TREND", 1, None, 1), ("TREND", -1, None, -1),         # 趨勢：順日 K 方向
    ("REVERT", 1, None, -1), ("REVERT", -1, None, 1),       # 均值回歸：逆日 K 方向
    ("RANDOM", 1, None, 0), ("UNCERTAIN", 1, None, 0), (None, 1, None, 0),
    ("TREND", 0, None, 0),                                  # 日 K 貼近均線：沒有方向可偏
    ("TREND", 1, "LOW", 0), ("REVERT", 1, "HIGH", 0),        # IV 偏低／偏高優先，期貨不偏向
    ("TREND", 1, "NORMAL", 1),
])
def test_want_direction_matches_the_production_rule(hs, direction, iv, want):
    assert vr.want_direction(hs, direction, iv) == want


# ── 回放：資料準備 ───────────────────────────────────────────────

def base_ts(month: int, day: int) -> int:
    return int(time.mktime((2026, month, day, 8, 45, 0, 0, 0, -1)))


def seed_day(store, month, day, *, flow, price, minutes=300, skip=()):
    """flow(m) → (buy_n, sell_n)；price(m) → (open, close)。m＝日盤第幾分鐘。"""
    rows = []
    for m in range(minutes):
        if m in skip:
            continue
        b, s = flow(m)
        o, c = price(m)
        rows.append({"ts": base_ts(month, day) + 60 * m, "buy_n": b, "sell_n": s, "unk_n": 0, "buy_vol": b, "sell_vol": s,
                     "unk_vol": 0, "open": o, "high": max(o, c), "low": min(o, c), "close": c})
    store.upsert_flow_1m("TMF", rows)


def seed_ind(store, date, **kw):
    store.upsert_indicator_daily([{"date": date, "hurst": kw.get("hurst", 0.6), "hurst_z": 1.0, "hurst_state": kw.get("state", "TREND"),
                                   "direction": kw.get("direction", 1), "iv": None, "iv_pct": kw.get("iv_pct"), "iv_state": None}])


def balanced(m):
    return 75, 75


def phased(m):
    """0~59 分鐘多空均衡；60~119 買方猛攻（80%）；120~179 賣方猛攻（80%）；之後均衡。"""
    if 60 <= m < 120:
        return 120, 30
    if 120 <= m < 180:
        return 30, 120
    return 75, 75


def rising(m):
    return 100.0 + m, 100.0 + m + 0.5


def test_replay_cells_follow_the_live_panel_state_machine_and_define_coherence(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-06", state="TREND", direction=1)
    seed_day(store, 10, 7, flow=phased, price=rising)
    rep = vr.build_replay(store, days=5, block_min=15)
    day = rep["days"][0]
    assert day["date"] == "2026-10-07" and day["asof"] == "2026-10-06" and day["want"] == 1
    assert len(day["cells"]) == 20 and day["cells"][0]["start"] == "08:45" and day["cells"][1]["start"] == "09:00"
    dirs = [c["dir"] for c in day["cells"]]
    assert dirs[:4] == [0, 0, 0, 0]                                   # 前 60 分鐘均衡 → 中性
    assert dirs[4:8] == [1, 1, 1, 1]                                  # 買方猛攻（80% 外盤）→ 買方主動（遲滯也要過 62.5%）
    assert dirs[8:12] == [-1, -1, -1, -1]                             # 賣方猛攻 → 賣方主動
    coh = [c["coherence"] for c in day["cells"]]
    assert coh[4:8] == [1, 1, 1, 1] and coh[8:12] == [-1, -1, -1, -1] and coh[0] == 0     # want=+1：買方主動＝協調，賣方主動＝矛盾
    assert all(c["shape"] == {1: "up", -1: "down", 0: "flat"}[c["dir"]] for c in day["cells"])
    assert day["cells"][4]["up_min"] > 0 and day["cells"][8]["down_min"] > 0


def test_forward_move_starts_at_the_first_trade_after_the_signal(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-06")
    seed_day(store, 10, 7, flow=balanced, price=rising)
    cells = vr.build_replay(store, days=1, block_min=15)["days"][0]["cells"]
    # 訊號在第 b 格最後一分鐘 e＝15(b+1)-1；起點＝e+1 分鐘的開盤價 100+e+1，終點＝下一格最後一分鐘收盤價 100+e+15+0.5 → 差 14.5
    assert [c["fwd"] for c in cells[:-1]] == [14.5] * 19 and cells[-1]["fwd"] is None


def test_asof_never_uses_the_same_day_or_the_future(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-06", state="TREND", direction=1, hurst=0.60)       # 盤前就知道的
    seed_ind(store, "2026-10-07", state="REVERT", direction=1, hurst=0.40)      # 當天收盤才知道的：不能用
    seed_ind(store, "2026-10-08", state="REVERT", direction=-1, hurst=0.40)
    seed_day(store, 10, 7, flow=balanced, price=rising)
    day = vr.build_replay(store, days=3)["days"][0]
    assert day["asof"] == "2026-10-06" and day["hurst"] == 0.6 and day["want"] == 1
    store2 = MarketStore(tmp_path / "m2.db")                                    # 沒有更早的列 → 沒有判斷，不亂猜
    seed_day(store2, 10, 7, flow=balanced, price=rising)
    d2 = vr.build_replay(store2, days=3)["days"][0]
    assert d2["asof"] is None and d2["want"] == 0 and d2["color"]["kind"] == "none"
    assert all(c["coherence"] is None for c in d2["cells"])


def test_changing_the_future_never_changes_an_earlier_cell(tmp_path):
    def build(path, tail_flow, tail_price):
        store = MarketStore(path)
        seed_ind(store, "2026-10-06")
        seed_day(store, 10, 7, flow=lambda m: phased(m) if m < 120 else tail_flow(m),
                 price=lambda m: rising(m) if m < 120 else tail_price(m))
        return vr.build_replay(store, days=1)["days"][0]["cells"]

    a = build(tmp_path / "a.db", phased, rising)
    b = build(tmp_path / "b.db", lambda m: (5, 140), lambda m: (50.0, 40.0))         # 120 分鐘之後整個換掉
    for ca, cb in zip(a, b):
        end = (ca["i"] + 1) * 15 - 1
        if end < 120:
            assert (ca["dir"], ca["share"], ca["coherence"]) == (cb["dir"], cb["share"], cb["coherence"])   # 訊號只用到當時的資料
        if (ca["i"] + 2) * 15 - 1 < 120:
            assert ca["fwd"] == cb["fwd"]                                                                  # 預期報酬所涵蓋的期間都還在 120 之前


def test_incomplete_days_are_excluded_and_only_the_latest_are_kept(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-01")
    seed_day(store, 10, 5, flow=balanced, price=rising, minutes=150)                       # 只有半天 → 不算
    for d in (6, 7, 8):
        seed_day(store, 10, d, flow=balanced, price=rising)
    seed_day(store, 10, 9, flow=balanced, price=rising, skip=range(100, 300))              # 只有 100 分鐘有成交 → 不算
    rep = vr.build_replay(store, days=2)
    assert [d["date"] for d in rep["days"]] == ["2026-10-07", "2026-10-08"] and rep["coverage"]["days"] == 2


def test_replay_is_json_safe(tmp_path):
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-06")
    seed_day(store, 10, 7, flow=lambda m: (0, 0) if m % 7 == 0 else (60, 40), price=rising)
    json.dumps(vr.build_replay(store, days=1), allow_nan=False)
    json.dumps(vr.validate(vr.build_replay(store, days=1)), allow_nan=False)


# ── 統計驗證：對照組 ─────────────────────────────────────────────

def synth(n_days, coherent_effect, seed, n_cells=19, p_dir=(0.3, 0.3, 0.4), noise=8.0):
    """直接組出 replay 結構：每天一個 want，每格隨機的力道方向；協調格的預期報酬多加 coherent_effect 點。"""
    rng = np.random.default_rng(seed)
    days = []
    for d in range(n_days):
        want = int(rng.choice([-1, 1]))
        cells = []
        for b in range(n_cells + 1):
            dir_ = int(rng.choice([1, -1, 0], p=p_dir))
            s = float(rng.normal(0, noise)) + (coherent_effect if dir_ == want else 0.0)
            cells.append({"i": b, "dir": dir_, "share": 0.5, "coherence": 0 if dir_ == 0 else (1 if dir_ == want else -1),
                          "fwd": (s * want) if b < n_cells else None})
        days.append({"date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}", "want": want, "move": float(rng.normal(0, 50)), "cells": cells})
    return {"days": days}


def test_positive_control_a_real_effect_is_detected():
    res = vr.validate(synth(40, coherent_effect=6.0, seed=1), n_perm=800)
    assert res["verdict"]["level"] == "significant" and res["diff"] > 3 and res["p_value"] < 0.01
    assert res["coherent"]["hit_rate"] > res["contradictory"]["hit_rate"]
    assert res["cusum"]["end_outside"] is True and res["cusum"]["observed"][-1] > res["cusum"]["hi"][-1]


def test_negative_control_pure_noise_is_not_called_significant():
    levels = [vr.validate(synth(40, coherent_effect=0.0, seed=s), n_perm=400)["verdict"]["level"] for s in range(12)]
    assert levels.count("significant") + levels.count("wrong_way") <= 2          # 5% 顯著水準下 12 組裡幾乎不會有（容許 2 組）
    assert "none" in levels


def test_a_real_effect_in_the_wrong_direction_is_reported_as_such():
    res = vr.validate(synth(40, coherent_effect=-6.0, seed=3), n_perm=800)
    assert res["verdict"]["level"] == "wrong_way" and res["diff"] < 0 and res["p_value"] < 0.05


def test_too_few_cells_is_insufficient_not_a_verdict():
    res = vr.validate(synth(2, coherent_effect=9.0, seed=4), n_perm=200)
    assert res["verdict"]["level"] == "insufficient" and "樣本不足" in res["verdict"]["text"]
    empty = vr.validate({"days": [{"date": "x", "want": 0, "move": 1.0, "cells": []}]}, n_perm=100)
    assert empty["verdict"]["level"] == "insufficient" and empty["eligible_cells"] == 0


def test_permutation_is_within_day_so_a_trending_day_alone_cannot_fake_an_effect():
    """每天的走勢都很強、且方向正好等於 want（want 猜對了），但力道方向在天內是隨機的 → 協調跟矛盾沒有差別，不該顯著。"""
    rng = np.random.default_rng(11)
    days = []
    for d in range(40):
        want = 1 if d % 2 else -1
        cells = []
        for b in range(20):
            dir_ = int(rng.choice([1, -1, 0]))
            cells.append({"i": b, "dir": dir_, "share": 0.5, "coherence": 0 if dir_ == 0 else (1 if dir_ == want else -1),
                          "fwd": (8.0 + float(rng.normal(0, 3))) * want if b < 19 else None})        # 整天都往 want 的方向走
        days.append({"date": f"d{d}", "want": want, "move": 150.0 * want, "cells": cells})
    res = vr.validate({"days": days}, n_perm=600)
    assert res["verdict"]["level"] == "none" and res["p_value"] > 0.05
    assert res["day_level"]["hit_rate"] == 1.0 and res["day_level"]["p_value"] <= 0.05          # 但「want 猜對當天漲跌」這件事本身是顯著的


def test_validation_is_reproducible_and_json_safe():
    rep = synth(30, coherent_effect=2.0, seed=5)
    a, b = vr.validate(rep, n_perm=300, seed=9), vr.validate(rep, n_perm=300, seed=9)
    assert a == b
    json.dumps(a, allow_nan=False)


# ── API ──────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from api import routes_market
    store = MarketStore(tmp_path / "m.db")
    seed_ind(store, "2026-10-05", state="TREND", direction=1)
    for d in (6, 7, 8):
        seed_day(store, 10, d, flow=phased, price=rising)
    monkeypatch.setattr(routes_market, "market_state", SimpleNamespace(store=store))
    return TestClient(main.app)


def test_api_returns_the_grid_and_the_validation(client):
    r = client.get("/api/market/replay?days=5&perms=200").json()
    assert r["coverage"]["days"] == 3 and len(r["blocks"]) == 20 and r["params"]["block_min"] == 15
    day = r["days"][-1]
    assert {"date", "asof", "hurst", "want", "color", "cells", "move"} <= set(day)
    assert {"start", "dir", "shape", "coherence", "fwd", "share", "up_min", "down_min"} <= set(day["cells"][0])
    v = r["validation"]
    assert v["verdict"]["level"] in ("insufficient", "none", "significant", "wrong_way")
    assert {"coherent", "contradictory", "neutral", "diff", "p_value", "cusum", "day_level"} <= set(v)


def test_api_can_skip_the_validation_and_takes_the_mapping_parameters(client):
    r = client.get("/api/market/replay?days=5&validate=false&neutral_band=0.02&full_at=0.06&block_min=30").json()
    assert "validation" not in r and len(r["blocks"]) == 10
    assert r["params"]["neutral_band"] == 0.02 and r["params"]["full_at"] == 0.06


@pytest.mark.parametrize("query", ["days=1", "days=999", "block_min=1", "flow_up=0.4", "neutral_band=0.2&full_at=0.1",
                                    "flow_down=0.7&flow_up=0.6", "perms=10"])
def test_api_rejects_nonsense_parameters(client, query):
    assert client.get(f"/api/market/replay?{query}").status_code == 422
