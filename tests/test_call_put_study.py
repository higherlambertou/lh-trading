"""到期前一天買價平買權 vs 賣權（core/call_put_study.py，update.md 發現 74）。

合成世界：選擇權用「沒有漂移」的遠期價（Black-76）定價；植入的世界裡指數每天真的漂 +mu，所以買權被低估、賣權被高估。"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import call_put_study as st
from core import option_backtest as ob
from core import weekly_straddle_study as ws
from core.iv_monitor import black76

ROOT = Path(__file__).resolve().parent.parent


def world(seed=0, n_days=3000, mu=0.0, start=date(2013, 1, 7), sigma=0.012):
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    S = np.empty(n_days)
    S[0] = 10000.0
    for i in range(1, n_days):
        S[i] = S[i - 1] * np.exp(mu + sigma * rng.standard_normal() - sigma ** 2 / 2)
    tx = {x.isoformat(): ws.TxDay("X", {"X": float(S[i])}) for i, x in enumerate(days)}
    rows, fsp = [], {}
    for i in range(25, n_days):
        x = days[i]
        if x.weekday() != 2:
            continue
        code = f"{x.year}{x.month:02d}W{(x.day - 1) // 7 + 1}"
        fsp[code] = (x, float(S[i]))
        F = float(S[i - 1])
        step = max(round(F * 0.005), 1)                                             # 履約價間隔跟著價格（往下漂很久的世界價格會很低）
        k0 = round(F / step) * step
        for m in range(-6, 7):
            k = k0 + step * m
            if k <= 0:
                continue
            for right in ("C", "P"):
                px = max(round(black76(F, k, 1 / 252, sigma * np.sqrt(252), 0.0, right == "C"), 1), 0.1)
                rows.append({"date": days[i - 1].isoformat(), "expiry": code, "kind": "W", "session": "G", "right": right, "strike": float(k),
                             "F": F, "settle": px, "close": None, "bid": max(px - 1, 0.1), "ask": px + 1})
    return ws.group_chain(rows), fsp, tx


def test_one_expiry_by_hand():
    q = {(46000.0, "C"): ob.Q(110.0, None, 100.0, 112.0), (46000.0, "P"): ob.Q(95.0, None, 90.0, 300.0)}
    chain = {("2026-10-13", "202610W2"): {"F": 46010.0, "kind": "W", "q": q}}
    tx = {d: ws.TxDay("202610", {"202610": 46000.0}) for d in ("2026-10-12", "2026-10-13", "2026-10-14")}
    tr, skipped = st.build_legs(chain, {"202610W2": (date(2026, 10, 14), 46150.0)}, tx, cpi=[])
    t = tr[0]
    cc, cp = 112.0 + 0.4 + 0.112, 95.0 * 1.02 + 0.4 + 0.001 * 95.0 * 1.02
    assert skipped == {"no_entry_rows": 0, "no_atm": 0} and t["entry"] == "2026-10-13" and t["K"] == 46000.0
    assert t["cost_c"] == pytest.approx(cc) and t["cost_p"] == pytest.approx(cp)
    assert t["rc"] == pytest.approx(150 * 0.999 / cc - 1) and t["rp"] == pytest.approx(-1.0) and t["pts_c"] == pytest.approx(150 * 0.999 - cc)
    assert t["rc_sens"] == pytest.approx(150 * 0.999 / ob.buy_cost(110 * 1.015) - 1) and t["cpi"] is False
    assert st.build_legs({}, {"202610W2": (date(2026, 10, 14), 1.0)}, tx)[1]["no_entry_rows"] == 1


def test_mean_check_halves_and_threshold():
    assert st.Z == pytest.approx(1.95996, abs=1e-4)
    x = np.r_[np.full(200, -0.05), np.full(200, 0.4)] + np.random.default_rng(1).standard_normal(400) * 0.3
    r = st.mean_check(x)
    assert r["t"] > st.Z and r["first"] < 0 and not r["pass"]
    assert st.mean_check(np.abs(x) + 0.1)["pass"]
    y = np.random.default_rng(12).standard_normal(80) * 0.5 + 0.1                     # t≈1.90：沒校正會過
    r = st.mean_check(y)
    assert 1.645 < r["t"] < st.Z and not r["pass"]
    assert r["t"] == pytest.approx(ws.mean_test(y, 5)["t"])


def test_planted_drift_makes_calls_beat_puts():
    for seed in (1, 2):
        r = st.run(*world(seed=seed, mu=0.0025))
        assert r["verdict"]["T1"] == "過關", r["T1"]
        assert r["describe"]["call"]["mean"] > r["describe"]["put"]["mean"] and r["describe"]["drift_pts"] > 0


def test_null_world_false_positive_rate_is_small():
    fp = 0
    sims = 120
    for s in range(sims):
        r = st.run(*world(seed=200 + s, n_days=1200))
        fp += r["T1"]["pass"] or r["T2"]["pass"]
    assert fp / sims <= 0.06, fp
    neg = st.run(*world(seed=9, n_days=1500, mu=-0.0025))                                         # 往下漂：賣權比較好，方向事先定好，不能算過關
    assert neg["T1"]["t"] < 0 and not neg["T1"]["pass"]


def test_entry_uses_only_data_up_to_the_entry_close():
    chain, fsp, tx = world(seed=3, n_days=600, mu=0.001)
    full = st.build_legs(chain, fsp, tx)[0]
    for t in full[::20]:
        cut = {d: v for d, v in tx.items() if d <= t["entry"]}
        again = st.build_legs(chain, {t["code"]: fsp[t["code"]]}, cut)[0]
        assert len(again) == 1 and again[0]["K"] == t["K"] and again[0]["cost_c"] == pytest.approx(t["cost_c"])


def test_describe_and_streak():
    assert st.longest_loss_streak([-1, -0.5, 0.2, -1, -1, -1, 0.0]) == 3 and st.longest_loss_streak([]) == 0
    r = st.run(*world(seed=4, n_days=1500, mu=0.001), cpi=[])
    d = r["describe"]
    assert d["call"]["n"] == d["put"]["n"] == r["n"] and set(d["by_kind"]) == {"W"} and d["cpi"] == {"n": 0, "call": None, "put": None}
    assert d["recent"]["n"] < r["n"] and d["call"]["mean_capped"] <= d["call"]["mean"] + 1e-12
    text = st.format_report(r)
    assert "T1" in text and "買權" in text and "賣權" in text and "nan" not in text
    small = st.run({}, {}, {})
    assert small["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "樣本不夠" in st.format_report(small)
    chain, fsp, tx = world(seed=5, n_days=60)
    assert 1 <= len(fsp) < 10 and st.run(chain, fsp, tx)["verdict"]["T1"] == "沒有樣本"     # 不到 10 筆
    late = min(d for d, _ in fsp.values()).isoformat()
    assert st.run(*world(seed=4, n_days=1500, mu=0.001), first="2099-01-01")["n"] == 0
    assert all(t["expiry"] >= late for t in st.build_legs(chain, fsp, tx, first=late)[0]) and len(st.build_legs(chain, fsp, tx, first="2099-01-01")[0]) == 0


def test_capped_mean():
    tr = [{"rc": 9.0 if i == 0 else -1.0, "rp": -1.0, "rc_sens": 0.0, "rp_sens": 0.0} for i in range(10)]
    assert st._leg(tr, "c")["mean_capped"] == pytest.approx((5.0 - 9.0) / 10) and st._leg(tr, "c")["mean"] == pytest.approx(0.0)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "call_put_study.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "call_put_study" not in " ".join(names), p
