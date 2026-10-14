"""停損停利建議的校準（core/stop_calibration.py，update.md 發現 33）。

測試方法（專案慣例）：手算得出答案的小例子、與逐筆暴力算法一致、隨機漫步上的已知答案（常態公式）、植入已知現象。"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

import core.stop_calibration as sc


# ── σ ────────────────────────────────────────────────────────────

def test_trailing_sigma_of_alternating_moves_is_hand_computable():
    cl = 100.0 + (np.arange(100) % 2)                       # 變動 +1、−1、+1…（30 個裡 15 個 +1、15 個 −1）→ 平均 0、變異數 30÷29
    s = sc.trailing_sigma(cl)
    assert np.isnan(s[:30]).all() and len(s) == 100
    assert s[30:] == pytest.approx(math.sqrt(30 / 29))


def test_trailing_sigma_of_a_straight_line_is_zero_and_short_series_is_nan():
    assert sc.trailing_sigma(np.arange(100.0))[30:] == pytest.approx(0.0)
    assert np.isnan(sc.trailing_sigma(np.arange(30.0))).all() and np.isnan(sc.trailing_sigma(np.arange(5.0))).all()


def test_missing_closes_invalidate_two_changes_each_and_need_25_valid():
    cl = 100.0 + (np.arange(100) % 2)
    cl[[40, 50]] = np.nan                                    # 兩個孤立的缺值＝4 個無效變動，視窗 [t−29, t] 內剩 26 個有效 → 還算得出來
    s = sc.trailing_sigma(cl)
    assert np.isfinite(s[60]) and np.isfinite(s[45])
    cl[60] = np.nan                                          # 第三個＝6 個無效 → 24 個 < 25 → 這個視窗算不出來
    s = sc.trailing_sigma(cl)
    assert np.isnan(s[65]) and np.isfinite(s[95])            # 缺值移出視窗後又算得出來


def test_sigma_uses_the_sample_standard_deviation():
    cl = np.concatenate([[100.0], 100.0 + np.cumsum(np.where(np.arange(40) % 3 == 0, 3.0, -1.0))])      # 變動 3、−1、−1、3、−1、−1…
    s = sc.trailing_sigma(cl)
    d = np.diff(cl)[10:40]
    assert s[40] == pytest.approx(d.std(ddof=1))


def test_entry_slots():
    assert sc.entry_slots(100, 15).tolist() == list(range(30, 85, 5))        # 30…80：之後 15 格還在時段內
    assert sc.entry_slots(100, 60).tolist() == [30, 35]
    assert len(sc.entry_slots(40, 15)) == 0


def test_gauss_touch_values():
    assert sc.gauss_touch(1.0) == pytest.approx(0.3173, abs=1e-4) and sc.gauss_touch(2.0) == pytest.approx(0.0455, abs=1e-4)
    assert sc.gauss_touch(0.5) == pytest.approx(0.6171, abs=1e-4) and sc.gauss_touch(3.0) == pytest.approx(0.0027, abs=1e-4)


# ── 計數：手算 ───────────────────────────────────────────────────

def flat_session(n=100):
    cl = 100.0 + (np.arange(n) % 2)                          # σ＝√(30÷29)＝1.0171；高低＝收盤，來回只有 1 點，k≥0.5（距離 ≥ 1.137）碰不到
    return cl.copy(), cl.copy(), cl


def test_a_planted_spike_is_counted_exactly():
    hi, lo, cl = flat_session()
    hi[33] = cl[30] + 2.5                                    # 進場格 30（收盤 100）之後第 3 格往上衝 2.5 點
    c = sc.session_counts(hi, lo, cl, 5)
    unit = math.sqrt(30 / 29) * math.sqrt(5)                 # 2.2744
    assert 1.0 * unit < 2.5 < 1.5 * unit                     # 衝到 1σ√T 以上、1.5σ√T 以下
    assert c["n"] == 13                                      # 進場格 30,35,…,90（90＋5 ≤ 99）
    assert c["touch"].tolist() == [1, 1, 0, 0, 0]            # 只有進場格 30 的多單看到的是「往上」＝空單不利；k＝0.5、1 碰到，1.5 以上沒有
    i1, j1, j15 = sc.KS_TP.index(1.0), sc.KS_SL.index(1.0), sc.KS_SL.index(1.5)
    # 停利 1σ、停損 1σ：多單先碰停利（第 3 格）→ 贏；空單的停損（往上 1σ）第 3 格先碰 → 輸
    assert (c["wins"][i1, j1], c["loss"][i1, j1]) == (1, 1)
    # 停利 1σ、停損 1.5σ：多單贏；空單的停損要往上 1.5σ（3.41）→ 沒碰到、停利（往下）也沒碰到 → 未結算
    assert (c["wins"][i1, j15], c["loss"][i1, j15]) == (1, 0)


def test_both_sides_in_the_same_bar_counts_as_the_stop_first():
    hi, lo, cl = flat_session()
    hi[33], lo[33] = cl[30] + 2.5, cl[30] - 2.5              # 同一根 K 棒上下都掃到
    c = sc.session_counts(hi, lo, cl, 5)
    i1, j1 = sc.KS_TP.index(1.0), sc.KS_SL.index(1.0)
    assert (c["wins"][i1, j1], c["loss"][i1, j1]) == (0, 2)  # 多單、空單都算停損先到（保守）
    assert c["touch"].tolist() == [2, 2, 0, 0, 0]


def test_no_signal_when_sigma_is_zero_or_not_enough_valid_changes():
    flat = np.full(100, 100.0)
    assert sc.session_counts(flat.copy(), flat.copy(), flat, 5) is None          # σ＝0：不做
    cl = 100.0 + (np.arange(100) % 2)
    cl[::3] = np.nan                                                             # 缺太多
    assert sc.session_counts(cl.copy(), cl.copy(), cl, 5) is None
    assert sc.session_counts(*flat_session(30), 5) is None                       # 太短，沒有進場格


# ── 計數：與逐筆暴力算法一致 ──────────────────────────────────────

def reference_counts(hi, lo, cl, T):
    """和 session_counts 一模一樣的定義，用最笨的迴圈寫（含 NaN）。"""
    n_entries, touch = 0, [0] * len(sc.KS)
    wins = np.zeros((len(sc.KS_TP), len(sc.KS_SL)), dtype=int)
    loss = np.zeros_like(wins)
    for t in range(sc.FIRST, len(cl) - T, sc.STEP):
        ch = [cl[j] - cl[j - 1] for j in range(t - sc.WINDOW + 1, t + 1)]
        ch = [x for x in ch if math.isfinite(x)]
        if len(ch) < sc.MIN_VALID or not math.isfinite(cl[t]):
            continue
        m = sum(ch) / len(ch)
        sig = math.sqrt(sum((x - m) ** 2 for x in ch) / (len(ch) - 1))
        if not sig > 0:
            continue
        n_entries += 1
        c, unit = cl[t], sig * math.sqrt(T)

        def first(excess_fn, dist):
            for i in range(T):
                if excess_fn(t + 1 + i) >= dist:
                    return i
            return T
        up = lambda b: hi[b] - c                                                # noqa: E731
        dn = lambda b: c - lo[b]                                                # noqa: E731
        for ik, k in enumerate(sc.KS):
            touch[ik] += (first(dn, k * unit) < T) + (first(up, k * unit) < T)
        for i, kt in enumerate(sc.KS_TP):
            for j, ks in enumerate(sc.KS_SL):
                for tp_fn, sl_fn in ((up, dn), (dn, up)):
                    tp, sl = first(tp_fn, kt * unit), first(sl_fn, ks * unit)
                    wins[i, j] += tp < sl
                    loss[i, j] += (sl <= tp) and (sl < T)
    return n_entries, touch, wins, loss


def random_session(rng, n=300, nan_rate=0.0):
    steps = rng.standard_normal(n) * 2.0
    cl = 20000.0 + np.cumsum(steps)
    hi = np.maximum(cl, np.concatenate([[cl[0]], cl[:-1]])) + rng.random(n) * 1.5
    lo = np.minimum(cl, np.concatenate([[cl[0]], cl[:-1]])) - rng.random(n) * 1.5
    if nan_rate:
        gone = rng.random(n) < nan_rate
        cl, hi, lo = cl.copy(), hi.copy(), lo.copy()
        cl[gone] = hi[gone] = lo[gone] = np.nan
    return hi, lo, cl


@pytest.mark.parametrize("T", [5, 15, 30, 60])
@pytest.mark.parametrize("nan_rate", [0.0, 0.03])
def test_session_counts_agree_with_the_brute_force_definition(T, nan_rate):
    rng = np.random.default_rng(7 + T)
    for _ in range(4):
        hi, lo, cl = random_session(rng, nan_rate=nan_rate)
        got = sc.session_counts(hi, lo, cl, T)
        n, touch, wins, loss = reference_counts(hi, lo, cl, T)
        if n == 0:
            assert got is None
            continue
        assert got["n"] == n and got["touch"].tolist() == touch
        assert np.array_equal(got["wins"], wins) and np.array_equal(got["loss"], loss)


# ── 隨機漫步上的已知答案 ──────────────────────────────────────────

def brownian_sessions(n_sessions=150, sub=20, seed=3):
    """每分鐘 sub 個小步的常態隨機漫步；1 分 K 的高低取小步路徑的極值（近似連續監看）。"""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n_sessions):
        steps = rng.standard_normal((sc.DAY_SLOTS, sub)) * (3.0 / math.sqrt(sub))     # 每分鐘標準差 3 點
        path = 20000.0 + np.cumsum(steps.reshape(-1))
        grid = path.reshape(sc.DAY_SLOTS, sub)
        out.append((f"2020-01-{i:03d}", grid.max(axis=1), grid.min(axis=1), grid[:, -1]))
    return out


def test_touch_probability_matches_the_normal_formula_on_a_random_walk():
    cal = sc.calibrate(brownian_sessions(), horizons=(15, 30))
    for T in ("15", "30"):
        for k, emp, gauss in zip(sc.KS, cal[T]["touch"], cal[T]["gauss"]):
            assert emp == pytest.approx(gauss, abs=0.05), (T, k, emp, gauss)
        assert cal[T]["days"] == 150 and cal[T]["n"] > 5000 and not cal[T]["few"]
        assert all(se is not None and 0 < se < 0.02 for se in cal[T]["touch_se"])


def test_symmetric_stops_win_half_the_time_and_every_cell_adds_up():
    cal = sc.calibrate(brownian_sessions(n_sessions=100), horizons=(30,))["30"]
    for kt, ks, w in ((1.0, 1.0, cal["win"][sc.KS_TP.index(1.0)][sc.KS_SL.index(1.0)]), (2.0, 2.0, cal["win"][sc.KS_TP.index(2.0)][sc.KS_SL.index(2.0)])):
        assert w == pytest.approx(0.5, abs=0.03)                                      # 沒有資訊、停利＝停損：勝率 50%
    assert all(0.0 <= u <= 1.0 for row in cal["unres"] for u in row)
    # 距離越遠越不容易在 T 內碰到：未結算比例隨 k 增加
    assert cal["unres"][-1][-1] > cal["unres"][0][0]


def test_a_planted_edge_is_visible_but_noise_is_not():
    """植入：每個進場點之後 T 分鐘價格往上漂移。多單的勝率要明顯高於 50%，（多空平均後）整體勝率要高於沒有漂移的對照。"""
    base = sc.calibrate(brownian_sessions(n_sessions=60, seed=11), horizons=(15,))["15"]
    drift = []
    rng = np.random.default_rng(11)
    for i in range(60):
        steps = rng.standard_normal((sc.DAY_SLOTS, 20)) * (3.0 / math.sqrt(20)) + 0.25 / 20
        grid = (20000.0 + np.cumsum(steps.reshape(-1))).reshape(sc.DAY_SLOTS, 20)
        drift.append((f"d{i}", grid.max(axis=1), grid.min(axis=1), grid[:, -1]))
    got = sc.calibrate(drift, horizons=(15,))["15"]
    i, j = sc.KS_TP.index(1.0), sc.KS_SL.index(1.0)
    assert base["win"][i][j] == pytest.approx(0.5, abs=0.04)
    assert abs(got["win"][i][j] - 0.5) < 0.04                                         # 多空平均後漂移互相抵消：沒有「總是做多」的假優勢
    assert got["touch"][sc.KS.index(1.0)] > base["touch"][sc.KS.index(1.0)] - 0.03    # 漂移讓兩側碰到的機率都不低於無漂移（多空平均）


# ── calibrate 的結構、前後半、樣本少 ─────────────────────────────

def test_calibrate_reports_halves_flags_and_few_samples():
    rng = np.random.default_rng(1)
    sessions = [(f"2020-01-{i:02d}", *random_session(rng)) for i in range(6)]
    few = sc.calibrate(sessions[:4], horizons=(15,))["15"]
    assert few["days"] == 4 and few["n"] == 4 * 51 < sc.MIN_N and few["few"] is True       # 每個時段 51 個進場點（30,35,…,280）
    cal = sc.calibrate(sessions, horizons=(15,))["15"]
    assert cal["days"] == 6 and cal["n"] == 306 and cal["few"] is False                  # 306 ≥ 300
    assert len(cal["touch"]) == len(sc.KS) and len(cal["win"]) == len(sc.KS_TP) and len(cal["win"][0]) == len(sc.KS_SL)
    assert len(cal["half_touch"]) == 2 and len(cal["unstable_touch"]) == len(sc.KS)
    assert len(cal["gauss"]) == len(sc.KS) and cal["gauss"][sc.KS.index(1.0)] == pytest.approx(0.3173, abs=1e-4)


def test_calibrate_flags_exactly_the_cells_whose_halves_disagree_by_more_than_the_limit():
    for f in (0.3, 0.6):                                                              # 後半的時段把高低放大 f 倍（雜訊範圍不變、碰到的機率變大）→ 前後半差距有大有小
        rng = np.random.default_rng(2)
        quiet = [(f"a{i}", *random_session(rng)) for i in range(4)]
        wild = []
        for i in range(4):
            hi, lo, cl = random_session(rng)
            wild.append((f"b{i}", hi + (hi - cl) * f, lo - (cl - lo) * f, cl))
        mixed = sc.calibrate(quiet[:2] + wild[:2], horizons=(15,))["15"]
        gaps = [abs(a - b) for a, b in zip(*mixed["half_touch"])]
        assert any(sc.MAX_HALF_GAP < g < 0.4 for g in gaps) and any(g < sc.MAX_HALF_GAP for g in gaps)      # 這組資料夠敏感：有的格子差一點、有的差很多
        assert mixed["unstable_touch"] == [g > sc.MAX_HALF_GAP for g in gaps]
        flag = lambda a, b: a is None or b is None or abs(a - b) > sc.MAX_HALF_GAP         # noqa: E731（某一半沒有已結算的單＝沒法比，也算不穩）
        assert mixed["unstable_win"] == [[flag(a, b) for a, b in zip(ra, rb)] for ra, rb in zip(*mixed["half_win"])]


def test_calibrate_with_no_usable_session_returns_empty_tables():
    flat = np.full(100, 100.0)
    cal = sc.calibrate([("x", flat, flat, flat)], horizons=(15,))["15"]
    assert cal["n"] == 0 and cal["touch"] == [None] * len(sc.KS) and cal["few"] is True


# ── 讀資料 ───────────────────────────────────────────────────────

@pytest.fixture
def taipei(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("需要 POSIX 的 tzset")
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def flow_1m(self, code):
        return self.rows


def epoch(y, m, d, hh, mm):
    return int(time.mktime((y, m, d, hh, mm, 0, 0, 0, -1)))


def test_flow_sessions_map_minutes_to_night_and_day_slots(taipei):
    rows = [{"ts": epoch(2026, 10, 9, 15, 0), "high": 11.0, "low": 9.0, "close": 10.0},            # 夜盤第 0 格
            {"ts": epoch(2026, 10, 9, 23, 59), "high": 12.0, "low": 8.0, "close": 11.0},           # 第 539 格
            {"ts": epoch(2026, 10, 10, 0, 0), "high": 13.0, "low": 7.0, "close": 12.0},            # 凌晨算前一晚：第 540 格
            {"ts": epoch(2026, 10, 10, 4, 59), "high": 14.0, "low": 6.0, "close": 13.0},           # 第 839 格
            {"ts": epoch(2026, 10, 10, 8, 45), "high": 99.0, "low": 1.0, "close": 50.0},           # 日盤：夜盤模式不收
            {"ts": epoch(2026, 10, 12, 15, 0), "high": 21.0, "low": 19.0, "close": 20.0},          # 下一晚
            {"ts": epoch(2026, 10, 9, 16, 0), "high": None, "low": None, "close": None}]           # 沒成交的分鐘不收
    night = sc.flow_sessions(FakeStore(rows), night=True, min_bars=1)
    assert [d for d, *_ in night] == ["2026-10-09", "2026-10-12"]
    _, hi, lo, cl = night[0]
    assert (cl[0], hi[0], lo[0]) == (10.0, 11.0, 9.0) and cl[539] == 11.0 and cl[540] == 12.0 and cl[839] == 13.0
    assert int(np.isfinite(cl).sum()) == 4 and len(cl) == sc.NIGHT_SLOTS
    day = sc.flow_sessions(FakeStore(rows), night=False, min_bars=1)
    assert [d for d, *_ in day] == ["2026-10-10"] and day[0][3][0] == 50.0 and len(day[0][3]) == sc.DAY_SLOTS


def test_flow_sessions_drop_sessions_with_too_few_minutes(taipei):
    rows = [{"ts": epoch(2026, 10, 9, 15, i), "high": 1.0, "low": 1.0, "close": 1.0} for i in range(10)]
    assert sc.flow_sessions(FakeStore(rows), night=True, min_bars=300) == []
    assert len(sc.flow_sessions(FakeStore(rows), night=True, min_bars=10)) == 1


def test_sigma_ratio_compares_the_same_minutes():
    rng = np.random.default_rng(5)
    hi, lo, cl = random_session(rng)
    other = (cl - cl[0]) * 2.0 + cl[0]                                              # 同樣的走勢、點數放大 2 倍：σ 是 2 倍
    r = sc.sigma_ratio([("d1", hi, lo, cl), ("d2", hi, lo, cl)], [("d1", hi * 2, lo * 2, other)])
    assert r["days"] == 1 and r["n"] > 20 and r["median"] == pytest.approx(2.0) and r["p10"] == pytest.approx(2.0)
    assert sc.sigma_ratio([("d1", hi, lo, cl)], [("zz", hi, lo, cl)]) == {"days": 0, "n": 0, "median": None, "p10": None, "p90": None}


def test_format_report_mentions_every_horizon():
    rng = np.random.default_rng(1)
    txt = sc.format_report("測試", sc.calibrate([(f"d{i}", *random_session(rng)) for i in range(3)], horizons=(5, 15)))
    assert "持有 5 分鐘" in txt and "持有 15 分鐘" in txt and "被雜訊掃到的機率" in txt and "沒有技巧時的勝率" in txt


def test_a_touch_exactly_at_the_distance_counts():
    """σ 剛好是 2（樣本變異數 116÷29＝4）、持有 4 分鐘（√4＝2）→ 1σ√T＝4.0；價格剛好衝到 +4.0，用 ≥ 才算碰到（整數運算，沒有浮點誤差）。"""
    ch = [3, -3] * 5 + [2, -2] * 3 + [1, -1] + [0] * 12
    assert len(ch) == 30 and sum(ch) == 0 and sum(x * x for x in ch) == 116
    cl = np.concatenate([[1000.0], 1000.0 + np.cumsum(ch)])               # 第 0～30 格
    cl = np.concatenate([cl, np.full(5, cl[-1])])                          # 第 31～35 格持平
    hi, lo = cl.copy(), cl.copy()
    hi[32] = cl[30] + 4.0
    assert sc.trailing_sigma(cl)[30] == 2.0
    c = sc.session_counts(hi, lo, cl, 4)
    assert c["n"] == 1 and c["touch"].tolist() == [1, 1, 0, 0, 0]          # k＝0.5（2.0）與 k＝1.0（4.0，剛好）碰到，k＝1.5（6.0）沒有
    i1, j1 = sc.KS_TP.index(1.0), sc.KS_SL.index(1.0)
    assert (c["wins"][i1, j1], c["loss"][i1, j1]) == (1, 1)                # 多單先碰停利、空單的停損先被碰到
