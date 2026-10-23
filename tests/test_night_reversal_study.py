"""日盤收黑之後的夜盤（core/night_reversal_study.py，update.md 發現 73）：配對、檢定、植入／無關／方向相反的世界、描述。"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import drift_timing_study as dt
from core import night_reversal_study as st

ROOT = Path(__file__).resolve().parent.parent


def _shift(c, k):
    y, m = int(c[:4]), int(c[4:]) - 1 + k
    return f"{y + m // 12}{m % 12 + 1:02d}"


def world(seed=0, n=3000, kappa=0.0, start=date(2010, 1, 4), night_from="2017-05-16", mu=0.0003):
    """過夜 O_{t+1}＝mu ＋ kappa × [前一天日盤收黑] ＋ 雜訊；夜盤占過夜的 70%（另兩段跳空 10%／20%）。"""
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    out, c, prev_down = {}, 10000.0, False
    for i, x in enumerate(days):
        o = mu + (kappa if prev_down else 0.0) + 0.008 * rng.standard_normal()
        dd = 0.009 * rng.standard_normal()
        op = c * np.exp(o) if i else c
        cl = op * np.exp(dd)
        code = x[:4] + x[5:7]
        ks = (_shift(code, -1), code, _shift(code, 1))
        no, nc = {}, {}
        if i and x >= night_from:
            no, nc = {k: c * np.exp(0.1 * o) for k in ks}, {k: c * np.exp(0.8 * o) for k in ks}
        out[x] = dt.Day(code, {k: op for k in ks}, {k: cl for k in ks}, no, nc)
        c, prev_down = cl, cl < op
    return out


# ── 配對與檢定 ────────────────────────────────────────────────────

def test_pairs_line_up_the_day_signal_with_the_next_overnight():
    D = lambda o, c, no=None, nc=None: dt.Day("202610", {"202610": o}, {"202610": c}, {"202610": no} if no else {}, {"202610": nc} if nc else {})  # noqa: E731
    days = {"2026-10-19": D(100, 101), "2026-10-20": D(102, 99, 101.5, 101), "2026-10-21": D(98, 100, 99.5, 98.5),
            "2026-10-22": dt.Day("202612", {"202612": 50, "202610": 97}, {"202612": 51, "202610": 96}, {}, {}),   # 近月的前一天沒有收盤 → build 略過這天
            "2026-10-23": D(95, 94)}                                                                            # 這天有（用 10-22 的 202610）
    assert list(st.build(days)["date"]) == ["2026-10-20", "2026-10-21", "2026-10-23"]
    p = st.pairs(days)
    assert list(p["date"]) == ["2026-10-20"]                                                   # 10-21 → 10-23 中間隔了一天：不能配對
    assert p["D"][0] == pytest.approx(np.log(99 / 102)) and p["O"][0] == pytest.approx(np.log(98 / 99))
    assert p["N"][0] == pytest.approx(np.log(98.5 / 99.5)) and p["g1"][0] == pytest.approx(np.log(99.5 / 99)) and p["g2"][0] == pytest.approx(np.log(98 / 98.5))
    assert p["R"][0] == pytest.approx(np.log(99 / 101)) and p["wd"][0] == 1


def test_check_is_the_registered_regression_with_halves():
    rng = np.random.default_rng(1)
    down = rng.random(800) < 0.5
    y = 0.001 * down + 0.003 * rng.standard_normal(800)
    r = st.check(y, down)
    assert r["b"] == pytest.approx(y[down].mean() - y[~down].mean()) and r["t"] == pytest.approx(st.slope_test(y, down.astype(float), 10)["t"])
    assert r["pass"] and st.Z == pytest.approx(1.95996, abs=1e-4)
    y2 = y.copy()
    y2[:400] = 0.003 * rng.standard_normal(400) - 0.002 * down[:400]                          # 前半相反
    r2 = st.check(y2, down)
    assert r2["first"] < 0 and not r2["pass"]
    nan = np.r_[np.full(100, np.nan), y[100:]]
    assert st.check(nan, down)["n"] == 700                                                    # 夜盤沒有的日子不算
    y3 = y.copy()
    y3[:400] = 0.003 * rng.standard_normal(400) - 0.0003 * down[:400]                         # 前半略負、後半很強：整體顯著也不算
    y3[400:] += 0.002 * down[400:]
    r3 = st.check(y3, down)
    assert r3["t"] > st.Z and r3["first"] < 0 < r3["second"] and not r3["pass"]


def test_run_uses_the_registered_definitions():
    days = world(seed=5, kappa=0.0004)
    r, p = st.run(days), st.pairs(days)
    down = p["D"] < 0
    nn = ~np.isnan(p["N"])
    assert r["describe"]["down"]["n"] == int(down.sum()) and r["T1"]["b"] == pytest.approx(p["O"][down].mean() - p["O"][~down].mean())
    assert r["T2"]["b"] == pytest.approx(p["N"][nn & down].mean() - p["N"][nn & ~down].mean()) and r["T2"]["n"] == int(nn.sum())
    assert r["T2"]["b"] != pytest.approx(r["T1"]["b"])


def test_planted_world_passes_both():
    for seed in (1, 2):
        r = st.run(world(seed=seed, kappa=0.0012))
        assert r["verdict"] == {"T1": "過關", "T2": "過關"}, (r["T1"], r["T2"])
        assert r["describe"]["down"]["O"] > r["describe"]["up"]["O"] and r["n_night"] > 1000


def test_null_world_false_positive_rate_and_the_wrong_direction_never_passes():
    fp = 0
    sims = 100
    for s in range(sims):
        r = st.run(world(seed=100 + s, n=1500, start=date(2015, 1, 5)))
        fp += r["T1"]["pass"] or r["T2"]["pass"]
    assert fp / sims <= 0.06, fp
    rev = st.run(world(seed=7, kappa=-0.0012))                                                # 收紅之後比較好：方向事先定好，不能算過關
    assert rev["T1"]["t"] < -3 and not rev["T1"]["pass"] and not rev["T2"]["pass"]


# ── 描述 ──────────────────────────────────────────────────────────

def test_describe_groups_and_trading_versions_by_hand():
    p = {"date": np.array(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"], dtype=object),
         "D": np.array([-0.01, 0.01, -0.02, 0.005]), "R": np.array([0.01, -0.01, -0.02, 0.005]), "wd": np.array([0.0, 1.0, 2.0, 3.0]),
         "O": np.array([0.002, -0.001, 0.003, 0.0]), "N": np.array([0.001, np.nan, 0.002, -0.001]),
         "g1": np.array([0.0005, np.nan, 0.0, 0.0]), "g2": np.array([0.0005, np.nan, 0.001, 0.001])}
    d = st.describe(p)
    assert d["down"]["n"] == 2 and d["down"]["O"] == pytest.approx(0.0025 * dt.PTS) and d["down"]["N"] == pytest.approx(0.0015 * dt.PTS)
    assert d["up"]["O"] == pytest.approx(-0.0005 * dt.PTS) and d["up"]["N"] == pytest.approx(-0.001 * dt.PTS) and d["up"]["O_win"] == 0.0
    assert d["fall"]["n"] == 2 and d["fall"]["O"] == pytest.approx(0.001 * dt.PTS)                # 收跌＝第 2、3 天
    td = d["trade_down"]
    pnl = np.array([0.002 - dt.COST, 0, 0.003 - dt.COST, 0]) * dt.PTS
    assert td["trades"] == 2 and td["per_trade"] == pytest.approx(pnl[[0, 2]].mean()) and td["per_day"] == pytest.approx(pnl.mean())
    assert td["win"] == 1.0 and td["max_dd"] == 0.0 and d["trade_all"]["trades"] == 4
    assert d["trade_all"]["worst"] == pytest.approx((-0.001 - dt.COST) * dt.PTS) and d["trade_all"]["max_dd"] < 0
    assert d["by_year"]["2026"]["diff"] == pytest.approx((0.0025 + 0.0005) * dt.PTS) and set(d["weekday"]) == {"一", "二", "三", "四"}
    assert d["weekday"]["一"]["diff"] is None


def test_report_and_too_few():
    text = st.format_report(st.run(world(seed=3, kappa=0.0006)))
    assert "T1" in text and "T2" in text and "只在收黑後留倉" in text and "五分位" in text and "nan" not in text
    small = st.run(world(seed=4, n=10))
    assert small["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "樣本不夠" in st.format_report(small)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "night_reversal_study.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "night_reversal_study" not in " ".join(names), p
