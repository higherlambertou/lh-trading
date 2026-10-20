"""前一天 ATM IV 預測隔天日盤振幅（發現 54）：因子只用嚴格早於當天的 IV、過期不用、判定規則、植入已知依賴、無關 IV 的假陽性率、同一天 IV 的金絲雀。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import iv_range_study as st
from core.intraday_context import SESSION_MIN, Session

ROOT = Path(st.__file__).resolve().parent.parent


def world(n=1500, seed=0, kind="leading", noise=0.10):
    """隨機漫步日盤＋每日 IV。潛在波動 lv_t＝0.9·lv_{t−1}＋ε_t＋a_t（a_t 是『隔天才發生、但選擇權市場已經知道』的衝擊）。
    kind：leading＝IV_{t−1} 知道 a_t（比 R20 多的資訊）；concurrent＝IV_t 知道 lv_t（同一天，偷看用）；null＝IV 與波動無關。"""
    rng = np.random.default_rng(seed)
    d = date(2020, 3, 1)
    dates: list[str] = []
    while len(dates) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d.isoformat())
    lv = np.zeros(n)
    a = rng.normal(0, 0.25, n)
    for t in range(1, n):
        lv[t] = 0.9 * lv[t - 1] + rng.normal(0, 0.10) + a[t]
    sessions, prev_close, F = [], 10000.0, np.zeros(n)
    for t in range(n):
        scale = float(np.exp(lv[t]))
        o = prev_close + rng.normal(0, 5)
        c = o + np.cumsum(rng.normal(0, 2.0 * scale, SESSION_MIN))
        op = np.concatenate([[o], c[:-1]])
        hi = np.maximum(op, c) + np.abs(rng.normal(0, 0.5 * scale, SESSION_MIN))
        lo = np.minimum(op, c) - np.abs(rng.normal(0, 0.5 * scale, SESSION_MIN))
        sessions.append(Session(dates[t], hi, lo, op, c, np.full(SESSION_MIN, 100.0)))
        prev_close, F[t] = float(c[-1]), float(c[-1])
    iv = {}
    for t in range(n):
        if kind == "leading":
            lvl = 0.9 * lv[t] + (a[t + 1] if t + 1 < n else 0.0)                      # 明天的衝擊
        elif kind == "concurrent":
            lvl = lv[t]
        else:
            lvl = rng.normal(0, 0.5)
        iv[dates[t]] = {"iv": float(20 * np.exp(lvl + rng.normal(0, noise))), "F": F[t]}
    return sessions, iv


# ── 因子的時間規則 ───────────────────────────────────────────────

def mini(dates):
    n = len(dates)
    return [Session(d, np.full(SESSION_MIN, 10100.0 + i), np.full(SESSION_MIN, 10000.0), np.full(SESSION_MIN, 10010.0), np.full(SESSION_MIN, 10050.0 + i), np.full(SESSION_MIN, 1.0)) for i, d in enumerate(dates)]


def test_x_prev_is_the_nearest_strictly_earlier_iv_day_and_stale_ones_are_dropped():
    days = [(date(2026, 9, 1) + timedelta(days=i)) for i in range(60)]
    dates = [d.isoformat() for d in days if d.weekday() < 5]
    iv = {"2026-10-09": {"iv": 25.0, "F": 40000.0}, "2026-10-12": {"iv": 30.0, "F": 41000.0}, "2026-10-05": {"iv": 20.0, "F": 39000.0}}
    b = st.build(mini(dates), iv)
    pos = {d: i for i, d in enumerate(dates)}
    x = lambda v, f: float(np.log(v / 100 * f))   # noqa: E731
    assert b["x_prev"][pos["2026-10-12"]] == pytest.approx(x(25.0, 40000.0))          # 週一用上週五（嚴格早於當天），不用當天的 30
    assert b["x_same"][pos["2026-10-12"]] == pytest.approx(x(30.0, 41000.0))          # 同一天的只給金絲雀
    assert b["x_prev"][pos["2026-10-13"]] == pytest.approx(x(30.0, 41000.0))
    assert np.isnan(b["x_prev"][pos["2026-10-05"]]) and np.isnan(b["x_same"][pos["2026-10-13"]])      # 10/5 之前沒有 IV；10/13 沒有同一天的
    assert b["x_prev"][pos["2026-10-06"]] == pytest.approx(x(20.0, 39000.0))
    assert np.isnan(b["x_prev"][pos["2026-10-15"]]) is np.False_ and b["x_prev"][pos["2026-10-15"]] == pytest.approx(x(30.0, 41000.0))   # 10/12 → 10/15＝3 天：還可以用
    assert np.isnan(b["x_prev"][pos["2026-10-19"]])                                    # 10/12 → 10/19＝7 天：太舊，不用


def test_staleness_boundary_is_four_calendar_days():
    dates = ["2026-10-01", "2026-10-05", "2026-10-06"]
    iv = {"2026-10-01": {"iv": 25.0, "F": 40000.0}}
    b = st.build(mini(dates), iv)
    assert np.isfinite(b["x_prev"][1]) and np.isnan(b["x_prev"][2])                   # 10/1→10/5＝4 天可以；→10/6＝5 天不行


def test_bad_iv_rows_become_nan_not_garbage():
    dates = ["2026-10-01", "2026-10-02", "2026-10-05"]
    iv = {"2026-10-01": {"iv": 0.0, "F": 40000.0}, "2026-10-02": {"iv": 25.0, "F": -1.0}}
    b = st.build(mini(dates), iv)
    assert np.isnan(b["x_prev"][1]) and np.isnan(b["x_prev"][2]) and np.isnan(b["x_same"][0]) and np.isnan(b["x_same"][1])


def test_the_factor_never_peeks_at_today_or_later():
    sessions, iv = world(n=120, seed=1)
    base = st.build(sessions, iv)
    ds = [s.date for s in sessions]
    for t in (40, 70):
        tampered = dict(iv)
        for d in ds[t:]:
            tampered[d] = {"iv": 999.0, "F": 999.0}                                     # 把『今天以後』的 IV 全改成怪值
        b = st.build(sessions, tampered)
        assert np.array_equal(b["x_prev"][:t + 1], base["x_prev"][:t + 1], equal_nan=True), t                 # 到今天為止的因子不變（今天用的是更早的 IV）
        assert not np.allclose(b["x_prev"][t + 1:t + 3], base["x_prev"][t + 1:t + 3])


def test_targets_and_baseline_scale_definitions():
    sessions, iv = world(n=60, seed=2)
    b = st.build(sessions, iv)
    s = sessions[40]
    assert b["range"][40] == pytest.approx(np.log(np.nanmax(s.hi) - np.nanmin(s.lo)))
    assert b["move"][40] == pytest.approx(np.log(abs(s.cl[-1] - s.op[0])))
    r20 = np.mean([np.nanmax(x.hi) - np.nanmin(x.lo) for x in sessions[20:40]])
    assert b["lnR"][40] == pytest.approx(np.log(r20)) and np.isnan(b["lnR"][19]) and np.isfinite(b["lnR"][20])
    flat = mini(["2026-10-01", "2026-10-02"])
    flat[1] = Session("2026-10-02", flat[1].hi, flat[1].lo, np.full(SESSION_MIN, 10000.0), np.full(SESSION_MIN, 10000.0), flat[1].vol)
    assert np.isnan(st.build(flat, {})["move"][1])                                      # 收開差為 0：不取對數


# ── 判定規則 ─────────────────────────────────────────────────────

def res(ex_dm=2.5, ho_dm=2.0, ex_red=0.05, ho_red=0.04, coef=0.3):
    return {"explore": {"dm": ex_dm, "reduction": ex_red}, "holdout": {"dm": ho_dm, "reduction": ho_red}, "coef": {"x_iv": coef}}


def test_passes_needs_all_four_conditions_at_the_registered_thresholds():
    assert st.passes(res()) is True
    assert st.passes(res(ex_dm=1.95)) is False and st.passes(res(ex_dm=1.96)) is True
    assert st.passes(res(ho_dm=1.64)) is False and st.passes(res(ho_dm=1.645)) is True
    assert st.passes(res(ex_red=0.019)) is False and st.passes(res(ex_red=0.02)) is True
    assert st.passes(res(ho_red=0.019)) is False and st.passes(res(ho_red=0.02)) is True
    assert st.passes(res(coef=-0.1)) is False and st.passes(res(coef=0.0)) is False
    assert st.passes(None) is False and st.passes({**res(), "holdout": {"dm": None, "reduction": None}}) is False
    assert (st.Z_EXPLORE, st.Z_HOLDOUT, st.MIN_REDUCTION, st.SPLIT, st.MAX_STALE_DAYS) == (1.96, 1.645, 0.02, "2025-01-01", 4)


def test_evaluate_needs_enough_samples_and_skips_nan_rows():
    sessions, iv = world(n=200, seed=3)
    b = st.build(sessions, iv)
    assert st.evaluate(b["dates"], b["range"], b["lnR"], b["x_prev"]) is None                       # 不到 250＋60
    s2, i2 = world(n=700, seed=3)
    b2 = st.build(s2, i2)
    x = b2["x_prev"].copy()
    x[300:310] = np.nan
    r = st.evaluate(b2["dates"], b2["range"], b2["lnR"], x)
    base = st.evaluate(b2["dates"], b2["range"], b2["lnR"], b2["x_prev"])
    assert r["n"] == base["n"] - 10


# ── 管線（合成日盤）────────────────────────────────────────────

def test_a_planted_leading_signal_passes_every_condition():
    sessions, iv = world(n=1500, seed=11, kind="leading")
    r = st.run(sessions, iv)
    t = r["tests"]["range"]
    assert r["verdict"]["range"] == "過關" and t["explore"]["dm"] > 3 and t["holdout"]["dm"] > 2 and t["explore"]["reduction"] > 0.03 and t["coef"]["x_iv"] > 0 and t["coef"]["x_iv_t"] > 3
    assert t["r2"]["cand"] > t["r2"]["base"] and set(t["yearly"]) >= {"2021", "2022", "2023", "2024", "2025"}


def test_an_unrelated_iv_has_a_low_false_positive_rate():
    passed, dms = 0, []
    for s in range(8):
        sessions, iv = world(n=1300, seed=100 + s, kind="null")
        r = st.run(sessions, iv)
        passed += sum(v == "過關" for v in r["verdict"].values())
        dms.append(r["tests"]["range"]["explore"]["dm"])
    assert passed == 0 and np.mean(np.array(dms) > 1.96) <= 0.25                         # 16 次檢定、四個條件都要過：不該有過關的


def test_the_canary_flags_when_the_previous_day_beats_the_same_day():
    sessions, iv = world(n=1300, seed=21, kind="concurrent")
    r = st.run(sessions, iv)
    c = r["canary"]["range"]
    assert c["same_day_reduction"] > c["prev_reduction"] and c["suspect"] is False        # 同一天 IV（偷看）比前一天更有用＝正常
    s2, i2 = world(n=1300, seed=22, kind="leading")
    c2 = st.run(s2, i2)["canary"]["range"]
    assert c2["suspect"] is (c2["prev_reduction"] > c2["same_day_reduction"]) and c2["prev_reduction"] > c2["same_day_reduction"]


def test_report_and_json_roundtrip():
    sessions, iv = world(n=1000, seed=31, kind="leading")
    r = st.run(sessions, iv)
    json.dumps(r, default=float)
    text = st.format_report(r)
    assert "日盤高低差" in text and "日盤收開差" in text and "金絲雀" in text and "x_iv 係數" in text and "每年降幅" in text
    assert "樣本不夠" in st.format_report({**r, "tests": {"range": None, "move": None}})


def test_study_is_not_used_by_trading_paths():
    paths = list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "protection.py", "quote_hub.py", "range_suggest.py", "stop_suggest.py")] \
        + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_market.py")] + [ROOT / "main.py"]
    for p in paths:
        assert "iv_range_study" not in p.read_text(encoding="utf-8"), p


# ── 數字對得上獨立算的、邊界 ─────────────────────────────────────

def test_numbers_match_an_independent_calculation():
    sessions, iv = world(n=1500, seed=41, kind="leading")
    b = st.build(sessions, iv)
    r = st.evaluate(b["dates"], b["range"], b["lnR"], b["x_prev"])
    ok = np.isfinite(b["range"]) & np.isfinite(b["lnR"]) & np.isfinite(b["x_prev"])
    X1 = np.column_stack([np.ones(ok.sum()), b["lnR"][ok], b["x_prev"][ok]])
    beta = np.linalg.lstsq(X1, b["range"][ok], rcond=None)[0]
    assert r["coef"]["x_iv"] == pytest.approx(beta[2]) and r["coef"]["lnR"] == pytest.approx(beta[1]) and r["coef"]["x_iv"] != r["coef"]["lnR"]
    assert r["corr_x_lnR"] == pytest.approx(np.corrcoef(b["x_prev"][ok], b["lnR"][ok])[0, 1]) and r["n"] == ok.sum()
    oos_dates = b["dates"][ok][st.WARM:]
    assert r["explore"]["n"] == (oos_dates < st.SPLIT).sum() and r["holdout"]["n"] == (oos_dates >= st.SPLIT).sum() and r["n_oos"] == len(oos_dates)       # 樣本外誤差和日期對齊
    assert sum(v["n"] for v in r["yearly"].values()) == r["n_oos"]
    e0 = st.oos_errors(np.column_stack([np.ones(ok.sum()), b["lnR"][ok]]), b["range"][ok])
    e1 = st.oos_errors(X1, b["range"][ok])
    m = oos_mask = oos_dates >= st.SPLIT
    assert r["holdout"]["reduction"] == pytest.approx(1 - (e1[m] ** 2).mean() / (e0[m] ** 2).mean()) and r["full_reduction"] == pytest.approx(1 - (e1 ** 2).mean() / (e0 ** 2).mean())
    assert r["holdout"]["dm"] == pytest.approx(st.dm_t(e0[m], e1[m])) and oos_mask.sum() == r["holdout"]["n"]


def test_sample_size_floor_and_small_periods_are_not_tested():
    rng = np.random.default_rng(0)

    def arrays(n):
        d = np.array([(date(2021, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)])
        return d, rng.normal(size=n), rng.normal(size=n), rng.normal(size=n)
    assert st.evaluate(*arrays(st.WARM + 59)) is None
    assert st.evaluate(*arrays(st.WARM + 60)) is not None
    sessions, iv = world(n=1275, seed=43, kind="leading")                                 # 保留期（2025 起）只有十幾個樣本外日子
    b = st.build(sessions, iv)
    r = st.evaluate(b["dates"], b["range"], b["lnR"], b["x_prev"])
    assert 0 < r["holdout"]["n"] < 30 and r["holdout"]["dm"] is None and r["holdout"]["reduction"] is None and "2025" not in r["yearly"]
    assert st.passes(r) is False                                                       # 保留期不夠就不算過關（不是當成通過）
