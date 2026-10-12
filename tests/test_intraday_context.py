"""盤中時段脈絡（core/intraday_context.py）：不偷看未來、迴歸抓得到植入的關係、假陽性率、時段對照、非同步快取。

合成資料的做法：每天的波動 = 基準 × exp(AR(1) 的持續成分 + 當天的衝擊) × 日內 U 形；
早盤與之後共用同一個波動，所以「早盤振幅有助預測之後的振幅」是真的（跟真實市場的波動群聚一樣）。
"""
from __future__ import annotations

import asyncio
import math
from datetime import date, datetime, timedelta

import numpy as np
import pytest

import core.intraday_context as ic
from core.intraday_context import Session


def synth(n_days: int, seed: int, shock_sd: float = 0.0, phi: float = 0.9, persist_sd: float = 0.12) -> list[Session]:
    rng = np.random.default_rng(seed)
    tod = np.interp(np.arange(300), [0, 15, 60, 150, 300], [1.4, 1.7, 1.2, 0.8, 0.7])
    f, out, d0 = 0.0, [], date(2020, 1, 1)
    for i in range(n_days):
        f = phi * f + rng.normal(0, persist_sd)
        g = rng.normal(0, shock_sd) if shock_sd else 0.0
        sigma = 6.0 * math.exp(f + g) * tod
        cl = 20000 + np.cumsum(rng.normal(0, 1, 300) * sigma)
        op = np.concatenate([[20000.0], cl[:-1]])
        wig = np.abs(rng.normal(0, 0.5, 300)) * sigma
        out.append(Session((d0 + timedelta(days=i)).isoformat(), np.maximum(op, cl) + wig, np.minimum(op, cl) - wig, op, cl,
                           np.full(300, 100.0)))
    return out


def flat_day(half_range: float, d: str = "2026-01-01") -> Session:
    """全天高低固定：高 = 10000 + half_range、低 = 10000 − half_range（全日振幅 = 2 × half_range）。"""
    hi, lo = np.full(300, 10000.0 + half_range), np.full(300, 10000.0 - half_range)
    return Session(d, hi, lo, np.full(300, 10000.0), np.full(300, 10000.0), np.full(300, 10.0))


# ── 不偷看未來 ────────────────────────────────────────────────────

def test_ref_ranges_use_only_previous_days():
    days = [flat_day(i + 1) for i in range(30)]                     # 第 i 天的全日振幅 = 2(i+1)
    r = ic.ref_ranges(days)
    assert np.isnan(r[:20]).all()
    assert r[20] == pytest.approx(np.mean([2 * (i + 1) for i in range(20)]))         # 前 20 天（0..19），不含第 20 天自己
    days2 = list(days)
    days2[25] = flat_day(1000)                                       # 改第 25 天
    r2 = ic.ref_ranges(days2)
    assert r2[25] == r[25] and np.array_equal(r2[:26], r[:26], equal_nan=True)       # 第 25 天（和之前）的 R20 完全沒變
    assert r2[26] != r[26]                                           # 之後的才受影響


def test_oos_errors_never_use_the_sample_being_predicted_or_later():
    rng = np.random.default_rng(3)
    X = np.column_stack([np.ones(400), rng.normal(size=400)])
    y = 0.5 * X[:, 1] + rng.normal(0, 0.3, 400)
    base = ic.oos_errors(X, y, warm=100)
    y2 = y.copy()
    y2[300] += 50.0                                                   # 把第 300 個樣本弄成離群值
    moved = ic.oos_errors(X, y2, warm=100)
    assert np.array_equal(base[:200], moved[:200])                    # 第 300 個（含）以前的預測誤差完全沒變（索引 100+200=300 起才變）
    assert not np.array_equal(base[200:], moved[200:])


def test_ols_hac_matches_numpy_and_classic_se_on_iid_data():
    rng = np.random.default_rng(1)
    x = rng.normal(size=3000)
    y = 2.0 + 0.7 * x + rng.normal(0, 0.5, 3000)
    X = np.column_stack([np.ones(3000), x])
    beta, se = ic.ols_hac(X, y)
    slope, icpt = np.polyfit(x, y, 1)
    assert beta == pytest.approx([icpt, slope], abs=1e-9)
    classic = 0.5 / math.sqrt(3000)                                    # iid 時 se(slope) ≈ σ/(√n·sd(x))
    assert se[1] == pytest.approx(classic, rel=0.15)


# ── 檢定的力道與假陽性率 ───────────────────────────────────────────

