"""日內動能（core/intraday_momentum_study.py，update.md 發現 75）：讀檔、略過規則、交易方向、植入／無關／反向的世界。"""
from __future__ import annotations

import ast
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import intraday_momentum_study as st

ROOT = Path(__file__).resolve().parent.parent


def world(seed=0, n=1500, beta=0.0, start=date(2020, 3, 2)):
    """r_last＝beta × r_first ＋ 雜訊；每 20 個交易日一個月結算日、每 5 個一個週選結算日。"""
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    bars, c = {}, 10000.0
    for x in days:
        gap, m1, mid, pre = 0.006 * rng.standard_normal(), 0.004 * rng.standard_normal(), 0.005 * rng.standard_normal(), 0.002 * rng.standard_normal()
        o = c * np.exp(gap)
        c915 = o * np.exp(m1)
        c1245 = c915 * np.exp(mid)
        c1315 = c1245 * np.exp(pre)
        c1345 = c1315 * np.exp(beta * (gap + m1) + 0.002 * rng.standard_normal())
        bars[x] = {846: (o, o), 915: (c915, c915), 1245: (c1245, c1245), 1315: (c1315, c1315), 1345: (c1345, c1345)}
        c = c1345
    return bars, days, set(days[::20]), set(days[3::5])


def test_load_bars_reads_only_the_needed_bars(tmp_path):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE bars_1m(code TEXT NOT NULL, date TEXT NOT NULL, hhmm INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume INTEGER, PRIMARY KEY(code, date, hhmm))")
    c.executemany("INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?)", [("TXF", "2026-10-22", 915, 100, 101, 99, 100.5, 10), ("TXF", "2026-10-22", 916, 1, 1, 1, 1, 1),
                                                                 ("TXF", "2026-10-22", 1345, 0, 0, 0, 0, 0), ("MXF", "2026-10-22", 915, 5, 5, 5, 5, 5)])
    c.commit()
    c.close()
    assert st.load_bars(db) == {"2026-10-22": {915: (100.0, 100.5)}}


def test_build_skips_rolls_gaps_and_missing_bars():
    bars, days, _, _ = world(seed=1, n=12)
    del bars[days[5]]                                                                  # 第 5 天沒資料 → 第 6 天的前一天不在 → 略過
    del bars[days[9]][1245]                                                            # 缺一根 → 略過
    b, sk = st.build(bars, days, monthly={days[2]}, weekly={days[7]})
    assert sk == {"no_prev": 2, "roll": 2, "missing": 1}                               # 第 0、6 天沒有前一天；第 2、3 天換月；第 9 天缺
    assert list(b["date"]) == [days[i] for i in (1, 4, 7, 8, 10, 11)]
    x, p = bars[days[7]], bars[days[6]]
    i = list(b["date"]).index(days[7])
    assert b["first"][i] == pytest.approx(np.log(x[915][1] / p[1345][1])) and b["last"][i] == pytest.approx(np.log(x[1345][1] / x[1315][1]))
    assert b["open30"][i] == pytest.approx(np.log(x[915][1] / x[846][0])) and b["pre"][i] == pytest.approx(np.log(x[1315][1] / x[1245][1]))
    assert b["wexp"][i] == 1.0 and b["wexp"][0] == 0.0


def test_trades_follow_the_sign_and_skip_zero():
    tr, on = st.trades(np.array([0.01, -0.02, 0.0]), np.array([0.001, 0.002, 0.5]))
    assert list(on) == [True, True, False] and tr == pytest.approx([0.001 - st.COST, -0.002 - st.COST])
    assert st.COST == pytest.approx(4 / 47000) and st.Z == pytest.approx(1.95996, abs=1e-4)


def test_planted_momentum_passes_and_the_tests_are_the_registered_ones():
    bars, days, m, w = world(seed=2, n=1600, beta=0.08)
    r = st.run(bars, days, m, w)
    assert r["verdict"] == {"T1": "過關", "T2": "過關"}, (r["T1"], r["T2"])
    b, _ = st.build(bars, days, m, w)
    tr, _ = st.trades(b["first"], b["last"])
    assert r["T1"]["t"] == pytest.approx(st.slope_test(b["last"], b["first"], 5)["t"]) and r["T2"]["t"] == pytest.approx(st.mean_test(tr, 5)["t"])
    assert r["describe"]["net_pts"]["大台"] > r["describe"]["net_pts"]["微台"]


def test_null_world_false_positive_rate_and_reversal_does_not_pass():
    fp = 0
    sims = 100
    for s in range(sims):
        bars, days, m, w = world(seed=300 + s, n=1000)
        r = st.run(bars, days, m, w)
        fp += r["T1"]["pass"] or r["T2"]["pass"]
    assert fp / sims <= 0.06, fp
    bars, days, m, w = world(seed=8, n=1600, beta=-0.08)
    r = st.run(bars, days, m, w)
    assert r["T1"]["t"] < -2 and not r["T1"]["pass"] and not r["T2"]["pass"]


def test_halves_condition():
    rng = np.random.default_rng(4)
    x = rng.standard_normal(600)
    y = np.r_[-0.3 * x[:300], 0.8 * x[300:]] + rng.standard_normal(600)
    r = st._slope_halves(y, x)
    assert r["t"] > st.Z and r["first"] < 0 < r["second"] and not r["pass"]
    z = np.r_[np.full(300, -0.0002), np.full(300, 0.003)] + rng.standard_normal(600) * 0.002
    m = st._mean_halves(z)
    assert m["t"] > st.Z and m["first"] < 0 and not m["pass"]


def test_describe_and_report():
    bars, days, m, w = world(seed=5, n=800, beta=0.05)
    r = st.run(bars, days, m, w)
    d = r["describe"]
    assert set(d["net_pts"]) == {"微台", "小台", "大台"} and sum(q["n"] for q in d["quintiles"]) == r["n"]
    assert d["weekly_expiry"]["yes"]["n"] + d["weekly_expiry"]["no"]["n"] == r["T2"]["n"] and d["weekly_expiry"]["yes"]["n"] > 0
    b, _ = st.build(bars, days, m, w)
    assert d["weekly_expiry"]["yes"]["n"] == int(((b["wexp"] > 0) & (np.sign(b["first"]) != 0)).sum()) < r["T2"]["n"] // 3
    assert d["net_pts"]["小台"] - d["net_pts"]["微台"] == pytest.approx(2.5) and set(d["by_year"]) >= {"2020", "2021"}
    text = st.format_report(r)
    assert "T1" in text and "T2" in text and "五分位" in text and "nan" not in text
    small = st.run(*world(seed=6, n=10))
    assert small["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "樣本不夠" in st.format_report(small)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "intraday_momentum_study.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "intraday_momentum_study" not in " ".join(names), p
