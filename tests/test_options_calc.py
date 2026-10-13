"""選擇權買方試算（core/options_calc.py）：Greeks 對照數值微分、損益兩平對照逐點掃描、情境表對照直接重算、
機率對照蒙地卡羅、平倉試算對照需求文件的手算例子。

模擬鏈用 Black-76 單一 IV 造（沒有價差時中價就是理論價），所以反推出來的遠期價與 IV 要等於當初給的。
"""
from __future__ import annotations

import json
import math
import random

import pytest

import core.options_calc as oc
from core.iv_monitor import black76

R = 0.015


def flat_chain(F=49283.0, iv=20.3, days=8.78, spread_pct=0.0, min_spread=0.0, step=50.0, hint=None):
    T = days / 365.0
    q = oc.model_quotes(F, iv, days, step=step, spread_pct=spread_pct, min_spread=min_spread)
    return oc.build_chain(q, T, hint=hint), q


# ── Greeks ────────────────────────────────────────────────────────

@pytest.mark.parametrize("K", [90.0, 100.0, 112.0])
@pytest.mark.parametrize("T", [0.02, 0.25])
@pytest.mark.parametrize("is_call", [True, False])
def test_greeks_match_finite_differences(K, T, is_call):
    F, s = 100.0, 0.2
    px = lambda f=F, t=T, sg=s: black76(f, K, t, sg, R, is_call)      # noqa: E731
    g = oc.greeks(F, K, T, s, R, is_call)
    h = 1e-3
    assert g["delta"] == pytest.approx((px(f=F + h) - px(f=F - h)) / (2 * h), rel=1e-5, abs=1e-8)
    assert g["gamma"] == pytest.approx((px(f=F + h) - 2 * px() + px(f=F - h)) / h ** 2, rel=1e-3, abs=1e-7)
    assert g["vega"] == pytest.approx((px(sg=s + 1e-5) - px(sg=s - 1e-5)) / 2e-5, rel=1e-5)
    dt = 1e-6
    assert g["theta"] == pytest.approx(-(px(t=T + dt) - px(t=T - dt)) / (2 * dt), rel=1e-4)       # theta＝時間過去時的價值變化（T 變小）


def test_put_call_relations():
    F, K, T, s = 100.0, 105.0, 0.1, 0.25
    c, p = black76(F, K, T, s, R, True), black76(F, K, T, s, R, False)
    assert c - p == pytest.approx(math.exp(-R * T) * (F - K), abs=1e-9)
    gc, gp = oc.greeks(F, K, T, s, R, True), oc.greeks(F, K, T, s, R, False)
    assert gc["delta"] - gp["delta"] == pytest.approx(math.exp(-R * T), abs=1e-12)
    assert gc["gamma"] == pytest.approx(gp["gamma"]) and gc["vega"] == pytest.approx(gp["vega"])
    assert gc["theta"] < 0 and gp["theta"] < 0 and gc["vega"] > 0 and gc["gamma"] > 0


def test_greeks_at_expiry_are_intrinsic():
    assert oc.greeks(100, 90, 0.0, 0.2, R, True)["delta"] == 1.0 and oc.greeks(100, 110, 0.0, 0.2, R, True)["delta"] == 0.0
    assert oc.greeks(100, 110, 0.0, 0.2, R, False)["delta"] == -1.0 and oc.greeks(100, 90, 0.0, 0.2, R, False)["delta"] == 0.0
    assert oc.greeks(100, 100, 0.1, 0.0, R, True)["theta"] == 0.0


def test_expected_move_matches_the_straddle_price():
    F, s, T = 49283.0, 0.203, 8.78 / 365
    em = oc.expected_move(F, s, T)
    straddle = black76(F, F, T, s, R, True) + black76(F, F, T, s, R, False)
    assert em == pytest.approx(F * s * math.sqrt(T)) and straddle / em == pytest.approx(math.sqrt(2 / math.pi), abs=0.005)
    assert oc.expected_move(F, 0.0, T) == 0.0 and oc.expected_move(F, s, 0.0) == 0.0


# ── 選擇權鏈 ──────────────────────────────────────────────────────

def test_chain_recovers_forward_and_iv_from_a_flat_model():
    ch, _ = flat_chain()
    assert ch["F"] == pytest.approx(49283.0, abs=0.5) and ch["atm_iv"] == pytest.approx(0.203, abs=5e-4)
    ivs = [s["iv"] for row in ch["rows"] if abs(row["strike"] - 49283) < 3000 for s in (row["call"], row["put"]) if s]
    assert all(abs(x - 0.203) < 2e-3 for x in ivs)
    assert [r["strike"] for r in ch["rows"]] == sorted(r["strike"] for r in ch["rows"])