def test_early_range_is_detected_on_data_with_volatility_clustering():
    sessions = synth(900, seed=11, shock_sd=0.3)            # 試過 8 個種子：t 在 7.8~8.5、誤差降 20~26%，離門檻很遠，不是邊緣
    t = ic.checkpoint_table(sessions, 30)
    y = np.log(t["Y"])
    one = np.ones(len(y))
    eA = ic.oos_errors(np.column_stack([one, np.log(t["R"])]), y)
    eB = ic.oos_errors(np.column_stack([one, np.log(t["R"]), np.log(t["E"])]), y)
    assert ic.dm_t(eA, eB) > 2 * ic.Z_ONE_SIDED and math.sqrt((eB ** 2).mean()) < 0.9 * math.sqrt((eA ** 2).mean())


def test_false_positive_rate_when_early_range_is_shuffled():
    """把早盤振幅在各天之間打亂（斷開關聯）：DM 超過單尾 1% 門檻的比例應該接近 1%，30 次最多 2 次。"""
    over = 0
    for seed in range(30):
        sessions = synth(420, seed=100 + seed, shock_sd=0.2)
        t = ic.checkpoint_table(sessions, 30)
        y = np.log(t["Y"])
        one = np.ones(len(y))
        perm = np.random.default_rng(seed).permutation(len(y))
        eA = ic.oos_errors(np.column_stack([one, np.log(t["R"])]), y, warm=150)
        eB = ic.oos_errors(np.column_stack([one, np.log(t["R"]), np.log(t["E"])[perm]]), y, warm=150)
        over += ic.dm_t(eA, eB) > ic.Z_ONE_SIDED
    assert over <= 2, over


def test_dm_is_zero_for_identical_errors_and_negative_when_the_candidate_is_worse():
    e = np.random.default_rng(0).normal(size=300)
    assert ic.dm_t(e, e) == 0.0
    assert ic.dm_t(e, e * 3) < 0


# ── 模型與預測 ────────────────────────────────────────────────────

def test_fit_recovers_a_positive_early_coefficient_and_ordered_quantiles():
    m = ic.fit_range_model(synth(700, seed=5, shock_sd=0.2), 30)
    assert m is not None and m.c == 30 and m.n > 600
    assert 0.15 < m.b_early < 0.9 and m.b_ref > 0
    assert m.q16 < m.q50 < m.q84


def test_model_band_covers_about_68_percent_of_days():
    sessions = synth(900, seed=21, shock_sd=0.3)
    m = ic.fit_range_model(sessions, 30)
    t = ic.checkpoint_table(sessions, 30)
    inside = 0
    for r, e, y in zip(t["R"], t["E"], t["Y"]):
        band = ic.project(m, float(r), float(e))
        inside += band["lo"] <= y <= band["hi"]
    assert 0.64 <= inside / len(t["Y"]) <= 0.72


def test_fit_needs_enough_days():
    assert ic.fit_range_model(synth(60, seed=1), 30) is None


def test_project_is_ordered_and_increases_with_early_range():
    m = ic.RangeModel(30, 0.6, 0.58, 0.32, -0.38, -0.02, 0.40, 1500)
    a, b = ic.project(m, 500.0, 100.0), ic.project(m, 500.0, 200.0)
    assert a["lo"] < a["mid"] < a["hi"] and b["mid"] > a["mid"]
    assert ic.project(m, 600.0, 100.0)["mid"] > a["mid"]               # R20 大 → 預期也大
    assert a["mid"] == pytest.approx(math.exp(0.6 + 0.58 * math.log(500) + 0.32 * math.log(100) - 0.02), abs=0.06)


# ── 各時段波動倍數 ────────────────────────────────────────────────

def test_profile_uses_the_median_so_one_wild_day_does_not_move_it():
    days = []
    for i in range(9):
        s = flat_day(1.0, f"2026-02-{i + 1:02d}")
        days.append(Session(s.date, s.hi, s.lo, s.op, s.op + 1.0, s.vol))
    wild = flat_day(1.0, "2026-02-20")
    cl = wild.op + 1.0
    cl[:15] = wild.op[:15] + 500.0                                     # 某一天開盤狂飆
    days.append(Session(wild.date, wild.hi, wild.lo, wild.op, cl, wild.vol))
    p = ic.tod_profile(days)
    assert p[0] == pytest.approx(1.0, abs=1e-3) and p[5] == pytest.approx(1.0, abs=1e-3)


def test_profile_finds_the_hot_bucket_and_is_normalised_to_the_day_mean():
    days = []
    for i in range(10):
        s = flat_day(1.0, f"2026-02-{i + 1:02d}")
        cl = s.op + 1.0                                               # 每分鐘 |收−開| = 1
        cl[:15] = s.op[:15] + 3.0                                     # 第一個 15 分鐘 = 3
        days.append(Session(s.date, s.hi, s.lo, s.op, cl, s.vol))
    p = ic.tod_profile(days)
    day_mean = (15 * 3 + 285 * 1) / 300
    assert len(p) == 20 and p[0] == pytest.approx(3 / day_mean, abs=1e-3) and p[1] == pytest.approx(1 / day_mean, abs=1e-3)
    assert ic.tod_profile([]) == []


