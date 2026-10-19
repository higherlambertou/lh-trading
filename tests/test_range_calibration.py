"""機率範圍帶的校準（core/range_calibration.py，update.md 發現 48）：與逐點暴力算法一致、植入已知分配（常態＋t(29) 估計誤差、肥尾、趨勢）、
樣本外驗證抓得出分配不同、訓練不偷看測試、到收盤的分組與邊界、缺值與空資料。合成資料的價格水準與雜訊接近真實（3 萬點上下、每分鐘幾點）。"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

import core.range_calibration as rc
from core.stop_calibration import FIRST, STEP, WINDOW

N = 300


def make_session(date: str, incs: np.ndarray, start: float = 30000.0, wiggle: float = 0.0, rng: np.random.Generator | None = None):
    """incs＝每分鐘收盤價變動（長度 N−1）。高低＝相鄰兩根收盤的上下界（再加 wiggle 的隨機外擴，模擬分鐘內的震盪）。"""
    cl = start + np.concatenate([[0.0], np.cumsum(incs)])
    prev = np.concatenate([[cl[0]], cl[:-1]])
    hi, lo = np.maximum(prev, cl), np.minimum(prev, cl)
    if wiggle and rng is not None:
        hi = hi + rng.uniform(0, wiggle, N)
        lo = lo - rng.uniform(0, wiggle, N)
    return (date, hi, lo, cl)


def gaussian_sessions(n_days: int, seed: int = 1, sigma: float = 3.0, start_day: str = "2024-01-01") -> list:
    rng = np.random.default_rng(seed)
    d0 = np.datetime64(start_day)
    return [make_session(str(d0 + i), rng.normal(0, sigma, N - 1), wiggle=0.0, rng=rng) for i in range(n_days)]


def t_quantile_abs(level: float, df: int = 29) -> float:
    """|t_df| 的 level 分位數（用大量抽樣近似，固定種子）。"""
    r = np.random.default_rng(7)
    return float(np.quantile(np.abs(r.standard_t(df, 2_000_000)), level))


# ── 暴力算法對照 ─────────────────────────────────────────────────

def brute_sigma(cl: np.ndarray, t: int):
    d = [cl[j] - cl[j - 1] for j in range(t - WINDOW + 1, t + 1)]
    ok = [x for x in d if math.isfinite(x)]
    if len(ok) < 25:
        return None
    m = sum(ok) / len(ok)
    return math.sqrt(sum((x - m) ** 2 for x in ok) / (len(ok) - 1))


def brute(hi, lo, cl, T, end=None):
    """逐點：回傳 {t: (z_term, z_path)}。end＝None 是固定持有 T 分鐘；給數字就是到收盤（剩餘 R＝end−t）。"""
    n = len(cl)
    out = {}
    for t in range(FIRST, n, STEP):
        e = t + T if end is None else end
        R = e - t
        if e > n - 1 or R < 1 or (end is None and not (t < n - T)):
            continue
        s = brute_sigma(cl, t)
        if s is None or s <= 0 or not math.isfinite(cl[t]):
            continue
        unit = s * math.sqrt(R)
        wh = [x for x in hi[t + 1:e + 1] if math.isfinite(x)]
        wl = [x for x in lo[t + 1:e + 1] if math.isfinite(x)]
        zt = abs(cl[e] - cl[t]) / unit if math.isfinite(cl[e]) else float("nan")
        zp = max(max(wh) - cl[t], cl[t] - min(wl)) / unit if wh and wl else float("nan")
        out[t] = (zt, zp)
    return out


def test_vectorised_z_matches_a_pointwise_brute_force():
    rng = np.random.default_rng(3)
    d, hi, lo, cl = make_session("2024-03-01", rng.normal(0, 4, N - 1), wiggle=2.0, rng=rng)
    hi, lo, cl = hi.copy(), lo.copy(), cl.copy()
    for i in (40, 41, 90, 150, 151, 152):                      # 缺值（沒成交的分鐘）
        hi[i] = lo[i] = cl[i] = np.nan
    z = rc.session_z(hi, lo, cl)
    for T in rc.HORIZONS:
        want = brute(hi, lo, cl, T)
        got = {int(s): (a, b) for s, a, b in zip(z[str(T)]["slot"], z[str(T)]["term"], z[str(T)]["path"])}
        assert set(want) == set(got), T
        for t, (a, b) in want.items():
            assert (got[t][0] == pytest.approx(a, nan_ok=True)) and (got[t][1] == pytest.approx(b, nan_ok=True)), (T, t)


def test_close_buckets_match_brute_force_and_split_on_remaining_minutes():
    rng = np.random.default_rng(4)
    d, hi, lo, cl = make_session("2024-03-02", rng.normal(0, 4, N - 1), wiggle=1.0, rng=rng)
    z = rc.session_z(hi, lo, cl)
    want_all = {}
    for t in range(FIRST, N, STEP):
        R = N - 1 - t
        if R >= 15 and R <= 270:
            w = brute(hi, lo, cl, 0, end=N - 1).get(t)
            if w:
                want_all[t] = (R, w)
    got = {}
    for b in rc.BUCKETS:
        zz = z[rc.bucket_key(b)]
        for s, a, p in zip(zz["slot"], zz["term"], zz["path"]):
            R = N - 1 - int(s)
            assert b[0] <= R <= b[1]                             # 每個進場點只落在它的那一組
            got[int(s)] = (R, (a, p))
    assert set(got) == set(want_all) and all(R == want_all[t][0] and got[t][1] == pytest.approx(want_all[t][1]) for t, (R, _) in got.items())
    assert max(R for R, _ in got.values()) <= 270 and min(R for R, _ in got.values()) >= 15


def test_bucket_of_edges():
    assert [rc.bucket_of(x) for x in (14, 15, 30, 31, 60, 61, 120, 121, 270, 271)] == [None, "c15_30", "c15_30", "c31_60", "c31_60", "c61_120", "c61_120", "c121_270", "c121_270", None]


def test_a_hand_computable_session():
    """每分鐘 +1、−1 交替：σ＝√(30/29)；到 t 的收盤固定，T 分鐘後收盤 cl[t+T]；手算 z。"""
    incs = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(N - 1)])
    d, hi, lo, cl = make_session("2024-01-02", incs)
    z = rc.session_z(hi, lo, cl, horizons=(5,), buckets=False)["5"]
    sig = math.sqrt(30 / 29)
    t0 = int(z["slot"][0])
    assert t0 == 30
    assert z["term"][0] == pytest.approx(abs(cl[35] - cl[30]) / (sig * math.sqrt(5)))            # 5 步：+1−1+1−1+1 → 1
    assert z["term"][0] == pytest.approx(1 / (sig * math.sqrt(5)))
    assert z["path"][0] == pytest.approx(1 / (sig * math.sqrt(5)))                                # 期間最多離現價 1 點（t=30 是偶數位置，先往下一格再回來）


# ── 植入已知分配 ─────────────────────────────────────────────────

def test_gaussian_walk_gives_t29_quantiles_and_the_normal_formula_undercovers_slightly():
    """價格是常態隨機漫步時，σ 用 30 筆估，z＝Z÷√(χ²/29)＝t(29)：80% 的 q 約 1.31（常態公式 1.28）、95% 約 2.05（1.96）。"""
    tab = rc.calibrate(gaussian_sessions(400), horizons=(15,), buckets=False)["15"]
    for q, L in zip(tab["term"], rc.LEVELS):
        assert q == pytest.approx(t_quantile_abs(L), rel=0.04), L
    assert tab["term"][1] > rc.gauss_q(0.8) and tab["term"][2] > rc.gauss_q(0.95)
    assert not any(tab["unstable_term"])
    assert tab["path"][1] > tab["term"][1]                                                          # 期間的最遠距離一定比落點寬


def test_fat_tailed_returns_widen_the_tail_quantile_and_the_empirical_band_still_covers():
    rng = np.random.default_rng(5)
    d0 = np.datetime64("2023-01-01")
    sess = [make_session(str(d0 + i), rng.standard_t(2.5, N - 1) * 1.5) for i in range(500)]
    tab = rc.calibrate(sess, horizons=(5,), buckets=False)["5"]
    assert tab["term"][2] > rc.gauss_q(0.95) * 1.05                                                  # 肥尾：95% 比常態公式寬
    hold = rc.holdout_check(sess, split=str(d0 + 250), horizons=(5,))
    cov = [c for c in hold["cells"] if c["type"] == "term" and c["key"] == "5"]
    assert all(c["ok"] for c in cov)                                                                 # 實測的範圍涵蓋率接近名目
    assert cov[2]["gauss_coverage"] < 0.95 - 0.005                                                   # 常態公式的 95% 其實不到 95%


def test_holdout_flags_cells_when_the_future_looks_different_from_the_past():
    """訓練期是隨機漫步、測試期每天有一個持續的漂移（趨勢）：位移隨 T 線性放大，落點分位數被拉高，名目 80% 的範圍涵蓋率明顯掉下來。"""
    rng = np.random.default_rng(6)
    d0 = np.datetime64("2023-01-01")
    train = [make_session(str(d0 + i), rng.normal(0, 3, N - 1)) for i in range(250)]
    test = [make_session(str(d0 + 250 + i), rng.normal(0, 3, N - 1) + rng.choice([-1, 1]) * 2.0) for i in range(250)]    # 每天的漂移 ±2 點／分鐘
    hold = rc.holdout_check(train + test, split=str(d0 + 250), horizons=(60,))
    c80 = next(c for c in hold["cells"] if c["key"] == "60" and c["type"] == "term" and c["level"] == 0.8)
    assert c80["coverage"] < 0.70 and not c80["ok"]
    assert hold["ok"] < hold["total"]


def test_instability_flag_catches_a_regime_change_in_tail_shape():
    rng = np.random.default_rng(8)
    d0 = np.datetime64("2023-01-01")
    a = [make_session(str(d0 + i), rng.normal(0, 3, N - 1)) for i in range(300)]
    b = [make_session(str(d0 + 300 + i), rng.standard_t(2.2, N - 1) * 2.0) for i in range(300)]
    tab = rc.calibrate(a + b, horizons=(5,), buckets=False)["5"]
    assert tab["unstable_term"][2] is True                                                          # 前半常態、後半肥尾：95% 的分位數前後半差很多


def test_training_table_never_depends_on_the_test_period():
    """金絲雀：split 之後的資料怎麼改，訓練表（與用它量的涵蓋率的分子分母）不能因此改變——表只能看 split 以前。"""
    sess = gaussian_sessions(200, seed=11)
    split = sess[120][0]
    t1 = rc.calibrate([s for s in sess if s[0] < split], horizons=(15,))
    changed = [s if s[0] < split else make_session(s[0], np.random.default_rng(99).normal(0, 50, N - 1)) for s in sess]
    t2 = rc.calibrate([s for s in changed if s[0] < split], horizons=(15,))
    assert json.dumps(t1, sort_keys=True) == json.dumps(t2, sort_keys=True)
    h1, h2 = rc.holdout_check(sess, split, (15,)), rc.holdout_check(changed, split, (15,))
    assert h1["train_sessions"] == h2["train_sessions"] == 120
    assert h1["cells"][0]["coverage"] != h2["cells"][0]["coverage"]                                  # 測試期不同，涵蓋率才不同（驗證真的在量測試期）


def test_coverage_of_the_training_period_itself_is_nominal():
    sess = gaussian_sessions(150, seed=12)
    per = [rc.session_z(hi, lo, cl, (15,)) for _, hi, lo, cl in sess]
    tab = rc.calibrate(sess, (15,), buckets=False)["15"]
    for kind in rc.TYPES:
        for c, L in zip(rc.coverage([p["15"] for p in per], kind, tab[kind]), rc.LEVELS):
            assert c["coverage"] == pytest.approx(L, abs=0.002)                                      # 用同一批資料算分位數再量涵蓋率，必然幾乎等於名目


def test_cell_ok_rule():
    assert rc.cell_ok(0.79, 0.001, 0.8) and rc.cell_ok(0.83, None, 0.8)                              # 差 ≤ 3 個百分點
    assert not rc.cell_ok(0.84, None, 0.8) and not rc.cell_ok(0.84, 0.01, 0.8)                       # 差 4 個百分點、標準誤小
    assert rc.cell_ok(0.84, 0.03, 0.8)                                                               # 差 4 個百分點但小於 2 個標準誤（噪音大）
    assert not rc.cell_ok(None, None, 0.8)


# ── 缺值、空資料、輸出 ────────────────────────────────────────────

def test_empty_and_tiny_inputs_give_empty_cells_not_errors():
    tab = rc.calibrate([], horizons=(5,))
    assert tab["5"]["n"] == 0 and tab["5"]["term"] == [None] * 3 and tab["5"]["few"] is True and all(tab["5"]["unstable_term"])
    short = (("2024-01-01", np.full(20, 100.0), np.full(20, 99.0), np.full(20, 99.5)),)
    assert rc.calibrate(short, horizons=(5,))["5"]["n"] == 0
    hold = rc.holdout_check([], horizons=(5,))
    assert hold["ok"] == 0 and hold["total"] > 0 and all(c["coverage"] is None and not c["ok"] for c in hold["cells"])


def test_a_session_with_no_trades_for_long_stretches_is_ignored_where_sigma_is_undefined():
    d, hi, lo, cl = make_session("2024-01-03", np.random.default_rng(1).normal(0, 3, N - 1))
    hi, lo, cl = hi.copy(), lo.copy(), cl.copy()
    hi[60:120] = lo[60:120] = cl[60:120] = np.nan
    z = rc.session_z(hi, lo, cl, horizons=(15,), buckets=False)["15"]
    assert all(not (60 <= s < 120) for s in z["slot"])                                              # 進場那格沒價格的不算


def test_table_is_json_serialisable_with_no_nan():
    tab = rc.calibrate(gaussian_sessions(60, seed=2))
    hold = rc.holdout_check(gaussian_sessions(60, seed=2), split="2024-02-15")
    rc.attach_flags(tab, hold)
    json.dumps({"t": tab, "h": hold}, allow_nan=False)
    assert tab["15"]["holdout_ok_term"] is not None and len(tab["15"]["holdout_ok_term"]) == 3


def test_attach_flags_put_the_result_back_on_the_right_cell():
    tab = rc.calibrate(gaussian_sessions(40, seed=2), horizons=(15,), buckets=False)
    hold = {"cells": [{"key": "15", "type": "term", "level": 0.8, "ok": False}, {"key": "15", "type": "path", "level": 0.95, "ok": True}, {"key": "99", "type": "term", "level": 0.5, "ok": True}]}
    rc.attach_flags(tab, hold)
    assert tab["15"]["holdout_ok_term"] == [None, False, None] and tab["15"]["holdout_ok_path"] == [None, None, True]


def test_night_table_has_no_buckets_and_is_marked_few(tmp_path):
    class Store:
        def bars_1m(self, code): return []
        def flow_1m(self, code): return []
    res = rc.build(Store())
    assert res["day"]["table"]["15"]["n"] == 0 and set(res["night"]["table"]) == {str(T) for T in rc.HORIZONS} and res["night"]["few"] is True
    json.dumps(res, allow_nan=False)
    assert "樣本外驗證" in rc.format_report(res)


def test_levels_match_the_stop_table_sigma_definition():
    import core.stop_suggest as ss
    closes = [30000.0 + (i % 3) * 2 for i in range(31)]
    from core.stop_calibration import trailing_sigma
    a = trailing_sigma(np.array(closes))[-1]
    b, n = ss.sigma_from_closes(closes)
    assert a == pytest.approx(b) and n == 30                                                          # 範圍帶沿用同一個 σ，與建議停損停利不會各算各的


def test_coverage_counts_a_point_exactly_on_the_boundary_as_covered():
    day = {"slot": np.arange(5), "term": np.array([1.0, 2.0, 3.0, 4.0, 5.0]), "path": np.array([1.0, 2.0, 3.0, np.nan, 5.0])}
    c = rc.coverage([day], "term", [3.0, None, 5.0])
    assert c[0]["coverage"] == 0.6 and c[0]["n"] == 5 and c[1] == {"level": 0.8, "coverage": None, "se": None, "n": 0, "days": 0} and c[2]["coverage"] == 1.0
    p = rc.coverage([day], "path", [3.0, 0.0, 0.0])
    assert p[0]["coverage"] == 0.75 and p[0]["n"] == 4                                              # NaN 不算分母