def test_a_given_forward_is_used_as_is_and_the_hint_only_narrows_the_search():
    q = oc.model_quotes(100.0, 20.0, 30.0, step=1.0)
    T = 30 / 365
    assert oc.build_chain(q, T, F=101.0)["F"] == 101.0
    bad = [dict(x) for x in q]
    for x in bad:                                                       # 遠離平值的幾檔報價壞掉（買賣權價差亂給）：平價反推不能被帶偏
        if x["strike"] in (60.0, 61.0) and x["right"] == "C":
            x["bid"], x["ask"] = 1.0, 1.2
    assert oc.build_chain(bad, T, hint=100.0)["F"] == pytest.approx(100.0, abs=0.2)


def test_chain_errors():
    with pytest.raises(ValueError, match="已到期"):
        oc.build_chain([{"strike": 100, "right": "C", "bid": 1, "ask": 2}], 0.0)
    with pytest.raises(ValueError, match="沒有任何報價"):
        oc.build_chain([], 0.1)
    one_side = [{"strike": 100, "right": "C", "bid": 1, "ask": 2}, {"strike": 105, "right": "C", "bid": 0.5, "ask": 1}]
    with pytest.raises(ValueError, match="反推不出遠期價"):
        oc.build_chain(one_side, 0.1)
    assert oc.build_chain(one_side, 0.1, hint=102.0)["F"] == 102.0        # 給了提示就用提示


def test_mid_price_prefers_the_mid_and_distrusts_wide_spreads():
    assert oc.mid_price(100, 110) == 105
    assert oc.mid_price(50, 150, last=90) == 90                          # 價差超過中價 30%：退回最近成交價
    assert oc.mid_price(0, 110, last=95) == 95 and oc.mid_price(0, 0, 0) == 0.0


def test_insane_iv_is_dropped():
    q = [{"strike": 100, "right": "C", "bid": 5.0, "ask": 5.2}, {"strike": 100, "right": "P", "bid": 5.0, "ask": 5.2},
         {"strike": 130, "right": "C", "bid": 0.0, "ask": 0.1, "close": 28.0}]   # 這一檔的最近成交價高於內含值很多：IV 離譜
    ch = oc.build_chain(q, 30 / 365)
    far = next(r for r in ch["rows"] if r["strike"] == 130)["call"]
    assert far["iv"] is None and far["delta"] is None


# ── 選履約價 ──────────────────────────────────────────────────────

def test_pick_atm_requires_both_sides_to_be_buyable():
    ch, q = flat_chain(step=50.0)
    k0 = oc.pick_atm(ch)
    assert abs(k0 - ch["F"]) <= 25
    q2 = [dict(x) for x in q]
    for x in q2:
        if x["strike"] == k0 and x["right"] == "P":
            x["ask"] = 0.0                                              # 最靠近的那檔賣權沒有賣價
    ch2 = oc.build_chain(q2, 8.78 / 365, F=ch["F"])
    k1 = oc.pick_atm(ch2)
    assert k1 != k0 and abs(k1 - ch["F"]) <= 75


def test_delta_selection_equals_brute_force():
    ch, _ = flat_chain()
    for right, sign in (("C", 1), ("P", -1)):
        c = oc.pick_by_delta(ch, right, 0.25)
        side = lambda row: row["call"] if right == "C" else row["put"]          # noqa: E731
        pool = [(row["strike"], side(row)) for row in ch["rows"]               # 能買（有賣價）而且反推得出 IV 的價外檔
                if side(row) and side(row)["ask"] > 0 and side(row)["delta"] is not None
                and (row["strike"] > ch["F"] if right == "C" else row["strike"] < ch["F"])]
        best = min(pool, key=lambda x: abs(abs(x[1]["delta"]) - 0.25))
        assert c["strike"] == best[0] and sign * c["delta"] > 0 and abs(abs(c["delta"]) - 0.25) < 0.02


def test_move_selection_snaps_to_the_nearest_buyable_strike():
    ch, q = flat_chain()
    c = oc.pick_by_move(ch, "C", 800.0)
    assert c["strike"] == min((r["strike"] for r in ch["rows"]), key=lambda k: abs(k - (ch["F"] + 800)))
    p = oc.pick_by_move(ch, "P", 800.0)
    assert abs(p["strike"] - (ch["F"] - 800)) <= 25
    q2 = [dict(x) for x in q]
    for x in q2:
        if x["right"] == "C" and abs(x["strike"] - c["strike"]) < 1:
            x["ask"] = 0.0                                              # 那一檔沒有賣價 → 換相鄰的
    c2 = oc.pick_by_move(oc.build_chain(q2, 8.78 / 365, F=ch["F"]), "C", 800.0)
    assert c2["strike"] != c["strike"] and abs(c2["strike"] - c["strike"]) <= 50