# ── 讀資料庫 ──────────────────────────────────────────────────────

def make_store(tmp_path, days: dict[str, int]):
    from core.market_store import MarketStore
    st = MarketStore(str(tmp_path / "m.db"))
    for d, n in days.items():
        rows = []
        for k in range(n):                                            # hhmm 是 K 棒結束時間：第 0 根 08:46 結束
            end = 8 * 60 + 46 + k
            rows.append({"date": d, "hhmm": (end // 60) * 100 + end % 60, "open": 100 + k, "high": 101 + k, "low": 99 + k,
                         "close": 100.5 + k, "volume": 5})
        st.upsert_bars_1m("TXF", rows)
    return st


def test_load_sessions_maps_end_time_to_the_start_slot_and_filters(tmp_path):
    st = make_store(tmp_path, {"2026-03-02": 300, "2026-03-03": 100, "2026-03-04": 300})
    got = ic.load_sessions(st, "TXF")
    assert [s.date for s in got] == ["2026-03-02", "2026-03-04"]       # 100 根的殘缺日不納入
    s = got[0]
    assert s.hi[0] == 101 and s.lo[0] == 99 and s.hi[299] == 101 + 299    # 08:46 結束 → 第 0 格；13:45 結束 → 第 299 格
    assert s.bars() == 300
    assert [x.date for x in ic.load_sessions(st, "TXF", before="2026-03-04")] == ["2026-03-02"]       # before 不含當天


# ── describe ─────────────────────────────────────────────────────

MODEL30 = ic.RangeModel(30, 0.6, 0.58, 0.32, -0.38, -0.02, 0.40, 1500)
MODEL60 = ic.RangeModel(60, 0.18, 0.59, 0.35, -0.41, -0.01, 0.39, 1500)
DATA = ic.ContextData("2026-10-12", 500.0, {30: MODEL30, 60: MODEL60}, [1.0 + i / 20 for i in range(20)], 1600, "2026-10-08")
CP = {"30": {"early": 100.0, "post": 40.0}, "60": {"early": 140.0, "post": 25.0}}


def at(h, m, day=12):
    return datetime(2026, 10, day, h, m)


def test_describe_day_bucket_and_minutes_to_close():
    d = ic.describe(DATA, at(10, 20), CP, "day")
    assert d["elapsed"] == 95 and d["bucket"]["idx"] == 6 and d["bucket"]["label"] == "10:15~10:30"
    assert d["bucket"]["mult"] == DATA.profile[6] and d["mins_to_close"] == 205 and d["close_hhmm"] == "13:45"
    assert d["settle_day"] is False and len(d["profile"]) == 20
    last = ic.describe(DATA, at(13, 44), CP, "day")
    assert last["bucket"]["idx"] == 19 and last["mins_to_close"] == 1


def test_describe_settlement_day_closes_at_1330():
    wed = at(11, 0, day=21)                                            # 2026-10-21 是第三個週三
    d = ic.describe(DATA, wed, CP, "day")
    assert d["settle_day"] is True and d["close_hhmm"] == "13:30" and d["mins_to_close"] == 150
    assert ic.describe(DATA, at(11, 0, day=14), CP, "day")["settle_day"] is False        # 第二個週三
    assert ic.is_settlement_day(date(2026, 10, 21)) and not ic.is_settlement_day(date(2026, 10, 22))


def test_describe_night_session_only_has_the_countdown():
    n1 = ic.describe(DATA, datetime(2026, 10, 12, 16, 40), CP, "night")
    assert n1["mins_to_close"] == (24 * 60 - (16 * 60 + 40)) + 300 and n1["close_hhmm"] == "05:00" and "bucket" not in n1
    n2 = ic.describe(DATA, datetime(2026, 10, 13, 3, 30), CP, "night")
    assert n2["mins_to_close"] == 90
    assert ic.describe(DATA, datetime(2026, 10, 12, 14, 0), CP, "other") == {"session": "other", "ready": True}


def test_describe_projection_uses_the_latest_finished_checkpoint():
    before = ic.describe(DATA, at(9, 10), CP, "day")                   # 開盤後 25 分
    assert "proj" not in before and "09:15" in before["proj_note"]
    mid = ic.describe(DATA, at(9, 30), CP, "day")                      # 30 分之後、60 分之前 → 用 30 分模型
    assert mid["proj"]["c"] == 30 and mid["proj"]["early"] == 100.0 and mid["proj"]["done"] == 40.0
    exp = ic.project(MODEL30, 500.0, 100.0)
    assert mid["proj"]["expect"] == exp and mid["proj"]["done_ratio"] == round(40.0 / exp["mid"], 2)
    late = ic.describe(DATA, at(11, 0), CP, "day")                     # 60 分之後 → 用 60 分模型
    assert late["proj"]["c"] == 60 and late["proj"]["expect"] == ic.project(MODEL60, 500.0, 140.0)


@pytest.mark.parametrize("h,m,c", [(9, 14, None), (9, 15, 30), (9, 44, 30), (9, 45, 60)])
def test_describe_checkpoint_boundaries(h, m, c):
    d = ic.describe(DATA, at(h, m), CP, "day")
    assert (d["proj"]["c"] if "proj" in d else None) == c


@pytest.mark.parametrize("r20", [None, 0.0, -5.0])
def test_describe_without_a_usable_reference_range_does_not_predict_or_crash(r20):
    data = ic.ContextData("2026-10-12", r20, {30: MODEL30, 60: MODEL60}, DATA.profile, 1600, None)
    d = ic.describe(data, at(11, 0), CP, "day")
    assert "proj" not in d and "不預測" in d["proj_note"] and "bucket" in d


def test_describe_degrades_gracefully():
    assert "proj" not in ic.describe(DATA, at(11, 0), None, "day")                       # 重啟後沒有完整開盤資料
    assert "不預測" in ic.describe(DATA, at(11, 0), None, "day")["proj_note"]
    assert "proj" not in ic.describe(DATA, at(11, 0), {"30": {"early": None, "post": None}, "60": {"early": 0, "post": 0}}, "day")
    no_model = ic.ContextData("2026-10-12", 500.0, {}, DATA.profile, 50, None)
    assert "proj" not in ic.describe(no_model, at(11, 0), CP, "day") and "bucket" in ic.describe(no_model, at(11, 0), CP, "day")
    not_ready = ic.describe(None, at(11, 0), CP, "day")
    assert not_ready["ready"] is False and not_ready["mins_to_close"] == 165 and "bucket" not in not_ready


# ── 非同步快取 ────────────────────────────────────────────────────

def test_context_is_computed_in_background_then_served_from_cache(monkeypatch):
    calls = []

    def fake_compute(store, code, today):
        calls.append(today)
        return ic.ContextData(today, 500.0, {}, [], 10, None)

    monkeypatch.setattr(ic, "compute", fake_compute)

    async def run():
        c = ic.IntradayContext()
        assert await c.get(object(), "TXF", "2026-10-12") is None            # 第一次：先回 None，背景開始算
        await c._task
        d = await c.get(object(), "TXF", "2026-10-12")
        assert d is not None and d.day == "2026-10-12"
        assert await c.get(object(), "TXF", "2026-10-12") is d and calls == ["2026-10-12"]      # 同一天不重算
        assert await c.get(object(), "TXF", "2026-10-13") is None            # 換日 → 重算
        await c._task
        assert calls == ["2026-10-12", "2026-10-13"]

    asyncio.run(run())


def test_failed_computation_backs_off_instead_of_retrying_every_poll(monkeypatch):
    calls = []

    def boom(store, code, today):
        calls.append(1)
        raise RuntimeError("db locked")

    monkeypatch.setattr(ic, "compute", boom)

    async def run():
        c = ic.IntradayContext()
        assert await c.get(object(), "TXF", "2026-10-12") is None
        await c._task
        for _ in range(5):
            assert await c.get(object(), "TXF", "2026-10-12") is None            # 每 3 秒輪詢一次，不能每次都重算
            await asyncio.sleep(0.01)                                            # 讓背景工作有機會真的跑起來（沒有這行，重算根本不會執行）
            if c._task:
                await c._task
        assert len(calls) == 1
        c._fail_at -= ic.RETRY_SEC + 1                                           # 過了等待時間才再試
        await c.get(object(), "TXF", "2026-10-12")
        await c._task
        assert len(calls) == 2

    asyncio.run(run())


def test_compute_uses_only_days_before_today(tmp_path):
    days = {f"2026-03-{d:02d}": 300 for d in range(1, 29)}
    st = make_store(tmp_path, days)
    data = ic.compute(st, "TXF", "2026-03-20")
    assert data.last_session == "2026-03-19" and data.n_days == 19 and data.r20 is None       # 不到 20 個時段 → 沒有 R20
    data = ic.compute(st, "TXF", "2026-03-29")
    assert data.last_session == "2026-03-28" and data.n_days == 28 and data.r20 == pytest.approx(301.0)
