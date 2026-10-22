"""「跌／漲 1% 之後 120 日內走到停利點」（發現 58）：命中的定義、視窗、偷看金絲雀、與發現 57 共用的統計、判定門檻、植入依賴會過關／無關不會、描述的數字。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core import dip_study as ds
from core import dip_tp_study as tp
from core.intraday_context import ols_hac
from test_dip_study import bdays

ROOT = Path(tp.__file__).resolve().parent.parent


def world_tp(n=6800, seed=0, vol=0.008):
    """有波動群聚的隨機漫步（跌日／漲日各佔 10～15%）；純隨機、沒有任何植入。"""
    rng = np.random.default_rng(seed)
    lv, r = 0.0, np.zeros(n)
    for t in range(1, n):
        lv = 0.97 * lv + rng.normal(0, 0.05)
        r[t] = vol * np.exp(lv) * rng.normal()
    return bdays(n), 5000.0 * np.exp(np.cumsum(r))


def planted(n=6800, seed=0, kind="dip", gap=25, lag=3):
    """低噪音的價格；每隔約 gap 天出現一個事件日（dip＝單日 −2%、rally＝+2%），事件後 lag 天出現一次 +6%（dip）或 −6%（rally）的走勢。搭配 H＝10（monkeypatch）時：事件日一定命中，其他日子只有事件前幾天的視窗才會命中。"""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.002, n)
    s = 10
    while s + lag + 2 < n:
        r[s] = -0.02 if kind == "dip" else 0.02
        r[s + lag] = 0.06 if kind == "dip" else -0.06
        s += gap + int(rng.integers(-3, 4))
    return bdays(n), 5000.0 * np.exp(np.cumsum(r))


# ── 命中的定義 ───────────────────────────────────────────────────

def test_call_hit_first_day_and_adverse_excursion():
    c = np.array([100.0, 98.0, 96.0, 101.0, 106.0, 90.0, 90.0])
    info = tp.hit_info(c, "dip", 0.05, h=4)
    assert info["hit"][0] == 100.0 and info["day"][0] == 4 and info["adverse"][0] == pytest.approx(-4.0)                          # 第 4 天收 106 ≥ 105；之前最低 96＝−4%
    assert info["hit"][1] == 100.0 and info["day"][1] == 3 and info["adverse"][1] == pytest.approx((96 / 98 - 1) * 100)           # 從 98 起：第 3 天 106（+8.2%）；之前最低 96
    assert info["hit"][2] == 100.0 and info["day"][2] == 1 and info["adverse"][2] == 0.0                                           # 從 96 起：第 1 天 101（+5.2%）就命中，沒有被套
    assert np.isnan(info["hit"][3:]).all() and np.isnan(info["day"][3:]).all()                                                    # 後面不夠 4 天


def test_put_hit_uses_the_minimum_and_adverse_is_the_max_rise():
    c = np.array([100.0, 103.0, 99.0, 94.0, 95.0, 120.0])
    info = tp.hit_info(c, "rally", 0.05, h=4)
    assert info["hit"][0] == 100.0 and info["day"][0] == 3 and info["adverse"][0] == pytest.approx(3.0)                           # 第 3 天 94 ≤ 95；之前先漲到 103＝+3%
    assert info["hit"][1] == 100.0 and info["day"][1] == 2 and info["adverse"][1] == 0.0                                           # 從 103 起：第 2 天 94 命中；之前一路在進場價之下，沒被套（不是負數）
    assert tp.hit_info(np.array([100.0, 106.0, 99.0, 94.0, 120.0]), "rally", 0.05, h=3)["adverse"][0] == pytest.approx(6.0)        # 先漲 6% 才跌到


def test_hit_boundary_inclusive_and_close_prices_only_and_no_hit():
    c = np.array([100.0, 104.99, 105.0, 100.0])
    assert tp.hit_info(c, "dip", 0.05, h=2)["hit"][0] == 100.0 and tp.hit_info(c, "dip", 0.05, h=2)["day"][0] == 2                  # 剛好等於 +5% 算命中
    assert tp.hit_info(np.array([100.0, 104.99, 104.0, 100.0]), "dip", 0.05, h=2)["hit"][0] == 0.0
    flat = np.array([100.0, 101.0, 99.0, 100.0, 100.0])
    f = tp.hit_info(flat, "dip", 0.05, h=3)
    assert f["hit"][0] == 0.0 and np.isnan(f["day"][0]) and np.isnan(f["adverse"][0])
    first = tp.hit_info(np.array([100.0, 106.0, 90.0]), "dip", 0.05, h=2)
    assert first["day"][0] == 1 and first["adverse"][0] == 0.0                                                                  # 第一天就命中：沒有被套


def test_info_never_uses_closes_beyond_the_window():
    dates, close = world_tp(n=800, seed=3)
    base = tp.hit_info(close, "dip", 0.05)
    t = next(i for i in range(100, 600) if base["hit"][i] == 0.0)                                  # 找一個沒命中的進場日
    tampered = close.copy()
    tampered[t + tp.H + 1:] *= 5.0                                                                  # 視窗以後全改掉：第 t 天不變
    after = tp.hit_info(tampered, "dip", 0.05)
    for k in ("hit", "day", "adverse"):
        assert np.array_equal(after[k][:t + 1], base[k][:t + 1], equal_nan=True)
    inside = close.copy()
    inside[t + tp.H] = close[t] * 1.5                                                               # 視窗最後一天（t+120）改掉：第 t 天會變成命中
    assert tp.hit_info(inside, "dip", 0.05)["hit"][t] == 100.0 and tp.hit_info(inside, "dip", 0.05)["day"][t] <= tp.H
    outside = close.copy()
    outside[t + tp.H + 1] = close[t] * 1.5                                                          # 視窗外一天：不變
    assert tp.hit_info(outside, "dip", 0.05)["hit"][t] == 0.0


# ── 檢定 ─────────────────────────────────────────────────────────

def test_one_test_matches_an_independent_calculation_and_expects_positive_gaps(monkeypatch):
    monkeypatch.setattr(tp, "H", 10)
    dates, close = planted(seed=11, kind="dip")
    t = tp.one_test(dates, close, "dip", 0.05)
    info = tp.hit_info(close, "dip", 0.05)
    ev = ds.event_mask(ds.daily_returns(close), "dip")
    m = ds.period_masks(dates, 10, ds.forward_returns(close, 10))["explore"]
    X = np.column_stack([np.ones(m.sum()), ev[m].astype(float)])
    b, se = ols_hac(X, info["hit"][m], 10)
    assert t["explore"]["gap"] == pytest.approx(b[1]) and t["explore"]["t"] == pytest.approx(b[1] / se[1]) and t["explore"]["rate_event"] == pytest.approx(info["hit"][m & ev].mean())
    assert t["explore"]["rate_other"] == pytest.approx(info["hit"][m & ~ev].mean())
    assert t["explore"]["rate_event"] == 100.0 and 5 < t["explore"]["rate_other"] < 20 and t["explore"]["gap"] > 75                    # 事件日一定命中；其他日子只有事件後的 1～2 天（反彈還沒發生、價位已經被打低）才命中（約 8～10%）
    brute = [any(close[k] / close[i] - 1 >= 0.05 for k in range(i + 1, i + 11)) for i in range(len(close) - 10)]
    assert np.array_equal(info["hit"][:len(brute)] == 100.0, np.array(brute))                                                         # 逐日暴力對照


def gt(**kw):
    base = {"explore": {"t": 3.0, "gap": 8.0}, "holdout": {"t": 2.0, "gap": 8.0}}
    for k, v in kw.items():
        sec, f = k.split("_")
        base[{"ex": "explore", "ho": "holdout"}[sec]][f] = v
    return base


def test_passes_thresholds_are_the_registered_ones():
    assert (tp.H, tp.MOVES, tp.MIN_GAP, tp.N_TESTS, tp.TRAP) == (120, (0.05, 0.10), 5.0, 4, 0.05)
    assert tp.passes(gt()) and not tp.passes(gt(ex_t=2.23)) and tp.passes(gt(ex_t=2.24)) and not tp.passes(gt(ho_t=1.64)) and tp.passes(gt(ho_t=1.645))
    assert not tp.passes(gt(ex_gap=4.99)) and tp.passes(gt(ex_gap=5.0)) and not tp.passes(gt(ho_gap=4.99)) and tp.passes(gt(ho_gap=5.0))
    assert not tp.passes({"explore": {"t": None, "gap": None}, "holdout": gt()["holdout"]})


def test_planted_effects_pass_in_the_right_direction(monkeypatch):
    monkeypatch.setattr(tp, "H", 10)
    r = tp.run(*planted(seed=11, kind="dip"))
    assert r["tests"]["dip/5%"]["pass"] and r["necessary_condition_holds"] is True and r["tests"]["dip/5%"]["explore"]["t"] > 5 and r["tests"]["dip/5%"]["holdout"]["t"] > 3
    assert not r["tests"]["rally/5%"]["pass"]                                                                                   # 沒有漲日的事件：Put 邏輯沒有東西可檢定
    r2 = tp.run(*planted(seed=12, kind="rally"))
    assert r2["tests"]["rally/5%"]["pass"] and r2["tests"]["rally/5%"]["explore"]["gap"] > 75 and not r2["tests"]["dip/5%"]["pass"]            # Put 邏輯：漲日之後更常跌到 −5%
    assert r2["tests"]["rally/10%"]["pass"] is False                                                                              # 走的是 −6%：到不了 −10%


def test_random_walks_with_volatility_clustering_never_pass():
    passed = 0
    for s in range(6):
        d0, c0 = world_tp(seed=300 + s)
        passed += sum(t["pass"] for t in tp.run(d0, c0)["tests"].values())
    assert passed == 0


def test_keys_and_verdicts():
    dates, close = world_tp(n=3000, seed=21)
    r = tp.run(dates, close)
    assert list(r["tests"]) == ["dip/5%", "dip/10%", "rally/5%", "rally/10%"] and set(r["verdict"].values()) <= {"過關", "沒過"} and r["h"] == 120
    assert all(set(d) >= {"rate_event", "rate_other", "full_gap", "ex_crisis_gap", "median_days_event", "trapped_event", "median_final_when_miss_event"} for d in r["descriptive"].values())


def test_describe_numbers_by_hand_and_directional_trap_flag():
    dates = bdays(300)
    c = np.full(300, 100.0)
    c[10] = 98.0                                                                                       # 跌日（−2%）
    c[11] = 100.0
    c[20] = 92.0                                                                                       # 命中停利前先被套 6%（從 98 起算 −6.1%）
    c[21:] = 111.0                                                                                     # 之後漲到 +10% 以上
    d = tp.describe(dates, c, "dip", 0.05)
    info = tp.hit_info(c, "dip", 0.05)
    ev = ds.event_mask(ds.daily_returns(c), "dip")
    ok = np.isfinite(info["hit"])
    assert d["n_events"] == int((ev & ok).sum()) and d["rate_event"] == pytest.approx(info["hit"][ev & ok].mean()) and d["rate_other"] == pytest.approx(info["hit"][~ev & ok].mean())
    hits = ev & ok & (info["hit"] == 100.0)
    assert d["median_days_event"] == pytest.approx(np.median(info["day"][hits])) and d["trapped_event"] == pytest.approx((info["adverse"][hits] <= -5).mean())
    assert d["trapped_event"] is not None and d["trapped_event"] > 0                                  # 事件日 10（進場 98）命中前最低 92＝−6.1% ≤ −5%
    p = np.full(300, 100.0)
    p[10] = 102.0                                                                                      # 漲日
    p[11] = 100.0
    p[20] = 106.0                                                                                      # 命中前先漲 6%
    p[21:] = 89.0
    dp = tp.describe(dates, p, "rally", 0.05)
    infop = tp.hit_info(p, "rally", 0.05)
    evp = ds.event_mask(ds.daily_returns(p), "rally")
    hp = evp & np.isfinite(infop["hit"]) & (infop["hit"] == 100.0)
    assert dp["trapped_event"] == pytest.approx((infop["adverse"][hp] >= 5).mean())                    # Put 邏輯：反向是上漲


def test_report_json_and_static(monkeypatch):
    monkeypatch.setattr(tp, "H", 10)
    dates, close = planted(n=6000, seed=41, kind="dip")
    r = tp.run(dates, close)
    json.dumps(r, default=float)
    text = tp.format_report(r)
    assert "dip/5%" in text and "rally/10%" in text and "先被反向套 5%" in text and "停利版的必要條件成立" in text and "拿掉危機年" in text
    r["necessary_condition_holds"] = False
    assert "停利版也不成立" in tp.format_report(r)
    monkeypatch.setattr(tp, "H", 120)
    short = tp.run(*world_tp(n=400, seed=42))
    assert set(short["verdict"].values()) == {"沒過"} and "—" in tp.format_report(short)
    for p in list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "range_suggest.py", "stop_suggest.py", "options_calc.py")] + [ROOT / "api" / n for n in ("routes_order.py", "routes_options.py")] + [ROOT / "main.py"]:
        assert "dip_tp_study" not in p.read_text(encoding="utf-8"), p


# ── 補強 ─────────────────────────────────────────────────────────

def test_hit_boundary_is_exactly_the_move_not_beyond():
    c = np.array([100.0, 149.99, 150.0, 100.0])
    assert tp.hit_info(c, "dip", 0.5, h=2)["hit"][0] == 100.0 and tp.hit_info(c, "dip", 0.5, h=2)["day"][0] == 2                     # 150/100 − 1 恰好 0.5 → 命中
    assert tp.hit_info(np.array([100.0, 149.99, 149.0, 100.0]), "dip", 0.5, h=2)["hit"][0] == 0.0
    p = np.array([100.0, 60.01, 60.0, 100.0])
    assert tp.hit_info(p, "rally", 0.4, h=2)["hit"][0] == 100.0 and tp.hit_info(np.array([100.0, 60.01, 60.5, 100.0]), "rally", 0.4, h=2)["hit"][0] == 0.0


def test_describe_matches_independent_calculations_for_medians_misses_and_crisis_exclusion():
    dates = bdays(1500)                                                                               # 1999-02 起約 6 年：含 2000～2002 的危機年
    rng = np.random.default_rng(7)
    r = rng.normal(0, 0.006, 1500)
    close = 1000.0 * np.exp(np.cumsum(r))
    for direction in ("dip", "rally"):
        d = tp.describe(dates, close, direction, 0.05)
        info = tp.hit_info(close, direction, 0.05)
        ev = ds.event_mask(ds.daily_returns(close), direction)
        ok = np.isfinite(info["hit"])
        fwd = ds.forward_returns(close, tp.H)
        hits = ev & ok & (info["hit"] == 100.0)
        miss = ev & ok & (info["hit"] == 0.0)
        assert np.mean(info["day"][hits]) != np.median(info["day"][hits])                              # 這組資料能分辨平均與中位數
        assert d["median_days_event"] == pytest.approx(np.median(info["day"][hits])) and d["median_final_when_miss_event"] == pytest.approx(np.median(fwd[miss]))
        assert d["median_final_when_miss_other"] == pytest.approx(np.median(fwd[~ev & ok & (info["hit"] == 0.0)]))
        yrs = np.array([int(x[:4]) for x in dates])
        keep = ok & ~np.isin(yrs, ds.CRISIS_YEARS)
        g, t = ds.dummy_excess(info["hit"][keep], ev[keep], tp.H)
        assert d["ex_crisis_gap"] == pytest.approx(g) and d["ex_crisis_t"] == pytest.approx(t)
        assert d["full_gap"] == pytest.approx(ds.dummy_excess(info["hit"][ok], ev[ok], tp.H)[0]) and abs(d["ex_crisis_gap"] - d["full_gap"]) > 1e-9


def test_put_trap_flag_needs_a_rise_of_at_least_5_percent_first():
    dates = bdays(300)
    p = np.full(300, 100.0)
    p[10] = 102.0
    p[11] = 100.0
    p[20] = 110.0                                                                                      # 從 102 起先漲 7.8% 才跌
    p[21:] = 89.0
    dp = tp.describe(dates, p, "rally", 0.05)
    infop = tp.hit_info(p, "rally", 0.05)
    evp = ds.event_mask(ds.daily_returns(p), "rally")
    hp = evp & np.isfinite(infop["hit"]) & (infop["hit"] == 100.0)
    assert dp["trapped_event"] == pytest.approx((infop["adverse"][hp] >= 5).mean()) and dp["trapped_event"] > 0
    c = np.full(300, 100.0)
    c[10] = 98.0
    c[20] = 92.0
    c[21:] = 111.0
    assert tp.describe(dates, c, "dip", 0.05)["trapped_event"] > 0 and tp.describe(dates, c, "rally", 0.05)["trapped_event"] is not None