def test_select_legs_by_kind():
    ch, _ = flat_chain()
    sd = oc.select_legs(ch, "straddle")
    assert [l["right"] for l in sd] == ["C", "P"] and sd[0]["strike"] == sd[1]["strike"] and all(l["qty"] == 1 for l in sd)
    st = oc.select_legs(ch, "strangle", select="delta", delta=0.25)
    assert st[0]["right"] == "C" and st[1]["right"] == "P" and st[0]["strike"] > ch["F"] > st[1]["strike"]
    sm = oc.select_legs(ch, "strangle", select="move", k=0.5)
    em = oc.expected_move(ch["F"], ch["atm_iv"], ch["T"])
    assert abs(sm[0]["strike"] - (ch["F"] + 0.5 * em)) <= 25 and abs(sm[1]["strike"] - (ch["F"] - 0.5 * em)) <= 25
    c = oc.select_legs(ch, "call", select="delta", delta=0.6)                  # 單向可以選價內
    assert len(c) == 1 and c[0]["right"] == "C" and c[0]["strike"] < ch["F"] and abs(c[0]["delta"] - 0.6) < 0.03
    p = oc.select_legs(ch, "put", select="move", k=-0.2)                        # k<0＝價內
    assert p[0]["right"] == "P" and p[0]["strike"] > ch["F"]
    custom = oc.select_legs(ch, "strangle", select="move", k=1.0, ref_move=500.0)    # 自己填預期波動：距離用你的數字
    assert abs(custom[0]["strike"] - (ch["F"] + 500)) <= 25


def test_strangle_wings_stay_out_of_the_money_even_for_an_odd_delta():
    """勒式兩翼的 Delta 填到 0.7（價內才有那麼大）：還是要選價外那一側最接近的，不能變成「買權在下、賣權在上」。"""
    ch, _ = flat_chain()
    st = oc.select_legs(ch, "strangle", select="delta", delta=0.7)
    assert st[0]["strike"] > ch["F"] > st[1]["strike"] and abs(abs(st[0]["delta"]) - 0.5) < 0.1


def test_select_legs_entry_price_is_the_ask_or_the_mid():
    ch, _ = flat_chain(spread_pct=0.04, min_spread=2.0)
    a = oc.select_legs(ch, "straddle")[0]
    assert a["entry"] == a["ask"] > a["mid"] > a["bid"]
    m = oc.select_legs(ch, "straddle", fill="mid")[0]
    assert m["entry"] == m["mid"]


def test_select_legs_errors():
    ch, _ = flat_chain()
    with pytest.raises(ValueError, match="結構"):
        oc.select_legs(ch, "iron_condor")
    with pytest.raises(ValueError, match="選法"):
        oc.select_legs(ch, "strangle", select="vibes")
    no_iv = dict(ch, atm_iv=None)
    with pytest.raises(ValueError, match="預期波動"):
        oc.select_legs(no_iv, "strangle", select="move")
    assert oc.select_legs(no_iv, "strangle", select="move", ref_move=600.0)           # 自己填就不需要 IV
    empty = dict(ch, rows=[])
    with pytest.raises(ValueError, match="跨式"):
        oc.select_legs(empty, "straddle")
    with pytest.raises(ValueError, match="找不到"):
        oc.select_legs(empty, "call")


# ── 到期損益與損益兩平 ─────────────────────────────────────────────

def L(right, k, qty=1, entry=0.0):
    return {"right": right, "strike": float(k), "qty": qty, "entry": entry}


def test_breakevens_known_cases():
    assert oc.breakevens([L("C", 100), L("P", 100)], 20) == [80.0, 120.0]               # 跨式：K ± 成本
    assert oc.breakevens([L("C", 110), L("P", 90)], 8) == [82.0, 118.0]                  # 勒式：上翼 + 成本、下翼 − 成本
    assert oc.breakevens([L("C", 100)], 5) == [105.0] and oc.breakevens([L("P", 100)], 5) == [95.0]
    assert oc.breakevens([L("P", 10)], 12) == []                                         # 成本比履約價還大：不可能回本（S 不能為負）
    assert oc.breakevens([L("C", 100, qty=2), L("P", 100, qty=1)], 30) == [70.0, 115.0]  # 口數不同：右邊斜率 2、左邊 1
    assert oc.breakevens([], 5) == [] and oc.breakevens([L("C", 100)], 0) == []


