"""進場因子篩選（update.md 發現 45）：植入的因子要找得到並通過保留期、純隨機漫步不能帶出因子、因子不偷看未來、只跑一次。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest

import core.factor_screen as fs
from core.signal_study import N_MIN


def hhmm_of(i):
    tot = 525 + i + 1
    return (tot // 60) * 100 + tot % 60


def weekdays(start, n):
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def synth(n_days, seed, momentum=0.0, sigma=0.0004, start=date(2024, 3, 1)):
    """隨機漫步；momentum > 0 時，每分鐘報酬多加 momentum ×（前 15 分鐘報酬 ÷ 15）——r15 會預測之後的方向。"""
    rng = np.random.default_rng(seed)
    rows, px = [], 47000.0
    for ds in weekdays(start, n_days):
        c = np.empty(N_MIN)
        prev = px
        for i in range(N_MIN):
            r = rng.normal(0, sigma)
            if momentum and i >= 16:
                r += momentum * (c[i - 1] / c[i - 16] - 1) / 15
            c[i] = prev * (1 + r)
            rows.append({"date": ds, "hhmm": hhmm_of(i), "open": prev, "high": max(prev, c[i]) * 1.00003, "low": min(prev, c[i]) * 0.99997,
                         "close": c[i], "volume": float(300 * np.exp(rng.normal(0, 0.3)))})
            prev = c[i]
        px = c[-1]
    return rows


def test_a_planted_momentum_factor_is_found_and_survives_the_holdout():
    res = fs.run(synth(420, seed=1, momentum=0.3))                      # 2024-03 起約 420 個交易日：一半以上在 2025 前
    assert res["stage1"]["r15/15"]["pass"] and "r15/15" in res["carried"]
    assert res["stage2"]["r15/15"]["pass"] and res["stage1"]["r15/15"]["sign"] == 1
    assert "有線索" in res["verdict"]
    json.dumps(res, default=float)
    assert "r15/15" in fs.format_report(res)


def test_a_random_walk_carries_nothing():
    res = fs.run(synth(420, seed=2))
    assert res["carried"] == [] and res["passes"] == [] and "沒有因子過關" in res["verdict"]


def test_features_never_look_ahead():
    p = fs.build_panel(synth(40, seed=3))
    f = fs.features(p)
    d, m = 30, 150
    p2 = fs.build_panel(synth(40, seed=3))
    p2.c[d, m + 1:] *= 1.5
    p2.h[d, m + 1:] *= 1.5
    p2.l[d, m + 1:] *= 0.5
    p2.v[d, m + 1:] *= 9
    p2.o[d, m + 1:] *= 1.5
    f2 = fs.features(p2)
    for k in fs.FACTORS:
        a, b = f[k][d, :m + 1], f2[k][d, :m + 1]
        assert np.allclose(a, b, equal_nan=True), k
        if k.startswith(("prev", "gap")):
            continue
    # 前一天的因子只能用前一天：改今天的資料不影響
    assert np.allclose(f["prev_ret"][d], f2["prev_ret"][d], equal_nan=True)


def test_registered_constants():
    assert fs.T1 == 3.08 and fs.SPLIT == "2025-01-01" and len(fs.FACTORS) * len(fs.HORIZONS) == 24 and fs.POINTS_M[0] == 14 and fs.POINTS_M[-1] == 284


def test_entry_is_the_next_open_and_exit_is_the_close_h_minutes_later():
    p = fs.build_panel(synth(5, seed=4))
    f = fs.features(p)
    ds, x, y = fs.samples(p, f, 15)
    i = len(p.dates) + 2                                    # 第二個決策時點（第 29 分鐘）的某一天；第 14 分鐘還沒有 r15
    d, m = ds[i], fs.POINTS_M[i // len(p.dates)]
    assert y[i] == pytest.approx(p.c[d, m + 15] / p.o[d, m + 1] - 1)
    assert x["r15"][i] == pytest.approx(p.c[d, m] / p.c[d, m - 15] - 1)


def test_vol15_is_relative_to_the_previous_twenty_days_only():
    p = fs.build_panel(synth(30, seed=5))
    f = fs.features(p)
    from core import signal_rules as sr
    w = np.vstack([sr.rolling_sum(p.v[d], 15) for d in range(len(p.dates))])
    d, m = 25, 100
    assert f["vol15"][d, m] == pytest.approx(w[d, m] / np.median(w[d - 20:d, m]))


def test_the_trade_must_beat_the_cost_in_discovery(monkeypatch):
    monkeypatch.setattr(fs, "COST_PTS", 1e6)
    res = fs.run(synth(420, seed=1, momentum=0.3))
    assert res["stage1"]["r15/15"]["t"] >= fs.T1 and res["carried"] == []          # 顯著但賺不過成本 → 不帶到第二關


def test_holdout_net_is_gross_minus_cost():
    res = fs.run(synth(420, seed=1, momentum=0.3))
    s = res["stage2"]["r15/15"]
    assert s["mean_net_pts"] == pytest.approx(s["mean_gross_pts"] - fs.COST_PTS)


def test_runs_only_once(monkeypatch, tmp_path, capsys):
    path = tmp_path / "r.json"
    monkeypatch.setattr(fs, "RESULT_PATH", path)
    path.write_text(json.dumps(fs.run(synth(60, seed=6)), default=float))
    import core.market_store as ms

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("不該再讀資料庫")

    monkeypatch.setattr(ms, "MarketStore", Boom)
    assert fs.main(["run"]) == 0 and "只跑一次" in capsys.readouterr().out


# ── 發現 46：單一假設 ─────────────────────────────────────────

def drift_walk(n_days, seed, drift=0.0, start=date(2024, 11, 1), sigma=0.0004):
    """每分鐘報酬加 drift（整體上漲的漂移）的隨機漫步。"""
    rows = synth(n_days, seed, 0.0, sigma, start)
    if drift:
        by, px, last = {}, 47000.0, None
        for r in rows:
            by.setdefault(r["date"], []).append(r)
        k = 0
        for ds, rs in by.items():
            for r in rs:
                f = (1 + drift) ** k
                for key in ("open", "high", "low", "close"):
                    r[key] *= f
                k += 1
    return rows


def test_registered_single_hypothesis_constants():
    assert fs.T_SINGLE == 1.645 and (fs.RP_HI, fs.RP_LO) == (0.8, 0.2) and fs.COSTS_PTS == {"micro": 4.0, "mini": 1.5, "large": 0.5}


def test_a_planted_intraday_trend_passes():
    res = fs.vwap_trend(synth(600, seed=11, momentum=1.0, sigma=0.00012))
    assert res["passes"] and res["mean_gross_pts"] > res["always_long_gross_pts"] and res["t"] >= 1.645
    assert res["net_pts_by_contract"]["mini"] > res["net_pts_by_contract"]["micro"] > res["control_net_pts"]
    assert "有線索" in res["verdict"] and "保留期" in fs.format_vwap_report(res)
    json.dumps(res, default=float)


def test_a_random_walk_does_not_pass():
    assert fs.vwap_trend(synth(420, seed=12))["passes"] is False


def test_a_market_drift_alone_is_not_a_factor():
    """整體上漲的漂移會讓『做多』賺錢；規則的毛報酬沒有超過一律做多，就不能算過關。"""
    res = fs.vwap_trend(drift_walk(420, seed=13, drift=0.00002))
    assert res["always_long_gross_pts"] > 20 and res["passes"] is False


def test_one_trade_per_day_held_to_the_close():
    res = fs.vwap_trend(synth(420, seed=14, momentum=0.4))
    assert res["n"] <= res["n_days"] and 1 <= res["mean_entry_min"] <= 285


def test_vwap_runs_only_once(monkeypatch, tmp_path, capsys):
    path = tmp_path / "v.json"
    monkeypatch.setattr(fs, "VWAP_RESULT_PATH", path)
    path.write_text(json.dumps(fs.vwap_trend(synth(420, seed=15, momentum=0.4)), default=float))
    import core.market_store as ms

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("不該再讀資料庫")

    monkeypatch.setattr(ms, "MarketStore", Boom)
    assert fs.main(["vwap"]) == 0 and "只跑一次" in capsys.readouterr().out


def test_direction_truth_table():
    d = fs.direction_of
    assert d(0.001, 0.8) == 1 and d(0.001, 0.79) == 0 and d(-0.001, 0.2) == -1 and d(-0.001, 0.21) == 0
    assert d(0.001, 0.1) == 0 and d(-0.001, 0.9) == 0 and d(0.0, 0.9) == 0                      # 位置與 VWAP 方向不一致、或剛好等於 VWAP：不進


def test_every_registered_condition_is_required():
    ok = dict(n=100, t=1.645, h1=1.0, h2=1.0, gross=5.0, always_long=4.0)
    assert fs.single_pass(**ok) is True
    for k, bad in dict(n=99, t=1.64, h1=0.0, h2=-1.0, gross=4.0).items():
        assert fs.single_pass(**{**ok, k: bad}) is False, k
    assert fs.single_pass(**{**ok, "t": float("nan")}) is False


def test_the_primary_net_uses_the_micro_cost():
    r = fs.vwap_trend(synth(600, seed=11, momentum=1.0, sigma=0.00012))
    assert r["mean_net_pts"] == pytest.approx(r["mean_gross_pts"] - 4.0, abs=1e-6)
    assert r["net_pts_by_contract"]["micro"] == pytest.approx(r["mean_net_pts"]) and r["net_pts_by_contract"]["large"] - r["net_pts_by_contract"]["micro"] == pytest.approx(3.5)


def test_the_entry_point_is_at_the_end_of_the_module():
    """入口放在中間會在後面的常數定義之前就執行（python -m 時 NameError）——測試用 import 看不出來。"""
    src = open(fs.__file__, encoding="utf-8").read()
    assert src.rstrip().endswith("sys.exit(main())") and src.count('if __name__ == "__main__"') == 1
