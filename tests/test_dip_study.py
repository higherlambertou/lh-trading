"""「跌／漲 1% 之後的前瞻報酬」（發現 57）：定義、視窗、探索／保留期的界線、OLS＋Newey-West 的數字、方向（Call／Put 邏輯）、判定門檻、植入已知漂移會過關、有波動群聚的隨機漫步不會（假陽性率）、偷看金絲雀、載入。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import dip_study as ds
from core.intraday_context import ols_hac
from intel_fixture import make_db, obs

ROOT = Path(ds.__file__).resolve().parent.parent


def bdays(n, start="1999-02-01"):
    d, out = date.fromisoformat(start) - timedelta(days=1), []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def world(n=6800, seed=0, plant_dip=0.0, plant_rally=0.0, h=120, vol=0.008):
    """有波動群聚的隨機漫步。plant_dip：每個跌日（≤ −1%）之後 h 天總共多出這麼多個百分點的漂移（正＝反彈）；plant_rally 同理給漲日。"""
    rng = np.random.default_rng(seed)
    lv = 0.0
    extra = np.zeros(n + h + 2)
    r = np.zeros(n)
    for t in range(1, n):
        lv = 0.97 * lv + rng.normal(0, 0.12)
        r[t] = vol * np.exp(lv) * rng.normal() + extra[t]
        if plant_dip and r[t] <= -0.01:
            extra[t + 1:t + 1 + h] += plant_dip / 100.0 / h
        if plant_rally and r[t] >= 0.01:
            extra[t + 1:t + 1 + h] += plant_rally / 100.0 / h
    close = 5000.0 * np.exp(np.cumsum(r))
    return bdays(n), close


# ── 定義 ─────────────────────────────────────────────────────────

def test_returns_forward_and_drawdown_definitions():
    c = np.array([100.0, 99.0, 101.0, 97.0, 104.0, 100.0])
    r = ds.daily_returns(c)
    assert np.isnan(r[0]) and r[1] == pytest.approx(-0.01) and r[3] == pytest.approx(97 / 101 - 1)
    f = ds.forward_returns(c, 2)
    assert f[0] == pytest.approx(1.0) and f[1] == pytest.approx((97 / 99 - 1) * 100) and f[3] == pytest.approx((100 / 97 - 1) * 100) and np.isnan(f[4]) and np.isnan(f[5])
    assert np.isnan(ds.forward_returns(c, 6)).all()
    dd = ds.forward_drawdown(c, 3)
    assert dd[0] == pytest.approx((97 / 100 - 1) * 100) and dd[1] == pytest.approx((97 / 99 - 1) * 100) and dd[2] == pytest.approx((97 / 101 - 1) * 100) and np.isnan(dd[3])    # 進場日不算、視窗內最低收盤


def test_event_thresholds_include_the_boundary_and_ignore_nan():
    r = np.array([np.nan, -0.01, -0.0099, 0.0099, 0.01, 0.02, -0.5])
    assert list(ds.event_mask(r, "dip")) == [False, True, False, False, False, False, True]
    assert list(ds.event_mask(r, "rally")) == [False, False, False, False, True, True, False]


def test_period_masks_respect_the_explore_end_and_holdout_start():
    dates = ["2018-12-27", "2018-12-28", "2018-12-31", "2019-01-02", "2019-01-03", "2019-01-04", "2019-01-07"]
    c = np.arange(100.0, 107.0)
    fwd = ds.forward_returns(c, 2)
    m = ds.period_masks(dates, 2, fwd)
    assert list(m["explore"]) == [True, False, False, False, False, False, False]                     # 前瞻視窗最後一天 ≤ 2018-12-31：只有第 0 天（視窗到 12/31）
    assert list(m["holdout"]) == [False, False, False, True, True, False, False]                       # 事件日 ≥ 2019-01-01 且有完整視窗（最後兩天沒有）
    assert not (m["explore"] & m["holdout"]).any()


# ── 統計量 ───────────────────────────────────────────────────────

def test_dummy_excess_equals_mean_difference_and_the_registered_hac():
    rng = np.random.default_rng(1)
    d = rng.random(500) < 0.2
    y = rng.normal(0, 5, 500) + 6.0 * d
    beta, t = ds.dummy_excess(y, d, 20)
    X = np.column_stack([np.ones(500), d.astype(float)])
    b, se = ols_hac(X, y, 20)
    assert beta == pytest.approx(y[d].mean() - y[~d].mean()) and beta == pytest.approx(b[1]) and t == pytest.approx(b[1] / se[1]) and t > 5
    assert ds.dummy_excess(y, np.zeros(500, bool), 20) is None and ds.dummy_excess(y, rng.random(500) < 0.01, 20) is None                  # 事件太少不檢定
    assert ds.dummy_excess(y[:30], np.arange(30) < 25, 5) is None                                                                         # 對照太少也不


def test_episodes_grouping():
    assert ds.episodes(np.array([], int)) == 0 and ds.episodes(np.array([3])) == 1
    assert ds.episodes(np.array([1, 2, 3, 10, 11, 30])) == 3
    assert ds.episodes(np.array([1, 6])) == 1 and ds.episodes(np.array([1, 7])) == 2                    # 相隔 5 個交易日還算同一波、6 個就不是


def res(ex_t=3.0, ho_t=2.0, ex_x=6.0, ho_x=6.0):
    return {"explore": {"signed_t": ex_t, "signed_excess": ex_x}, "holdout": {"signed_t": ho_t, "signed_excess": ho_x}}


def test_passes_thresholds_are_the_registered_ones():
    assert (ds.THRESH, ds.HORIZONS, ds.EXPLORE_END, ds.HOLDOUT_START, ds.Z_EXPLORE, ds.Z_HOLDOUT, ds.MIN_EXCESS, ds.N_TESTS, ds.GROUP_GAP) == \
        (0.01, (60, 120), "2018-12-31", "2019-01-01", 2.24, 1.645, {60: 3.0, 120: 5.0}, 4, 5)
    assert ds.CRISIS_YEARS == (2000, 2001, 2002, 2008, 2020, 2022) and ds.DIRECTIONS == ("dip", "rally")
    assert ds.passes(res(), 120) is True
    assert ds.passes(res(ex_t=2.23), 120) is False and ds.passes(res(ex_t=2.24), 120) is True
    assert ds.passes(res(ho_t=1.64), 120) is False and ds.passes(res(ho_t=1.645), 120) is True
    assert ds.passes(res(ex_x=4.99), 120) is False and ds.passes(res(ex_x=5.0), 120) is True and ds.passes(res(ho_x=4.99), 120) is False
    assert ds.passes(res(ex_x=2.99, ho_x=2.99), 60) is False and ds.passes(res(ex_x=3.0, ho_x=3.0), 60) is True                                      # 60 日門檻是 3、120 日是 5
    assert ds.passes(res(ex_x=4.0, ho_x=4.0), 120) is False and ds.passes(res(ex_x=4.0, ho_x=4.0), 60) is True
    assert ds.passes({"explore": {"signed_t": None, "signed_excess": None}, "holdout": res()["holdout"]}, 60) is False


# ── 方向與整條管線 ───────────────────────────────────────────────

def test_a_planted_rebound_after_dips_passes_the_call_test_and_not_the_put_test():
    dates, close = world(plant_dip=12.0, seed=11)
    r = ds.run(dates, close)
    for h in ds.HORIZONS:
        t = r["tests"][f"dip/{h}"]
        assert t["pass"] and t["explore"]["signed_excess"] > ds.MIN_EXCESS[h] and t["explore"]["signed_t"] > 2.24 and t["holdout"]["signed_t"] > 1.645 and t["explore"]["excess"] > 0
        assert not r["tests"][f"rally/{h}"]["pass"]
    assert r["necessary_condition_holds"] is True and r["verdict"]["dip/120"] == "過關"


def test_a_planted_fade_after_rallies_passes_the_put_test_with_the_sign_flipped():
    dates, close = world(plant_rally=-12.0, seed=12)
    r = ds.run(dates, close)
    t = r["tests"]["rally/120"]
    assert t["pass"] and t["explore"]["excess"] < 0 and t["explore"]["signed_excess"] == pytest.approx(-t["explore"]["excess"]) and t["explore"]["signed_t"] == pytest.approx(-t["explore"]["t"])
    assert not r["tests"]["dip/120"]["pass"]


def test_a_significant_effect_below_the_practical_bar_still_fails(monkeypatch):
    dates, close = world(plant_dip=12.0, seed=11)
    ok = ds.run(dates, close)["tests"]["dip/120"]
    assert ok["pass"] and ok["explore"]["signed_t"] > ds.Z_EXPLORE
    monkeypatch.setattr(ds, "MIN_EXCESS", {60: 500.0, 120: 500.0})                           # 統計上顯著，但幅度不夠大＝不過關
    low = ds.run(dates, close)["tests"]["dip/120"]
    assert low["explore"]["signed_t"] == pytest.approx(ok["explore"]["signed_t"]) and low["pass"] is False


def test_random_walks_with_volatility_clustering_rarely_pass():
    passed, big_t = 0, []
    for s in range(8):
        dates, close = world(seed=100 + s)
        r = ds.run(dates, close)
        passed += sum(t["pass"] for t in r["tests"].values())
        big_t += [abs(t["explore"]["signed_t"]) for t in r["tests"].values() if t["explore"]["signed_t"] is not None]
    assert passed == 0 and np.mean(np.array(big_t) > 2.24) <= 0.25                          # 32 次檢定、都要過三關：不該有


def test_describe_numbers_and_crisis_exclusion():
    dates, close = world(n=3000, seed=21, plant_dip=10.0)
    d = ds.run(dates, close)["descriptive"]["dip/60"]
    r, fwd = ds.daily_returns(close), ds.forward_returns(close, 60)
    ev, ok = ds.event_mask(r, "dip"), np.isfinite(ds.forward_returns(close, 60))
    assert d["n_events"] == int((ev & ok).sum()) and d["events_fwd"]["P50"] == pytest.approx(np.percentile(fwd[ev & ok], 50)) and d["events_positive"] == pytest.approx((fwd[ev & ok] > 0).mean())
    assert d["episodes"] == ds.episodes(np.flatnonzero(ev)) and d["other_fwd"]["P90"] == pytest.approx(np.percentile(fwd[~ev & ok], 90))
    yrs = np.array([int(x[:4]) for x in dates])
    keep = ok & ~np.isin(yrs, ds.CRISIS_YEARS)
    assert d["ex_crisis_excess"] == pytest.approx(ds.dummy_excess(fwd[keep], ev[keep], 60)[0]) and d["full_excess"] == pytest.approx(ds.dummy_excess(fwd[ok], ev[ok], 60)[0])
    assert all(v["n"] >= 3 for v in d["by_year"].values()) and d["events_drawdown_mean"] < d["other_drawdown_mean"] + 5


def test_nothing_is_computed_from_data_after_the_forward_window():
    dates, close = world(n=800, seed=31)
    base = ds.forward_returns(close, 60)
    t = 300
    tampered = close.copy()
    tampered[t + 61:] *= 3.0                                                              # 視窗以後的收盤全改掉：第 t 天的前瞻報酬不變
    assert ds.forward_returns(tampered, 60)[t] == base[t]
    assert not np.isclose(ds.forward_returns(tampered, 60)[t + 5], base[t + 5])
    r0, r1 = ds.daily_returns(close), ds.daily_returns(tampered)
    assert np.array_equal(r0[:t + 61], r1[:t + 61], equal_nan=True)                       # 事件的判斷只用當天與前一天


def test_report_and_json_roundtrip():
    dates, close = world(n=6000, seed=41, plant_dip=12.0)
    r = ds.run(dates, close)
    json.dumps(r, default=float)
    text = ds.format_report(r)
    assert "dip/60" in text and "rally/120" in text and "拿掉危機年" in text and "必要條件成立" in text and "每年" in text
    r["necessary_condition_holds"] = False
    assert "四個都沒過關 → 停" in ds.format_report(r)
    dates0, close0 = world(n=400, seed=42)                                                 # 太短：保留期沒有任何樣本，欄位顯示破折號、不崩、判定是沒過
    r0 = ds.run(dates0, close0)
    assert r0["tests"]["dip/60"]["holdout"]["signed_t"] is None and set(r0["verdict"].values()) == {"沒過"} and "—" in ds.format_report(r0)


# ── 載入 ─────────────────────────────────────────────────────────

def test_load_taiex_takes_latest_revision_skips_blanks_and_is_read_only(tmp_path):
    c = make_db(tmp_path / "intel.db")
    for d, v, rev in (("2026-10-15", 47000.0, 0), ("2026-10-16", 46000.0, 0), ("2026-10-16", 46100.0, 1), ("2026-10-19", 46822.08, 0)):
        obs(c, "twse", "taiex.close", d, v, 1.0, revision=rev)
    c.execute("INSERT INTO observations(source, series, event_date, value, fetched_at, revision, backfilled, missing_reason) VALUES('twse','taiex.close','2026-10-20',NULL,1,0,0,'blank_in_source')")
    obs(c, "twse", "market.turnover_twd", "2026-10-15", 9e11, 1.0)
    c.commit()
    c.close()
    dates, close = ds.load_taiex(tmp_path / "intel.db")
    assert dates == ["2026-10-15", "2026-10-16", "2026-10-19"] and list(close) == [47000.0, 46100.0, 46822.08]
    before = (tmp_path / "intel.db").read_bytes()
    ds.load_taiex(tmp_path / "intel.db")
    assert (tmp_path / "intel.db").read_bytes() == before


@pytest.mark.skipif(not ds.DEFAULT_INTEL_DB.exists(), reason="lh-intel 不在旁邊")
def test_the_real_series_looks_sane():
    dates, close = ds.load_taiex()
    assert dates[0] == "1999-02-01" and len(dates) > 6800 and dates == sorted(dates) and len(set(dates)) == len(dates) and (close > 0).all()
    assert np.nanmax(np.abs(ds.daily_returns(close))) < 0.12                              # 台股漲跌幅限制 10%（含四捨五入）


def test_study_is_not_used_by_trading_paths():
    for p in list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "range_suggest.py", "stop_suggest.py", "intraday_context.py", "live_state.py", "options_calc.py")] \
            + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_options.py", "routes_market.py")] + [ROOT / "main.py"]:
        assert "dip_study" not in p.read_text(encoding="utf-8"), p


# ── 補強 ─────────────────────────────────────────────────────────

def test_forward_returns_edge_lengths():
    c = np.array([100.0, 110.0, 120.0])
    assert list(ds.forward_returns(c, 2)[:1]) == [pytest.approx(20.0)] and np.isnan(ds.forward_returns(c, 2)[1:]).all()
    assert np.isnan(ds.forward_returns(c, 3)).all() and np.isnan(ds.forward_returns(np.array([100.0]), 1)).all()


def test_one_test_uses_newey_west_with_lag_h():
    dates, close = world(n=6000, seed=51, plant_dip=10.0)
    for h in (60, 120):
        t = ds.one_test(dates, close, "dip", h)
        fwd = ds.forward_returns(close, h)
        ev = ds.event_mask(ds.daily_returns(close), "dip")
        m = ds.period_masks(dates, h, fwd)["explore"]
        X = np.column_stack([np.ones(m.sum()), ev[m].astype(float)])
        b, se = ols_hac(X, fwd[m], h)
        assert t["explore"]["t"] == pytest.approx(b[1] / se[1]) and t["explore"]["excess"] == pytest.approx(b[1])
        b0, se0 = ols_hac(X, fwd[m], 1)
        assert abs(b0[1] / se0[1] - b[1] / se[1]) > 0.05                                                   # 落後期數真的有影響（不是任意值）


def test_by_year_only_lists_years_with_at_least_three_events():
    n = 520
    dates = bdays(n)
    r = np.full(n, 0.001)
    yrs = np.array([int(d[:4]) for d in dates])
    i99, i00 = np.flatnonzero(yrs == 1999), np.flatnonzero(yrs == 2000)
    for k in (10, 20):
        r[i99[k]] = -0.02                                                                                   # 1999 年 2 個跌日
    for k in (10, 20, 30, 40):
        r[i00[k]] = -0.02                                                                                   # 2000 年 4 個
    close = 1000.0 * np.exp(np.cumsum(r))
    d = ds.describe(dates, close, "dip", 60)
    assert set(d["by_year"]) == {2000} and d["by_year"][2000]["n"] == 4 and d["n_events"] == 6