def test_breakevens_agree_with_a_brute_force_scan():
    rng = random.Random(5)
    for _ in range(200):
        legs = [L(rng.choice("CP"), rng.randint(80, 120), rng.randint(1, 3)) for _ in range(rng.randint(1, 4))]
        basis = rng.uniform(1, 40)
        got = oc.breakevens(legs, basis)
        xs = [i * 0.01 for i in range(1, 30001)]
        scan, prev = [], oc.payoff(legs, xs[0]) - basis
        for x in xs[1:]:
            cur = oc.payoff(legs, x) - basis
            if prev * cur < 0 or cur == 0:
                scan.append(x)
            prev = cur
        assert len(got) == len(scan) and all(abs(a - b) < 0.02 for a, b in zip(got, scan)), (legs, basis, got, scan)


def test_payoff_is_the_sum_of_intrinsic_values():
    legs = [L("C", 100, 2), L("P", 90, 1)]
    assert oc.payoff(legs, 110) == 20.0 and oc.payoff(legs, 80) == 10.0 and oc.payoff(legs, 95) == 0.0


# ── 情境表 ────────────────────────────────────────────────────────

def test_expiry_column_equals_payoff_minus_cost():
    ch, _ = flat_chain(spread_pct=0.03, min_spread=1.0)
    legs = oc.select_legs(ch, "strangle", select="delta", delta=0.25)
    a = oc.analyze(ch, legs, kind="strangle", hold_hours=6)
    cost = sum(l["entry"] for l in legs)
    sc = a["scenarios"]
    last = len(sc["time_cols"]) - 1
    assert sc["time_cols"][last]["label"] == "到期"
    for m, row in zip(sc["moves"], sc["pnl_pts"]):
        S = ch["F"] + m["pts"]
        assert row[last] == pytest.approx(oc.payoff(legs, S) - cost, abs=0.15)


def test_now_column_at_zero_move_is_minus_the_slippage():
    ch, _ = flat_chain(spread_pct=0.04, min_spread=2.0)
    legs = oc.select_legs(ch, "straddle")
    a = oc.analyze(ch, legs, kind="straddle")
    row0 = next(i for i, m in enumerate(a["scenarios"]["moves"]) if m["mult"] == 0.0)
    assert a["scenarios"]["pnl_pts"][row0][0] == pytest.approx(-a["cost"]["slippage_pts"], abs=0.2)         # 一進場就虧的買賣價差
    assert a["cost"]["slippage_pts"] > 0


def test_time_decay_shows_up_for_an_atm_straddle_when_nothing_moves():
    ch, _ = flat_chain()
    a = oc.analyze(ch, oc.select_legs(ch, "straddle"), kind="straddle", hold_hours=6)
    row0 = next(i for i, m in enumerate(a["scenarios"]["moves"]) if m["mult"] == 0.0)
    vals = a["scenarios"]["pnl_pts"][row0]
    k = a["legs"][0]["strike"]
    assert all(x > y for x, y in zip(vals, vals[1:]))
    assert vals[-1] == pytest.approx(-a["cost"]["pts"] + abs(ch["F"] - k), abs=0.5)      # 不動 → 一路掉到只剩內含價值 |F−K|，扣掉權利金


def test_iv_table_is_monotonic_for_long_options():
    ch, _ = flat_chain()
    for kind in ("straddle", "strangle", "call"):
        a = oc.analyze(ch, oc.select_legs(ch, kind, select="delta", delta=0.3), kind=kind)
        for row in a["scenarios"]["iv"]["pnl_pts"]:
            assert all(x < y for x, y in zip(row, row[1:])), (kind, row)         # IV 平移越高，買方損益越好


def test_time_columns_collapse_for_a_very_short_expiry():
    cols = oc._time_cols(0.5 / 365, 6)
    assert [c["label"] for c in cols][-1] == "到期" and len(cols) == len({round(c["days"], 4) for c in cols})
    assert cols[0]["days"] == 0 and cols[-1]["days"] == pytest.approx(0.5)
    long = [c["label"] for c in oc._time_cols(9 / 365, 6)]
    assert long == ["現在", "6 小時後", "1 天後", "到期"]
    assert [c["label"] for c in oc._time_cols(9 / 365, 24)].count("到期") == 1 and len(oc._time_cols(9 / 365, 24)) == 3


