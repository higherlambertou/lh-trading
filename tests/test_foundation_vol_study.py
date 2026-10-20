"""實驗 1（發現 56）：序列與特徵的定義、決策時點之前才用得到的資料（偷看金絲雀）、樣本外校準、DM、判定門檻、整條管線（植入已知依賴／無關的預測）、匯出格式。
Chronos 的預測在這裡用注入的陣列，不需要 torch。"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import foundation_vol_study as fv
from core.intraday_context import SESSION_MIN, Session, dm_t

ROOT = Path(fv.__file__).resolve().parent.parent
TOD = 1.0 + 0.8 * np.abs(np.linspace(-1, 1, SESSION_MIN)) ** 2          # 開盤與尾盤波動大


def make_world(n_days=700, seed=0, start="2020-03-02"):
    rng = np.random.default_rng(seed)
    d, dates = date.fromisoformat(start) - timedelta(days=1), []
    while len(dates) < n_days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d.isoformat())
    sessions, lvd, price = [], 0.0, 10000.0
    for i in range(n_days):
        lvd = 0.9 * lvd + rng.normal(0, 0.15)
        lvm = np.zeros(SESSION_MIN)
        for t in range(1, SESSION_MIN):
            lvm[t] = 0.98 * lvm[t - 1] + rng.normal(0, 0.08)
        sigma = 0.0004 * np.exp(lvd + lvm) * TOD
        r = sigma * rng.normal(size=SESSION_MIN)
        op0 = price * (1 + rng.normal(0, 0.002))
        cl = op0 * np.exp(np.cumsum(r))
        op = np.concatenate([[op0], cl[:-1]])
        hi, lo = np.maximum(op, cl) * (1 + 1e-4), np.minimum(op, cl) * (1 - 1e-4)
        vol = np.round(200 * sigma / 0.0004 * np.exp(rng.normal(0, 0.5, SESSION_MIN)))
        sessions.append(Session(dates[i], hi, lo, op, cl, vol))
        price = float(cl[-1])
    return sessions


def planted_forecast(sessions, informative=True, noise=0.15, seed=1):
    """與 export_inputs 的決策位置對應的『預測』：informative＝之後 30 分鐘的真實值×雜訊（植入的、偷看未來的特徵）；否則是和未來無關的隨機數。"""
    s = fv.series(sessions)
    A, V = s["a"].ravel() * 1e4, s["v"].ravel()
    idx, p = fv.samples_index(len(sessions))
    p = p[p >= fv.CONTEXT]
    rng = np.random.default_rng(seed)
    if informative:
        rv = np.array([np.nansum(A[q:q + fv.H]) for q in p]) * np.exp(rng.normal(0, noise, len(p)))
        vo = np.array([np.nansum(V[q:q + fv.H]) for q in p]) * np.exp(rng.normal(0, noise, len(p)))
    else:
        rv = np.exp(rng.normal(3, 0.5, len(p)))
        vo = np.exp(rng.normal(8, 0.5, len(p)))
    return {"p": p, "rv": rv, "vol": vo}


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(fv, "WARM_DAYS", 150)
    monkeypatch.setattr(fv, "SPLIT", "2022-06-01")


# ── 序列 ─────────────────────────────────────────────────────────

def test_series_abs_log_returns_and_volume():
    s = make_world(3, seed=1)[0]
    out = fv.series([s])
    a, v = out["a"][0], out["v"][0]
    assert a[0] == pytest.approx(abs(math.log(s.cl[0] / s.op[0]))) and a[5] == pytest.approx(abs(math.log(s.cl[5] / s.cl[4])))                      # 第一分鐘用收盤對開盤
    assert np.array_equal(v, s.vol) and a.shape == (SESSION_MIN,)
    broken = Session(s.date, s.hi, s.lo, s.op, s.cl.copy(), s.vol.copy())
    broken.cl[10] = np.nan
    o2 = fv.series([broken])
    assert np.isnan(o2["a"][0][10]) and np.isnan(o2["a"][0][11]) and np.isnan(o2["v"][0][10]) and np.isfinite(o2["a"][0][12])                    # 缺的分鐘與它的下一分鐘（報酬算不出）都是 NaN


def test_level_and_transform():
    a, v = np.full((2, SESSION_MIN), 0.001), np.full((2, SESSION_MIN), 50.0)
    assert np.allclose(fv.level("rv", a, v), 1e-6) and np.allclose(fv.level("vol", a, v), 50.0) and fv.level("rv", a, v).shape == (2 * SESSION_MIN,)
    assert fv.transform("rv", np.array([1e-6]))[0] == pytest.approx(0.5 * math.log(1e-6)) and fv.transform("vol", np.array([49.0]))[0] == pytest.approx(math.log(50.0))
    assert fv.chronos_feature("rv", np.array([30 * 2.0]))[0] == pytest.approx(math.log(2.0 + 1e-8)) and fv.chronos_feature("vol", np.array([30 * 99.0]))[0] == pytest.approx(math.log(100.0))


def test_prefix_mean_window_rules():
    x = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
    pre = fv.Prefix(x)
    assert pre.mean(np.array([0]), np.array([2]), 1)[0] == 1.5
    assert pre.mean(np.array([0]), np.array([4]), 1)[0] == pytest.approx(7 / 3)                      # 缺值不算進平均
    assert np.isnan(pre.mean(np.array([0]), np.array([4]), 4)[0])                                      # 有效個數不夠
    assert pre.mean(np.array([0]), np.array([4]), 3)[0] == pytest.approx(7 / 3)
    assert np.isnan(pre.mean(np.array([-1]), np.array([3]), 1)[0]) and np.isnan(pre.mean(np.array([2]), np.array([6]), 1)[0])                # 超出資料
    assert np.isnan(pre.mean(np.array([2]), np.array([3]), 1)[0])                                      # 整段缺值


def test_ewma_half_life_and_nan_carry():
    x = np.concatenate([np.zeros(400), np.ones(60)])
    e = fv.ewma(x, 60.0)
    assert e[399] == 0.0 and e[-1] == pytest.approx(1 - 0.5, abs=0.01)                                  # 半衰期 60：60 步之後走完一半
    y = np.array([np.nan, 1.0, np.nan, np.nan, 3.0])
    ey = fv.ewma(y, 60.0)
    lam = 0.5 ** (1 / 60)
    assert np.isnan(ey[0]) and ey[1] == 1.0 and ey[2] == 1.0 and ey[3] == 1.0 and ey[4] == pytest.approx(lam * 1.0 + (1 - lam) * 3.0)           # 缺值沿用前一個


def test_samples_index_layout():
    idx, p = fv.samples_index(3)
    assert idx.shape == (24, 2) and list(idx[0]) == [0, 0] and list(idx[9]) == [1, 1] and p[0] == 30 and p[1] == 60 and p[8] == SESSION_MIN + 30 and p[-1] == 2 * SESSION_MIN + 240


# ── 特徵與目標 ───────────────────────────────────────────────────

def small_features(kind="rv", seed=0, n=30):
    sessions = make_world(n, seed=seed)
    s = fv.series(sessions)
    return sessions, s, fv.features(kind, s["a"], s["v"])


def test_features_match_hand_calculation():
    sessions, s, f = small_features("rv", n=30)
    L = (s["a"] ** 2).ravel()
    d, k = 25, 3
    i = d * len(fv.CHECKPOINTS) + k
    p = d * SESSION_MIN + fv.CHECKPOINTS[k]
    assert f["p"][i] == p and f["day"][i] == d and f["k"][i] == k
    assert f["har1"][i] == pytest.approx(0.5 * math.log(np.mean(L[p - 30:p]) + fv.EPS_RV)) and f["har2"][i] == pytest.approx(0.5 * math.log(np.mean(L[p - 120:p]) + fv.EPS_RV))
    assert f["har3"][i] == pytest.approx(0.5 * math.log(np.mean(L[p - 300:p]) + fv.EPS_RV))
    assert f["y"][i] == pytest.approx(0.5 * math.log(np.mean(L[p:p + 30]) + fv.EPS_RV))                   # 目標＝決策時點之後 30 分鐘
    prev = [np.mean(L[dd * SESSION_MIN + fv.CHECKPOINTS[k]: dd * SESSION_MIN + fv.CHECKPOINTS[k] + 30]) for dd in range(5, 25)]
    assert f["tod"][i] == pytest.approx(0.5 * math.log(np.mean(prev) + fv.EPS_RV))                      # 前 20 個日盤同一窗口的平均
    e = fv.ewma(L)
    assert f["ewma"][i] == pytest.approx(0.5 * math.log(e[p - 1] + fv.EPS_RV))
    assert np.isnan(f["har4"][i]) is np.False_ and np.isnan(f["har4"][1])                                  # 5 天窗口要有 1500 分鐘：第一天沒有


def test_volume_features_use_log_mean_plus_one():
    sessions, s, f = small_features("vol", n=12)
    V = s["v"].ravel()
    i, p = 9 * len(fv.CHECKPOINTS) + 2, 9 * SESSION_MIN + 90
    assert f["har1"][i] == pytest.approx(math.log(np.mean(V[p - 30:p]) + 1)) and f["y"][i] == pytest.approx(math.log(np.mean(V[p:p + 30]) + 1))


def test_target_and_feature_window_validity_thresholds():
    sessions = make_world(8, seed=2)
    s = fv.series(sessions)
    d, k = 6, 1
    p = d * SESSION_MIN + fv.CHECKPOINTS[k]
    i = d * len(fv.CHECKPOINTS) + k
    for n_missing, expect_finite in ((5, True), (6, False)):                                           # 30 分鐘裡缺 5 個＝剛好 25 個有效；缺 6 個＝24 個，剔除
        a2 = s["a"].copy().ravel()
        a2[p:p + n_missing] = np.nan
        f = fv.features("rv", a2.reshape(s["a"].shape), s["v"])
        assert np.isfinite(f["y"][i]) is np.bool_(expect_finite)
    for n_missing, expect_finite in ((6, True), (7, False)):                                           # 過去 30 分鐘特徵：≥ 24 個有效（80%）
        a2 = s["a"].copy().ravel()
        a2[p - n_missing:p] = np.nan
        f = fv.features("rv", a2.reshape(s["a"].shape), s["v"])
        assert np.isfinite(f["har1"][i]) is np.bool_(expect_finite)


@pytest.mark.parametrize("kind", ["rv", "vol"])
def test_features_never_use_data_at_or_after_the_decision_time(kind):
    sessions = make_world(14, seed=3)
    s = fv.series(sessions)
    base = fv.features(kind, s["a"], s["v"])
    for d, k in ((9, 0), (10, 4), (12, 7)):
        i = d * len(fv.CHECKPOINTS) + k
        p = int(base["p"][i])
        a2, v2 = s["a"].copy().ravel(), s["v"].copy().ravel()
        a2[p:], v2[p:] = 7.0, 1e6                                                                         # 偷看金絲雀：把決策時點以後全改成怪值
        f = fv.features(kind, a2.reshape(s["a"].shape), v2.reshape(s["v"].shape))
        for name in ("har1", "har2", "har3", "har4", "ewma", "tod"):
            assert np.array_equal(f[name][i:i + 1], base[name][i:i + 1], equal_nan=True), (kind, d, k, name)
        assert not np.isclose(f["y"][i], base["y"][i])                                                    # 目標當然會變（它就是之後的資料）


# ── 樣本外校準與檢定 ─────────────────────────────────────────────

def test_oos_predict_is_expanding_and_uses_only_earlier_days():
    rng = np.random.default_rng(0)
    days = np.repeat(np.arange(40), 3)
    X = np.column_stack([np.ones(120), rng.normal(size=120)])
    y = 1.0 + 2.0 * X[:, 1] + rng.normal(0, 0.1, 120)
    out = fv.oos_predict(days, y, X, warm=10)
    assert np.isnan(out[:30]).all() and np.isfinite(out[30:]).all()                                      # 前 10 天暖機
    b = np.linalg.lstsq(X[days < 20], y[days < 20], rcond=None)[0]
    assert out[days == 20] == pytest.approx(X[days == 20] @ b)                                           # 第 20 天只用 0～19 天
    y2 = y.copy()
    y2[days >= 25] += 100.0                                                                              # 竄改第 25 天以後的目標：之前的預測不變
    out2 = fv.oos_predict(days, y2, X, warm=10)
    assert np.array_equal(out[days <= 25], out2[days <= 25], equal_nan=True) and not np.allclose(out[days > 25], out2[days > 25])


def test_dm_matches_the_registered_dm_and_signs_and_small_samples():
    rng = np.random.default_rng(1)
    ea, eb = rng.normal(0, 1.2, 300), rng.normal(0, 1.0, 300)
    assert fv.dm(ea ** 2 - eb ** 2) == pytest.approx(dm_t(ea, eb)) and fv.dm(ea ** 2 - eb ** 2) > 0
    assert fv.dm(eb ** 2 - ea ** 2) < 0 and math.isnan(fv.dm(np.ones(29))) and math.isnan(fv.dm(np.zeros(100)))


def test_compare_aggregates_by_day_splits_periods_and_reports_reductions():
    n_days, per = 400, 8
    days = np.repeat(np.arange(n_days), per)
    dates = np.array([(date(2022, 1, 1) + timedelta(days=i)).isoformat() for i in range(n_days)])
    rng = np.random.default_rng(2)
    y = rng.normal(size=len(days))
    base, cand = y - rng.normal(0, 1.0, len(y)), y - rng.normal(0, 0.8, len(y))
    r = st = fv.compare(days, dates, y, base, cand)
    ex = days <= np.searchsorted(dates, fv.SPLIT) - 1 if False else np.array([dates[d] < fv.SPLIT for d in days])
    assert r["explore"]["days"] == len({d for d in days[ex]}) and r["holdout"]["days"] == n_days - r["explore"]["days"]
    mb, mc = np.mean((y[ex] - base[ex]) ** 2), np.mean((y[ex] - cand[ex]) ** 2)
    assert r["explore"]["mse_base"] == pytest.approx(mb) and r["explore"]["reduction"] == pytest.approx(1 - mc / mb) and r["explore"]["dm"] > 2
    ud = np.unique(days[ex])
    diff = np.array([np.mean((y[days == d] - base[days == d]) ** 2 - (y[days == d] - cand[days == d]) ** 2) for d in ud])
    assert r["explore"]["dm"] == pytest.approx(fv.dm(diff))                                              # 以「日」為單位，不是以樣本
    few = fv.compare(days[:100], dates, y[:100], base[:100], cand[:100])
    assert few["holdout"]["dm"] is None and few["holdout"]["reduction"] is None and st is r


def res(ex_dm=3.0, ho_dm=2.0, ex_red=0.05, ho_red=0.04):
    return {"explore": {"dm": ex_dm, "reduction": ex_red}, "holdout": {"dm": ho_dm, "reduction": ho_red}}


def test_passes_thresholds_are_the_registered_ones():
    assert (fv.Z_EXPLORE, fv.Z_HOLDOUT, fv.MIN_REDUCTION, fv.N_TESTS, fv.WARM_DAYS, fv.SPLIT, fv.LAG, fv.H, fv.CONTEXT) == (2.24, 1.645, 0.02, 4, 250, "2025-01-01", 10, 30, 512)
    assert fv.CHECKPOINTS == (30, 60, 90, 120, 150, 180, 210, 240) and fv.WINDOWS == (30, 120, 300, 1500) and (fv.MIN_TARGET_VALID, fv.MIN_VALID_FRAC, fv.TOD_DAYS, fv.TOD_MIN, fv.HALF_LIFE) == (25, 0.8, 20, 15, 60.0)
    assert fv.passes(res()) is True
    assert fv.passes(res(ex_dm=2.23)) is False and fv.passes(res(ex_dm=2.24)) is True
    assert fv.passes(res(ho_dm=1.64)) is False and fv.passes(res(ho_dm=1.645)) is True
    assert fv.passes(res(ex_red=0.019)) is False and fv.passes(res(ho_red=0.019)) is False
    assert fv.passes(res(), need_positive_coef=False) is False and fv.passes(res(), need_positive_coef=True) is True
    assert fv.passes({"explore": {"dm": None, "reduction": None}, "holdout": res()["holdout"]}) is False and fv.passes(res(ex_dm=float("nan"))) is False


# ── 整條管線 ─────────────────────────────────────────────────────

def test_a_planted_informative_forecast_passes_both_tests_and_unrelated_one_does_not(fast):
    sessions = make_world(700, seed=11)
    r = fv.run(sessions, planted_forecast(sessions, informative=True))
    for kind in fv.TARGETS:
        t = r["targets"][kind]
        assert t["pass_T_A"] and t["pass_T_B"] and r["verdict"][kind].startswith("有優勢") and t["coef_chronos_in_har"] > 0 and t["T_B"]["explore"]["reduction"] > 0.1
        assert t["simple_baseline"] in ("last30", "ewma") and t["n"] > 4000 and set(t["mse"]) == {"last30", "ewma", "har", "chronos", "har+chronos"}
    assert r["experiment_1_has_edge"] is True
    r0 = fv.run(sessions, planted_forecast(sessions, informative=False))
    for kind in fv.TARGETS:
        assert not r0["targets"][kind]["pass_T_B"] and not r0["targets"][kind]["pass_T_A"]
    assert r0["experiment_1_has_edge"] is False and all(v == "沒過" for v in r0["verdict"].values())


def test_null_forecasts_rarely_pass_across_worlds(fast):
    passed = 0
    for s in range(4):
        sessions = make_world(600, seed=200 + s)
        r = fv.run(sessions, planted_forecast(sessions, informative=False, seed=300 + s))
        passed += sum(t["pass_T_A"] or t["pass_T_B"] for t in r["targets"].values())
    assert passed == 0


def test_run_simple_baseline_is_chosen_by_explore_period_mse(fast):
    sessions = make_world(600, seed=21)
    r = fv.run(sessions, planted_forecast(sessions, informative=False))
    for kind, t in r["targets"].items():
        pick = min(("last30", "ewma"), key=lambda k: t["mse"][k]["explore"])
        assert t["simple_baseline"] == pick


def test_report_and_json_roundtrip(fast):
    import json
    sessions = make_world(700, seed=31)
    r = fv.run(sessions, planted_forecast(sessions, informative=True))
    json.dumps(r, default=float)
    text = fv.format_report(r)
    assert "已實現波動" in text and "成交量" in text and "T_A" in text and "T_B" in text and "樣本外 MSE" in text and "有優勢 → 可以討論實驗 2" in text
    r["experiment_1_has_edge"] = False
    assert "停（不做實驗 2、3）" in fv.format_report(r)


def test_export_inputs_roundtrip(tmp_path):
    sessions = make_world(5, seed=41)
    path = tmp_path / "in.npz"
    n = fv.export_inputs(sessions, path)
    z = np.load(path)
    idx, p = fv.samples_index(5)
    assert n == int((p >= fv.CONTEXT).sum()) == len(z["p"]) and (z["p"] >= fv.CONTEXT).all() and int(z["context"]) == 512 and int(z["horizon"]) == 30
    s = fv.series(sessions)
    assert np.allclose(z["a"], np.nan_to_num(s["a"].ravel() * 1e4), atol=1e-3) and np.allclose(z["v"], np.nan_to_num(s["v"].ravel()))
    assert list(z["dates"]) == [x.date for x in sessions]
    q = int(z["p"][0])
    assert q - fv.CONTEXT >= 0                                                                         # 脈絡 [p−512, p) 整段都在資料裡，沒有補零的開頭


def test_study_is_not_used_by_trading_paths_and_forecast_script_is_standalone():
    for p in list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "range_suggest.py", "stop_suggest.py", "intraday_context.py", "live_state.py")] \
            + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_market.py")] + [ROOT / "main.py"]:
        assert "foundation_vol_study" not in p.read_text(encoding="utf-8"), p
    src = (ROOT / "scripts" / "chronos_forecast.py").read_text(encoding="utf-8")
    assert "from core" not in src and "import core" not in src and "amazon/chronos-bolt-base" in src and "quantile_levels=[0.5]" in src          # 腳本不依賴交易系統、模型與分位數是登記的


# ── 補強：登記的細節要真的被執行 ─────────────────────────────────

def test_tod_ignores_previous_windows_with_too_few_valid_minutes():
    sessions = make_world(30, seed=5)
    s = fv.series(sessions)
    base = fv.features("rv", s["a"], s["v"])
    a2 = s["a"].copy()
    for dd in range(6, 25):                                                                     # 前 19 個日盤同一窗口只剩 10 個有效分鐘（< 25）：不算進季節平均
        a2[dd, 90:120] = np.nan
        a2[dd, 100:] = a2[dd, 100:]
        a2[dd, 90:100] = s["a"][dd, 90:100]
    f = fv.features("rv", a2, s["v"])
    i = 25 * len(fv.CHECKPOINTS) + 2                                                           # 第 25 天、第 90 分鐘
    assert np.isnan(f["tod"][i]) and np.isfinite(base["tod"][i])                               # 只剩 1 個有效的日子（< 15）→ 不給季節特徵，不拿殘缺的湊


def test_dm_needs_at_least_30_days_and_compare_needs_30_days_per_period():
    rng = np.random.default_rng(3)
    assert math.isnan(fv.dm(rng.normal(size=29))) and not math.isnan(fv.dm(rng.normal(size=30)))
    days = np.repeat(np.arange(120), 2)
    dates = np.array([(date(2024, 12, 1) + timedelta(days=i)).isoformat() for i in range(120)])        # 2025-01-01 之前 31 天、之後 89 天
    y = rng.normal(size=len(days))
    r = fv.compare(days, dates, y, y - rng.normal(0, 1, len(y)), y - rng.normal(0, 0.8, len(y)))
    assert r["explore"]["days"] == 31 and r["holdout"]["days"] == 89 and r["explore"]["dm"] is not None
    cut = days < 100                                                                            # 保留期只剩 69 天；再砍到只有 10 天
    r2 = fv.compare(days[days < 41], dates, y[days < 41], (y - rng.normal(0, 1, len(y)))[days < 41], (y - rng.normal(0, 0.8, len(y)))[days < 41])
    assert r2["holdout"]["days"] == 10 and r2["holdout"]["dm"] is None and r2["holdout"]["reduction"] is None and cut.sum() > 0


def test_compare_splits_exactly_at_the_split_date_and_averages_per_day_not_per_sample():
    n_days = 80
    dates = np.array([(date(2024, 12, 1) + timedelta(days=i)).isoformat() for i in range(n_days)])
    counts = np.where(np.arange(n_days) % 2 == 0, 8, 3)                                         # 每天樣本數不同：以日為單位的平均 ≠ 以樣本為單位
    days = np.repeat(np.arange(n_days), counts)
    rng = np.random.default_rng(4)
    y = rng.normal(size=len(days))
    base, cand = y - rng.normal(0, 1.0, len(y)), y - rng.normal(0, 0.7, len(y))
    r = fv.compare(days, dates, y, base, cand)
    first_holdout = list(dates).index("2025-01-01")
    assert r["explore"]["days"] == first_holdout and r["holdout"]["days"] == n_days - first_holdout                       # 2025-01-01 當天算保留期
    sel = days >= first_holdout
    ud = np.unique(days[sel])
    diff = np.array([np.mean((y[days == d] - base[days == d]) ** 2 - (y[days == d] - cand[days == d]) ** 2) for d in ud])
    assert r["holdout"]["dm"] == pytest.approx(fv.dm(diff))


def test_pick_simple_and_verdict_helpers():
    m = lambda a, b: {"last30": {"explore": a}, "ewma": {"explore": b}}   # noqa: E731
    assert fv.pick_simple(m(1.0, 2.0)) == "last30" and fv.pick_simple(m(2.0, 1.0)) == "ewma" and fv.pick_simple(m(1.0, 1.0)) == "last30"
    assert fv.pick_simple(m(None, 1.0)) == "ewma" and fv.pick_simple(m(1.0, None)) == "last30" and fv.pick_simple(m(None, None)) == "last30"
    assert fv.verdict_of(True, True).startswith("有優勢") and fv.verdict_of(True, False) == "T_A 過關、T_B 沒過" and fv.verdict_of(False, True) == "T_B 過關、T_A 沒過" and fv.verdict_of(False, False) == "沒過"


def test_run_predictors_match_an_independent_calculation(fast):
    sessions = make_world(700, seed=61)
    fc = planted_forecast(sessions, informative=True, noise=0.4, seed=62)
    r = fv.run(sessions, fc)
    s = fv.series(sessions)
    f = fv.features("rv", s["a"], s["v"])
    pos = {int(p): i for i, p in enumerate(fc["p"])}
    chr_ = fv.chronos_feature("rv", np.array([fc["rv"][pos[int(p)]] if int(p) in pos else np.nan for p in f["p"]]))
    cols = [f[c] for c in ("y", "har1", "har2", "har3", "har4", "ewma", "tod")] + [chr_]
    ok = np.all([np.isfinite(c) for c in cols], axis=0)
    days, y, one = f["day"][ok], f["y"][ok], np.ones(ok.sum())
    har = np.column_stack([one, f["har1"][ok], f["har2"][ok], f["har3"][ok], f["har4"][ok], f["tod"][ok]])
    X = {"last30": np.column_stack([one, f["har1"][ok]]), "ewma": np.column_stack([one, f["ewma"][ok]]), "har": har, "chronos": np.column_stack([one, chr_[ok]]), "har+chronos": np.column_stack([har, chr_[ok]])}
    dates = np.array([x.date for x in sessions])
    t = r["targets"]["rv"]
    assert t["n"] == ok.sum()
    pred = {}
    for k, x in X.items():
        pred[k] = fv.oos_predict(days, y, x, warm=150)
        e2 = (y - pred[k]) ** 2
        ex = np.array([dates[d] < "2022-06-01" for d in days]) & np.isfinite(e2)
        ho = np.array([dates[d] >= "2022-06-01" for d in days]) & np.isfinite(e2)
        assert t["mse"][k]["explore"] == pytest.approx(e2[ex].mean()) and t["mse"][k]["holdout"] == pytest.approx(e2[ho].mean()), k
    simple = fv.pick_simple(t["mse"])
    assert t["simple_baseline"] == simple
    exp_a = fv.compare(days, dates, y, pred[simple], pred["chronos"])
    exp_b = fv.compare(days, dates, y, pred["har"], pred["har+chronos"])
    assert t["T_A"]["explore"]["dm"] == pytest.approx(exp_a["explore"]["dm"]) and t["T_B"]["holdout"]["reduction"] == pytest.approx(exp_b["holdout"]["reduction"])
    assert t["chronos_vs_har"]["explore"]["dm"] == pytest.approx(fv.compare(days, dates, y, pred["har"], pred["chronos"])["explore"]["dm"])
    assert t["coef_chronos_in_har"] == pytest.approx(np.linalg.lstsq(X["har+chronos"], y, rcond=None)[0][-1])


def test_a_negatively_related_forecast_improves_but_fails_the_positive_coefficient_rule(fast):
    sessions = make_world(700, seed=71)
    fc = planted_forecast(sessions, informative=True, noise=0.1, seed=72)
    inv = {"p": fc["p"], "rv": 1.0 / (fc["rv"] + 1e-9) * 1e6, "vol": 1.0 / (fc["vol"] + 1e-9) * 1e9}               # 反向：越大代表之後越小
    r = fv.run(sessions, inv)
    for kind in fv.TARGETS:
        t = r["targets"][kind]
        assert t["coef_chronos_in_har"] < 0 and t["T_B"]["explore"]["reduction"] > 0.05 and t["T_B"]["holdout"]["dm"] > 2           # 有改善，但係數是負的
        assert t["pass_T_B"] is False                                                                                      # 照登記的規則：係數要為正才算過關
    assert r["experiment_1_has_edge"] is False


def test_run_requires_both_tests_for_an_edge_and_reports_a_verdict_per_target(fast):
    sessions = make_world(700, seed=81)
    r = fv.run(sessions, planted_forecast(sessions, informative=True))
    assert set(r["verdict"]) == {"rv", "vol"} and r["experiment_1_has_edge"] == any(r["targets"][k]["pass_T_A"] and r["targets"][k]["pass_T_B"] for k in fv.TARGETS)
