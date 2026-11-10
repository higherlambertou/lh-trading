"""夜盤時段檢定（core/night_window_study.py，update.md 發現 81）：品質篩選、時段報酬、探索／保留期不洩漏、帶過去的規則、四個過關條件、
植入已知現象找得到、隨機漫步上的假陽性率、日盤對照、風險描述、只跑一次、研究模組不得被交易路徑引用。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

import core.night_window_study as nw

ROOT = Path(nw.__file__).resolve().parent.parent


def flat_night(price=47000.0, n=840, first=1):
    return [(first + i, price, price, price, price, 1) for i in range(n)]


def bars_from_closes(closes, first=1):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append((first + i, prev, max(prev, c), min(prev, c), c, 1))
        prev = c
    return out


# ── 品質篩選與時段報酬 ──────────────────────────────────────────

def test_windows_tile_the_night_without_gaps_or_overlap():
    spans = [(a, b) for _, a, b, _ in nw.WINDOWS]
    assert spans[0][0] == 1 and spans[-1][1] == 840
    assert all(spans[i][1] + 1 == spans[i + 1][0] for i in range(len(spans) - 1))
    assert [b - a + 1 for a, b in spans] == [120, 270, 150, 180, 120]
    # 時間標籤對得上 seq：15:01＝1、17:00＝120、21:30＝390、00:00＝540、03:00＝720、05:00＝840
    assert [(a, b) for _, a, b, _ in nw.WINDOWS] == [(1, 120), (121, 390), (391, 540), (541, 720), (721, 840)]


def test_clean_night_bar_count_and_jump_boundaries():
    assert nw.clean_night(flat_night(n=600)) and not nw.clean_night(flat_night(n=599))

    def step(factor):                                                              # 第 300 根起整個水位乘上 factor（一根的跳動）
        return bars_from_closes([47000.0] * 300 + [47000.0 * factor] * 400)
    assert nw.clean_night(step(1.0199)) and nw.clean_night(step(0.9801))           # 1.99%：留
    assert not nw.clean_night(step(1.0201)) and not nw.clean_night(step(0.9799))   # 2.01%（漲、跌）：整晚丟掉


def test_window_return_hand_case_and_coverage_boundary():
    bars = [(s, 100.0 + s * 0.1, 0, 0, 100.0 + s * 0.1 + 0.05, 1) for s in range(1, 841)]
    r = nw.window_return(bars, 1, 120)
    assert r == pytest.approx((bars[119][4] / bars[0][1] - 1) * 47000)             # 最後一根收盤 ÷ 第一根開盤
    assert nw.window_return(bars[:96], 1, 120) is not None                         # 120 根的 80% ＝ 96 根：夠
    assert nw.window_return(bars[:95], 1, 120) is None
    gap = [b for b in bars if not (50 <= b[0] <= 60)]                              # 中間缺 11 根：用有的第一根與最後一根
    assert nw.window_return(gap, 1, 120) == pytest.approx(r)
    assert nw.window_return([], 1, 120) is None


def test_night_returns_drops_dirty_nights_and_incomplete_windows():
    good = flat_night()
    short = flat_night(n=500)
    spike = bars_from_closes([47000.0] * 400 + [50000.0] * 440)
    holey = [b for b in good if not (121 <= b[0] <= 180)]                          # W2 缺 60 根（只剩 77.8%）；整晚還有 780 根、不會被品質篩選丟掉
    r = nw.night_returns({"2025-01-02": good, "2025-01-03": short, "2025-01-06": spike, "2025-01-07": holey})
    assert sorted(r) == ["2025-01-02", "2025-01-07"]
    assert set(r["2025-01-02"]) == {"W1", "W2", "W3", "W4", "W5"} and set(r["2025-01-07"]) == {"W1", "W3", "W4", "W5"}
    assert all(v == 0 for v in r["2025-01-02"].values())


# ── 探索期／保留期 ──────────────────────────────────────────────

def dated(n, start, fn):
    d0 = date.fromisoformat(start)
    return {(d0 + timedelta(days=i)).isoformat(): fn(i) for i in range(n)}


def rets_of(rng, n_explore=1100, n_hold=450, drift=None, sd=None, drift_hold=None):
    """直接造各時段報酬（點）：探索期 n_explore 晚（到 2024-12-31）、保留期 n_hold 晚（2025-01-01 起）。"""
    sd = sd or {"W1": 100, "W2": 150, "W3": 120, "W4": 120, "W5": 90}
    drift = drift or {}
    drift_hold = drift if drift_hold is None else drift_hold
    start = (date(2024, 12, 31) - timedelta(days=n_explore - 1)).isoformat()
    ex = dated(n_explore, start, lambda i: {w: float(rng.normal(drift.get(w, 0.0), s)) for w, s in sd.items()})
    ho = dated(n_hold, "2025-01-01", lambda i: {w: float(rng.normal(drift_hold.get(w, 0.0), s)) for w, s in sd.items()})
    return {**ex, **ho}


def test_explore_ignores_the_holdout_period_completely():
    rng = np.random.default_rng(1)
    r1 = rets_of(rng, drift={"W3": 12.0})
    r2 = {d: dict(v) for d, v in r1.items()}
    for d in r2:
        if d >= nw.HOLDOUT_START:
            r2[d] = {w: 1e6 for w in r2[d]}                                           # 把保留期整個改掉
    assert nw.explore(r1) == nw.explore(r2)


def test_period_boundaries_are_inclusive_on_both_sides():
    r = rets_of(np.random.default_rng(21))
    ex = nw.explore(r)
    assert ex["windows"]["W3"]["n"] == 1100 and nw.EXPLORE_END in r                         # 探索期最後一晚（2024-12-31）算探索期
    ds, x = nw.split_by_date(r, "W3", nw.HOLDOUT_START)
    assert len(ds) == 450 and ds[0] == nw.HOLDOUT_START                                     # 保留期第一晚（2025-01-01）算保留期
    assert max(d for d in r if d <= nw.EXPLORE_END) == "2024-12-31" and min(d for d in r if d >= nw.HOLDOUT_START) == "2025-01-01"


def test_planted_window_is_carried_and_others_are_not():
    r = rets_of(np.random.default_rng(2), drift={"W3": 12.0})
    ex = nw.explore(r)
    assert ex["carried"] == ["W3"] and ex["windows"]["W3"]["carry"] and ex["windows"]["W3"]["net"] == pytest.approx(8, abs=4)
    below = nw.explore(rets_of(np.random.default_rng(2), drift={"W3": 3.0}))          # 漂移比成本 4 點還小：不帶
    assert below["carried"] == [] and below["windows"]["W3"]["t_net"] < 1.645
    few = nw.explore({k: v for k, v in list(r.items())[:20]})                         # 樣本太少：不帶
    assert few["carried"] == []


def exact_rets(net_mean, sd, n=400, window="W3"):
    """平均與標準差剛好是指定值的一組（探索期內的日期）：讓 t 恰好落在門檻兩邊。"""
    z = np.random.default_rng(0).normal(size=n)
    z = (z - z.mean()) / z.std(ddof=1)
    start = (date(2024, 12, 31) - timedelta(days=n - 1)).isoformat()
    vals = nw.COST + net_mean + sd * z
    return dated(n, start, lambda i: {window: float(vals[i])})


def test_carry_requires_net_positive_and_t_at_least_1645():
    sd, n = 100.0, 400
    need = 1.6449 * sd / n ** 0.5                                                  # 淨平均要超過這麼多才有 t ≥ 1.645
    above = nw.explore(exact_rets(need * 1.02, sd, n))["windows"]["W3"]
    below = nw.explore(exact_rets(need * 0.98, sd, n))["windows"]["W3"]
    assert above["t_net"] > 1.645 and above["carry"] and below["t_net"] < 1.645 and not below["carry"]
    neg = nw.explore(exact_rets(-5.0, 1.0, n))["windows"]["W3"]                     # 淨為負（t 很負）：不帶
    assert neg["net"] < 0 and not neg["carry"]
    assert nw.explore(exact_rets(need * 2, sd, 29))["windows"]["W3"]["carry"] is False        # 不到 30 晚：不帶


def test_holdout_passes_only_when_all_four_conditions_hold():
    rng = np.random.default_rng(4)
    ctl_zero = {"W3": 0.0}
    good = rets_of(rng, drift={"W3": 14.0})
    h = nw.holdout(good, ["W3"], ctl_zero)
    w = h["windows"]["W3"]
    assert h["m"] == 1 and h["z"] == pytest.approx(1.6449, abs=1e-3) and w["pass"] and w["t_net"] >= h["z"] and w["n"] == 450 and w["first"] > 0 and w["second"] > 0
    # ① t 不夠：保留期沒有漂移
    assert not nw.holdout(rets_of(np.random.default_rng(5), drift={"W3": 14.0}, drift_hold={"W3": 0.0}), ["W3"], ctl_zero)["windows"]["W3"]["pass"]
    # ② 夜數 < 100
    few = rets_of(np.random.default_rng(6), n_hold=90, drift={"W3": 40.0})
    wf = nw.holdout(few, ["W3"], ctl_zero)["windows"]["W3"]
    assert wf["n"] == 90 and wf["t_net"] >= 1.645 and not wf["pass"]
    # ③ 只有前半有漂移：前半 +60、後半 −10（幾乎沒有雜訊），整體 t 很大但後半扣成本後為負
    r3 = rets_of(np.random.default_rng(7), sd={"W3": 1.0})
    ds3 = sorted(d for d in r3 if d >= nw.HOLDOUT_START)
    for k, d in enumerate(ds3):
        r3[d]["W3"] = (60.0 if k < len(ds3) // 2 else -10.0) + float(np.random.default_rng(k).normal(0, 1))
    w3 = nw.holdout(r3, ["W3"], ctl_zero)["windows"]["W3"]
    assert w3["t_net"] >= 1.645 and w3["first"] > 0 and w3["second"] < 0 and not w3["pass"]
    # ③' 只有後半有漂移（對稱）
    r3b = rets_of(np.random.default_rng(8), sd={"W3": 1.0})
    for k, d in enumerate(ds3):
        r3b[d]["W3"] = (-10.0 if k < len(ds3) // 2 else 60.0) + float(np.random.default_rng(k).normal(0, 1))
    w3b = nw.holdout(r3b, ["W3"], ctl_zero)["windows"]["W3"]
    assert w3b["t_net"] >= 1.645 and w3b["first"] < 0 and w3b["second"] > 0 and not w3b["pass"]
    # ④ 日盤對照比它還高（整體上漲造成的）
    assert not nw.holdout(good, ["W3"], {"W3": 30.0})["windows"]["W3"]["pass"]
    assert not nw.holdout(good, ["W3"], {"W3": None})["windows"]["W3"]["pass"]                  # 對照算不出來：不過
    assert nw.holdout(good, [], ctl_zero) == {"m": 0, "z": None, "windows": {}}


def exact_holdout(t_target, n=450, sd=100.0, names=("W3", "W4")):
    """保留期：前後兩半完全一樣（交替 ±sd），淨平均讓 t 剛好是 t_target。"""
    base = np.where(np.arange(n) % 2 == 0, 1.0, -1.0) * sd
    se = float(np.std(base, ddof=1)) / n ** 0.5
    vals = nw.COST + t_target * se + base
    return dated(n, nw.HOLDOUT_START, lambda i: {w: float(vals[i]) for w in names})


def test_holdout_threshold_depends_on_how_many_windows_were_carried():
    r = exact_holdout(1.8)                                                                      # t 1.8：過單一檢定的 1.645、過不了兩個的 1.96
    one = nw.holdout(r, ["W3"], {"W3": 0.0})["windows"]["W3"]
    two = nw.holdout(r, ["W3", "W4"], {"W3": 0.0, "W4": 0.0})["windows"]["W3"]
    assert one["t_net"] == pytest.approx(1.8, abs=0.01) and one["first"] > 0 and one["second"] > 0
    assert one["pass"] and not two["pass"]
    assert nw.holdout(exact_holdout(2.0), ["W3", "W4"], {"W3": 0.0, "W4": 0.0})["windows"]["W3"]["pass"]       # t 2.0 過 1.96
    assert not nw.holdout(exact_holdout(1.5), ["W3"], {"W3": 0.0})["windows"]["W3"]["pass"]                  # t 1.5 連 1.645 都不到


def test_bonferroni_threshold_follows_the_number_carried():
    assert [round(nw.z_for(m), 3) for m in (1, 2, 3, 4, 5)] == [1.645, 1.960, 2.128, 2.241, 2.326]


def test_false_positive_rate_on_random_walk_is_small():
    """隨機漫步（沒有漂移）：整個流程（探索挑、保留期檢）說「過關」的比例要遠低於 5%。"""
    hits = 0
    for seed in range(150):
        r = rets_of(np.random.default_rng(1000 + seed))
        ex = nw.explore(r)
        h = nw.holdout(r, ex["carried"], {w: 0.0 for w in ("W1", "W2", "W3", "W4", "W5")})
        hits += any(v["pass"] for v in h["windows"].values())
    assert hits <= 4, hits                                                           # 150 次裡 ≤ 4（≈ 2.7%）：扣成本 4 點後更難過


def test_power_a_planted_effect_is_found_most_of_the_time():
    hits = 0
    for seed in range(60):
        r = rets_of(np.random.default_rng(2000 + seed), drift={"W3": 30.0})
        ex = nw.explore(r)
        h = nw.holdout(r, ex["carried"], {w: 0.0 for w in ("W1", "W2", "W3", "W4", "W5")})
        hits += bool(h["windows"].get("W3", {}).get("pass"))
    assert hits >= 55, hits                                                          # 平均每晚 +30（淨 +26）、sd 120、保留期 450 晚：偵測力 > 90%（+20 約 85%）


# ── 端到端（K 棒層級：植入、品質篩選、時段、日盤對照）─────────────────────

def synthetic_nights(rng, n=640, drift_w3=0.0, sd_min=0.00005, start="2023-01-02"):
    out = {}
    d0 = date.fromisoformat(start)
    per_min = drift_w3 / 47000.0 / 150                                                # 只放進 W3（150 根）
    for i in range(n):
        steps = rng.normal(0.0, sd_min, 840)
        steps[390:540] += per_min                                                    # seq 391～540
        closes = 47000.0 * np.exp(np.cumsum(steps))
        opens = np.concatenate([[47000.0], closes[:-1]])
        out[(d0 + timedelta(days=i)).isoformat()] = [(k + 1, float(opens[k]), float(max(opens[k], closes[k])), float(min(opens[k], closes[k])), float(closes[k]), 1) for k in range(840)]
    return out


def synthetic_days(rng, dates, sd_min=0.00005, drift_pts_per_150=0.0):
    out = {}
    for d in dates:
        closes = 47000.0 * np.exp(np.cumsum(rng.normal(drift_pts_per_150 / 47000.0 / 150, sd_min, 300)))
        opens = np.concatenate([[47000.0], closes[:-1]])
        out[d] = [(k + 1, float(opens[k]), float(closes[k])) for k in range(300)]
    return out


def test_end_to_end_finds_a_planted_w3_drift():
    rng = np.random.default_rng(11)
    nights = synthetic_nights(rng, drift_w3=14.0)                                     # 2023-01-02 起 640 晚 → 約到 2024-10；保留期不夠，所以把起點往後推
    nights = synthetic_nights(rng, n=1000, drift_w3=14.0, start="2023-01-02")
    r = nw.run(nights, synthetic_days(rng, list(nights)))
    assert r["explore"]["carried"] == ["W3"], r["explore"]["windows"]
    w = r["holdout"]["windows"]["W3"]
    assert w["pass"] and w["mean"] == pytest.approx(14.0, abs=3) and abs(w["day_control"]) < 5
    assert r["verdict"] == "過關：W3" and r["used"] == r["nights"] == 1000 and r["dropped"] == 0
    assert r["holdout_nights"] == sum(1 for d in nights if d >= nw.HOLDOUT_START)
    assert nw.format_report(r).startswith("夜盤時段檢定：1000 晚")


def test_end_to_end_a_rising_day_session_cancels_the_claim():
    """夜盤 W3 有 +14 點漂移，但同樣長度的日盤窗口漲得更多（+40 點）：整體上漲、不是夜盤特有 → 條件④不過。"""
    rng = np.random.default_rng(13)
    nights = synthetic_nights(rng, n=1000, drift_w3=14.0)
    r = nw.run(nights, synthetic_days(rng, list(nights), drift_pts_per_150=40.0))
    w = r["holdout"]["windows"]["W3"]
    assert w["t_net"] >= 1.645 and w["first"] > 0 and w["second"] > 0 and w["day_control"] == pytest.approx(40.0, abs=4)
    assert not w["pass"] and r["verdict"] == "沒過"


def test_end_to_end_null_has_no_carried_window():
    rng = np.random.default_rng(12)
    nights = synthetic_nights(rng, n=1000, drift_w3=0.0)
    r = nw.run(nights, synthetic_days(rng, list(nights)))
    assert r["explore"]["carried"] == [] and r["verdict"] == "沒有帶到第二關的時段" and r["holdout"]["windows"] == {}
    assert "第二關：沒有時段帶過來" in nw.format_report(r) and r["by_year"] == {}


def test_day_control_hand_case_and_cap():
    days = {"2025-01-02": [(k, 100.0, 100.0 + (0.01 if k == 270 else 0)) for k in range(1, 301)],
            "2025-01-03": [(k, 100.0, 100.0) for k in range(1, 301)],
            "2024-12-30": [(k, 100.0, 120.0) for k in range(1, 301)]}                  # 保留期之前：不算
    c = nw.day_control(days, 270, "2025-01-01")
    assert c == pytest.approx(((100.01 / 100 - 1) * 47000 + 0) / 2)
    assert nw.day_control(days, 500, "2025-01-01") == nw.day_control(days, 300, "2025-01-01")     # 超過日盤 300 根就用整個日盤
    assert nw.day_control({}, 100, "2025-01-01") is None
    thin = {"2025-01-02": [(k, 100.0, 101.0) for k in range(1, 50)]}                  # 根數不到 80%：整天不算
    assert nw.day_control(thin, 100, "2025-01-01") is None


def test_risk_description_hand_case():
    x = np.array([10.0, -20.0, 5.0, -30.0, 40.0])
    k = nw.risk([f"d{i}" for i in range(5)], x)
    assert k["worst"] == -30.0 and k["max_drawdown"] == pytest.approx(57.0) and k["sd"] == pytest.approx(float(np.std(x, ddof=1)))
    assert k["p5"] == pytest.approx(float(np.percentile(x, 5))) and nw.risk([], np.array([])) == {}
    k2 = nw.risk(["a", "b"], np.array([-10.0, -5.0]))                                           # 一開始就賠：回落從 0 算起（−14、−23 → 23）
    assert k2["max_drawdown"] == pytest.approx(23.0)


def test_halves_split_at_the_median_date():
    ds = [f"2025-01-{i:02d}" for i in range(1, 11)]
    x = np.array([10.0] * 5 + [-10.0] * 5)
    assert nw._halves(ds, x) == (6.0, -14.0) and nw._halves(ds[:4], x[:4]) == (None, None)


def test_one_sided_t_hand_case():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert nw.one_sided_t(x) == pytest.approx(2.5 / (np.std(x, ddof=1) / 2))                # 樣本標準差（n−1）：3.873


def test_by_year_and_report_with_missing_values():
    r = {"2024-03-01": {"W3": 10.0}, "2024-04-01": {"W3": 20.0}, "2025-01-02": {"W3": -5.0}}
    assert nw.by_year(r, ["W3"]) == {"W3": {"2024": {"n": 2, "mean": 15.0}, "2025": {"n": 1, "mean": -5.0}}}
    assert nw.one_sided_t(np.array([1.0, 1.0])) is None and nw.one_sided_t(np.array([2.0, 2.0, 2.0])) is None       # 樣本太少／沒變異


def test_runs_once_unless_rerun(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(nw, "RESULT_PATH", tmp_path / "r.json")
    called = []
    monkeypatch.setattr(nw, "load_inputs", lambda: called.append(1) or ({}, {}))
    assert nw.main(["run"]) == 0 and len(called) == 1 and (tmp_path / "r.json").exists()
    nw.main(["run"])
    assert len(called) == 1 and "已經跑過一次" in capsys.readouterr().out                  # 第二次不重算
    nw.main(["run", "--rerun"])
    assert len(called) == 2
    assert nw.main([]) == 2
    assert json.loads((tmp_path / "r.json").read_text())["nights"] == 0


def test_not_imported_by_trading_path():
    for sub in ("strategies", "api"):
        for f in (ROOT / sub).glob("*.py"):
            assert "night_window_study" not in f.read_text(encoding="utf-8"), f
    for f in ("main.py", "core/broker.py", "core/risk.py", "core/manual_monitor.py"):
        p = ROOT / f
        if p.exists():
            assert "night_window_study" not in p.read_text(encoding="utf-8"), f