# ── 機率 ──────────────────────────────────────────────────────────

def test_lognormal_probability_matches_monte_carlo():
    F, s, T = 100.0, 0.25, 0.1
    rng = random.Random(1)
    v = s * math.sqrt(T)
    n = 300_000
    S = [F * math.exp(-0.5 * v * v + v * rng.gauss(0, 1)) for _ in range(n)]
    assert sum(S) / n == pytest.approx(F, rel=0.003)                     # 平均就是遠期價
    bes, below = [92.0, 108.0], [True, False]
    mc = sum(1 for x in S if x < 92.0 or x > 108.0) / n
    assert oc.prob_lognormal(F, s, T, bes, below) == pytest.approx(mc, abs=0.004)


def test_each_tail_of_the_lognormal_probability_matches_monte_carlo():
    """上下兩尾各自對照蒙地卡羅（兩尾相加時漂移項的誤差會互相抵銷，單看一尾才抓得到）。"""
    F, s, T = 100.0, 0.25, 0.1
    rng = random.Random(11)
    v = s * math.sqrt(T)
    S = [F * math.exp(-0.5 * v * v + v * rng.gauss(0, 1)) for _ in range(300_000)]
    up = sum(1 for x in S if x > 108.0) / len(S)
    dn = sum(1 for x in S if x < 92.0) / len(S)
    assert oc.prob_lognormal(F, s, T, [108.0], [False]) == pytest.approx(up, abs=0.004)
    assert oc.prob_lognormal(F, s, T, [92.0], [True]) == pytest.approx(dn, abs=0.004)


def test_lognormal_probability_limits():
    assert oc.prob_lognormal(100, 0.2, 0.1, [], []) is None and oc.prob_lognormal(100, 0.0, 0.1, [110], [False]) is None
    near, far = oc.prob_lognormal(100, 0.2, 0.1, [101], [False]), oc.prob_lognormal(100, 0.2, 0.1, [130], [False])
    assert near > 0.4 > far > 0 and oc.prob_lognormal(100, 0.001, 0.01, [130], [False]) < 1e-9


def test_hist_stats_on_a_random_walk_agree_with_the_lognormal_probability():
    rng = random.Random(3)
    daily = 0.012
    closes = [100.0]
    for _ in range(6000):
        closes.append(closes[-1] * math.exp(rng.gauss(0, daily)))
    h, F = 5, 100.0
    sigma_ann_equiv = daily * math.sqrt(h)                                # h 天的標準差
    be = [F * math.exp(1.0 * sigma_ann_equiv), F * math.exp(-1.0 * sigma_ann_equiv)]
    st = oc.hist_stats(closes, h, F, be, [False, True])
    assert st["n"] == len(closes) - h and st["h_days"] == h
    assert st["p"] == pytest.approx(0.317, abs=0.03)                      # 越過 ±1 個標準差的機率
    q = st["abs_move_q"]
    assert q["50"] < q["75"] < q["90"] and q["50"] == pytest.approx(0.6745 * F * sigma_ann_equiv, rel=0.1)


def test_hist_stats_needs_enough_history():
    assert oc.hist_stats([100.0] * 20, 5, 100.0, [110.0], [False]) is None
    assert oc.hist_stats([], 5, 100.0, [110.0], [False]) is None
    assert oc.hist_stats([100.0 + i for i in range(100)], 0, 100.0, [110.0], [False]) is None


def test_trading_days_from_calendar_time():
    assert oc.trading_days(8.78 / 365) == 6 and oc.trading_days(1 / 365) == 1 and oc.trading_days(0.1 / 365) == 1 and oc.trading_days(30 / 365) == 21


# ── 試算（A）整體 ─────────────────────────────────────────────────

def test_analyze_straddle_numbers():
    ch, _ = flat_chain(spread_pct=0.03, min_spread=1.0)
    legs = oc.select_legs(ch, "straddle")
    a = oc.analyze(ch, legs, kind="straddle", lots=2, budget_twd=200_000)
    cost = legs[0]["ask"] + legs[1]["ask"]
    k = legs[0]["strike"]
    assert a["cost"]["pts"] == pytest.approx(cost, abs=0.05) and a["cost"]["per_lot_twd"] == round(cost * 50) and a["cost"]["total_twd"] == round(cost * 50) * 2
    assert a["cost"]["max_lots"] == int(200_000 // (cost * 50)) and a["max_loss"]["pts"] == a["cost"]["pts"]
    assert [b["level"] for b in a["breakeven"]] == [pytest.approx(k - cost, abs=0.1), pytest.approx(k + cost, abs=0.1)]
    assert a["breakeven"][0]["dist_pts"] < 0 < a["breakeven"][1]["dist_pts"]
    assert a["expected_move"]["straddle_pts"] == pytest.approx(a["expected_move"]["sigma_pts"] * 0.7979, abs=0.2)
    assert abs(a["greeks"]["delta"]) < 0.1 and a["greeks"]["theta_day_pts"] < 0 < a["greeks"]["vega_pts"] and a["greeks"]["gamma"] > 0
    assert a["greeks"]["theta_day_twd"] == pytest.approx(a["greeks"]["theta_day_pts"] * 50 * 2, abs=1.5)        # 元＝點 × 50 × 2 口（點數只顯示到小數第 2 位）
    assert a["greeks"]["vega_twd"] == pytest.approx(a["greeks"]["vega_pts"] * 50 * 2, abs=1.5)
    assert a["prob"]["lognormal"] is not None and 0 < a["prob"]["lognormal"] < 1 and a["prob"]["hist"] is None


def test_analyze_with_history_adds_the_empirical_comparison():
    ch, _ = flat_chain()
    rng = random.Random(8)
    closes = [30000.0]
    for _ in range(2500):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 0.011)))
    a = oc.analyze(ch, oc.select_legs(ch, "straddle"), kind="straddle", closes=closes)
    h = a["prob"]["hist"]
    assert h["h_days"] == oc.trading_days(ch["T"]) and h["n"] > 2000 and 0 < h["p"] < 1 and h["abs_move_q"]["90"] > h["abs_move_q"]["50"]


def test_reference_move_changes_the_scenario_rows_only():
    ch, _ = flat_chain()
    legs = oc.select_legs(ch, "straddle")
    base = oc.analyze(ch, legs, kind="straddle")
    mine = oc.analyze(ch, legs, kind="straddle", ref_move=800.0)
    assert mine["expected_move"]["ref_source"] == "user" and base["expected_move"]["ref_source"] == "implied"
    assert next(m for m in mine["scenarios"]["moves"] if m["mult"] == 1.0)["pts"] == 800.0
    assert next(m for m in base["scenarios"]["moves"] if m["mult"] == 1.0)["pts"] == pytest.approx(base["expected_move"]["sigma_pts"], abs=0.1)
    assert [b["dist_em"] for b in mine["breakeven"]] == [b["dist_em"] for b in base["breakeven"]]      # 損益兩平的倍數一律用市場隱含的 1σ


def test_quantity_scales_the_greeks():
    ch, _ = flat_chain()
    one = oc.select_legs(ch, "call", select="delta", delta=0.4)
    two = [dict(one[0], qty=2)]
    g1, g2 = oc.analyze(ch, one, kind="call")["greeks"], oc.analyze(ch, two, kind="call")["greeks"]
    assert g2["delta"] == pytest.approx(2 * g1["delta"], abs=0.002) and g2["vega_pts"] == pytest.approx(2 * g1["vega_pts"], abs=0.05)
    assert oc.analyze(ch, two, kind="call")["cost"]["pts"] == pytest.approx(2 * oc.analyze(ch, one, kind="call")["cost"]["pts"], abs=0.1)


def test_exit_spread_scales_with_quantity():
    ch, _ = flat_chain(spread_pct=0.03, min_spread=1.0)
    one = oc.select_legs(ch, "call", select="delta", delta=0.4)
    two = [dict(one[0], qty=2)]
    a1, a2 = oc.analyze(ch, one, kind="call"), oc.analyze(ch, two, kind="call")
    assert a1["cost"]["exit_spread_pts"] > 0
    assert a2["cost"]["exit_spread_pts"] == pytest.approx(2 * a1["cost"]["exit_spread_pts"], abs=0.11)
    assert a2["cost"]["slippage_pts"] == pytest.approx(2 * a1["cost"]["slippage_pts"], abs=0.11)


def test_analyze_warnings():
    ch, q = flat_chain(spread_pct=0.25, min_spread=1.0)                  # 價差佔中價 25%
    legs = oc.select_legs(ch, "strangle", select="delta", delta=0.1)
    w = " ".join(oc.analyze(ch, legs, kind="strangle")["warnings"])
    assert "買賣價差佔中價" in w
    short, _ = flat_chain(days=2.0)
    assert "距到期只剩" in " ".join(oc.analyze(short, oc.select_legs(short, "straddle"), kind="straddle")["warnings"])
    legs0 = [dict(legs[0], bid=0.0, spread=None, spread_pct=None)]
    assert "沒有買價" in " ".join(oc.analyze(ch, legs0, kind="call")["warnings"])
    noiv = dict(ch, atm_iv=None)
    assert "ATM IV 不明" in " ".join(oc.analyze(noiv, [dict(legs[0], iv=None)], kind="call", ref_move=500)["warnings"])


def test_everything_is_json_safe():
    ch, _ = flat_chain(spread_pct=0.03, min_spread=1.0)
    for kind in oc.KINDS:
        a = oc.analyze(ch, oc.select_legs(ch, kind, select="move", k=0.7), kind=kind, lots=3, budget_twd=100000, ref_move=700)
        json.dumps(a, allow_nan=False)
    deep = oc.analyze(ch, oc.select_legs(ch, "call", select="delta", delta=0.05), kind="call")
    json.dumps(deep, allow_nan=False)


def test_model_quotes_shape():
    q = oc.model_quotes(49283.0, 20.3, 8.78, step=50.0, spread_pct=0.03, min_spread=1.0)
    assert {x["right"] for x in q} == {"C", "P"} and all(x["strike"] % 50 == 0 for x in q)
    assert all(0 <= x["bid"] < x["ask"] for x in q) and len({x["strike"] for x in q}) > 100


# ── 平倉試算（B）─────────────────────────────────────────────────

def straddle_legs():
    return [{"right": "C", "strike": 100.0, "qty": 1, "entry": 50.0}, {"right": "P", "strike": 100.0, "qty": 1, "entry": 50.0}]


def test_the_requirement_documents_example_closing_the_losing_leg():
    """文件：跨式成本 100（Call 50 + Put 50）；Put 跌到剩 20 → 平掉 Put 已虧 30；留下 Call 要值到 80 才回本。"""
    marks = [{"bid": 78.0, "ask": 82.0}, {"bid": 20.0, "ask": 20.0}]
    res = oc.close_analysis(straddle_legs(), F=130.0, T=20 / 365, iv=0.25, marks=marks)
    a = next(x for x in res["actions"] if x["id"] == "close_1")
    assert a["realized_pts"] == -30.0 and a["net_basis_pts"] == 80.0 and a["proceeds_pts"] == 20.0
    assert [b["level"] for b in a["breakeven"]] == [180.0]                         # 履約價 100 + 80
    assert a["max_loss_pts"] == -80.0 and a["closed"] == [1]
    assert a["need_ratio"] == pytest.approx(80 / 80, abs=1e-6)                      # 留下的 Call 中價 80、要值 80 才回本
    roles = {l["role"] for l in res["legs"]}
    assert roles == {"loser", "winner"} and res["legs"][1]["role"] == "loser" and res["legs"][0]["role"] == "winner"


def test_actions_are_enumerated():
    res = oc.close_analysis(straddle_legs(), F=100.0, T=20 / 365, iv=0.25)
    assert [x["id"] for x in res["actions"]] == ["hold", "close_0", "close_1", "all"]
    one = oc.close_analysis([straddle_legs()[0]], F=100.0, T=20 / 365, iv=0.25)
    assert [x["id"] for x in one["actions"]] == ["hold", "close_0"] and one["legs"][0]["role"] is None


def test_hold_and_close_all_are_the_two_extremes():
    marks = [{"bid": 78.0, "ask": 82.0}, {"bid": 20.0, "ask": 22.0}]
    res = oc.close_analysis(straddle_legs(), F=130.0, T=20 / 365, iv=0.25, marks=marks)
    hold, allc = res["actions"][0], res["actions"][-1]
    assert hold["realized_pts"] == 0.0 and hold["net_basis_pts"] == 100.0 and hold["max_loss_pts"] == -100.0 and hold["closed"] == []
    assert allc["realized_pts"] == (78 - 50) + (20 - 50) == -2 and allc["breakeven"] == [] and allc["scenarios"] is None and allc["max_loss_pts"] == -2.0
    assert [b["level"] for b in hold["breakeven"]] == [200.0]          # 履約價 100、淨成本 100：上方 200；下方剛好是 0（價格不能為負，不算）


def test_net_basis_and_max_loss_are_consistent_for_every_action():
    marks = [{"bid": 60.0, "ask": 64.0}, {"bid": 30.0, "ask": 33.0}]
    res = oc.close_analysis(straddle_legs(), F=110.0, T=15 / 365, iv=0.22, marks=marks)
    for a in res["actions"][:-1]:
        assert a["max_loss_pts"] == pytest.approx(-a["net_basis_pts"], abs=0.11) and a["net_basis_pts"] == pytest.approx(100 - a["proceeds_pts"], abs=0.11)


def test_scenario_totals_match_a_direct_computation():
    marks = [{"bid": 78.0, "ask": 82.0}, {"bid": 20.0, "ask": 22.0}]
    res = oc.close_analysis(straddle_legs(), F=130.0, T=20 / 365, iv=0.25, marks=marks, hold_hours=24)
    a = next(x for x in res["actions"] if x["id"] == "close_1")
    keep = [l for i, l in enumerate(straddle_legs()) if i not in a["closed"]]
    sc = a["scenarios"]
    last = len(sc["time_cols"]) - 1
    for m, row in zip(sc["moves"], sc["pnl_pts"]):
        S = 130.0 + m["pts"]
        assert row[last] == pytest.approx(20.0 - 100.0 + oc.payoff(keep, S), abs=0.15)        # 到期：已收回的 20 − 全部成本 100 + 留下那腳的價值
    assert sc["time_cols"][0]["label"] == "現在"


def test_holding_everything_has_the_same_scenarios_as_a_fresh_position():
    ch, _ = flat_chain()
    legs = oc.select_legs(ch, "straddle")
    a = oc.analyze(ch, legs, kind="straddle", hold_hours=24)
    held = [{"right": l["right"], "strike": l["strike"], "qty": 1, "entry": l["entry"]} for l in legs]
    marks = [{"bid": l["bid"], "ask": l["ask"], "iv": l["iv"]} for l in legs]
    b = oc.close_analysis(held, F=ch["F"], T=ch["T"], iv=ch["atm_iv"], marks=marks, ref_move=a["expected_move"]["ref_pts"], hold_hours=24)
    hold = b["actions"][0]["scenarios"]
    for ra, rb in zip(a["scenarios"]["pnl_pts"], hold["pnl_pts"]):
        assert ra[-1] == pytest.approx(rb[-1], abs=0.2)                                      # 到期欄一致


def test_marks_fall_back_to_the_model_without_quotes():
    res = oc.close_analysis(straddle_legs(), F=100.0, T=30 / 365, iv=0.2)
    assert any("沒有現價報價" in w for w in res["warnings"])
    atm = black76(100.0, 100.0, 30 / 365, 0.2, R, True)
    assert res["legs"][0]["mid"] == pytest.approx(atm, abs=0.06) and res["legs"][0]["bid"] == res["legs"][0]["ask"]
    with pytest.raises(ValueError, match="沒有報價也沒有 IV"):
        oc.close_analysis(straddle_legs(), F=100.0, T=30 / 365)


def test_a_missing_quantity_means_one_lot_but_zero_is_rejected():
    legs = [{"right": "C", "strike": 100.0, "entry": 5.0}]
    res = oc.close_analysis(legs, F=100.0, T=30 / 365, iv=0.2)
    assert res["legs"][0]["qty"] == 1
    with pytest.raises(ValueError, match="每一腳"):
        oc.close_analysis([dict(legs[0], qty=0)], F=100.0, T=30 / 365, iv=0.2)


def test_missing_bid_is_warned_and_counted_as_zero():
    res = oc.close_analysis(straddle_legs(), F=100.0, T=30 / 365, iv=0.2, marks=[{"bid": 0.0, "ask": 3.0, "close": 2.0}, {"bid": 5.0, "ask": 5.4}])
    assert any("沒有買價" in w for w in res["warnings"])
    assert next(x for x in res["actions"] if x["id"] == "close_0")["proceeds_pts"] == 0.0


def test_close_analysis_validation():
    with pytest.raises(ValueError, match="沒有任何一腳"):
        oc.close_analysis([], F=100.0, T=0.1, iv=0.2)
    with pytest.raises(ValueError, match="已到期"):
        oc.close_analysis(straddle_legs(), F=100.0, T=0.0, iv=0.2)
    for bad in ({"right": "X"}, {"qty": 0}, {"entry": -1.0}, {"strike": 0.0}):
        legs = [dict(straddle_legs()[0], **bad)]
        with pytest.raises(ValueError, match="每一腳"):
            oc.close_analysis(legs, F=100.0, T=0.1, iv=0.2)


def test_close_analysis_is_json_safe():
    marks = [{"bid": 78.0, "ask": 82.0}, {"bid": 0.0, "ask": 0.0}]
    res = oc.close_analysis(straddle_legs(), F=130.0, T=5 / 365, iv=0.3, marks=marks)
    json.dumps(res, allow_nan=False)
