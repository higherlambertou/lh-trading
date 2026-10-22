"""開盤前預期日盤振幅（發現 55）：x_iv 的定義、擬合、區間、即時指數的讀取規則（只用前一個交易日、太舊／讀不到就不顯示）、與發現 54 研究的因子一致、描述性檢查、整合進〈時段脈絡〉。"""
from __future__ import annotations

import csv
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import core.intel_reader as ir
from core import intraday_context as ic
from core import iv_day_range as dr
from core import iv_range_study as study
from core.intraday_context import SESSION_MIN, Session
from intel_fixture import make_db, obs
from test_iv_range_study import world

ROOT = Path(dr.__file__).resolve().parent.parent
TPE = ir.TAIPEI


# ── 因子 ─────────────────────────────────────────────────────────

def test_x_of_definition_and_bad_inputs():
    assert dr.x_of(25.0, 40000.0) == pytest.approx(math.log(0.25 * 40000)) and dr.x_of("25", "40000") == pytest.approx(math.log(10000))
    for bad in ((0, 40000), (-1, 40000), (25, 0), (25, -5), (float("nan"), 40000), (25, float("inf")), (None, 1), ("x", 1)):
        assert dr.x_of(*bad) is None


def test_load_iv_history_reads_the_vrp_csv_files(tmp_path):
    for name, rows in (("iv_2026_09.csv", [("2026-09-30", 20.5, 40000.0)]), ("iv_2026_08.csv", [("2026-08-31", 21.0, 39000.0)])):
        with (tmp_path / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "expiry", "expiry_date", "T_days", "F", "K", "iv"])
            for d, v, f in rows:
                w.writerow([d, "202610", "2026-10-21", 15.0, f, f, v])
    h = dr.load_iv_history(tmp_path)
    assert list(h) == ["2026-08-31", "2026-09-30"] and h["2026-09-30"] == {"iv": 20.5, "F": 40000.0} and dr.load_iv_history(tmp_path / "none") == {}


@pytest.mark.skipif(not (dr.IV_DIR.exists() and any(dr.IV_DIR.glob("iv_*.csv"))), reason="本機沒有 data/taifex/vrp")
def test_the_history_loader_matches_the_research_loader_on_real_data():
    from core.vrp_study import load_iv
    a, b = dr.load_iv_history(), load_iv(dr.IV_DIR)
    assert list(a) == list(b) and all(a[d]["iv"] == b[d]["iv"] and a[d]["F"] == b[d]["F"] for d in a)


def test_prev_x_matches_the_study_factor_exactly():
    sessions, iv = world(n=400, seed=5, kind="leading")
    iv = dict(iv)
    for d in list(iv)[100:120]:
        del iv[d]                                                                         # 中間缺一段：過期與缺值的行為也要一致
    dates = [s.date for s in sessions]
    assert np.array_equal(dr.prev_x(dates, iv), study.build(sessions, iv)["x_prev"], equal_nan=True)


# ── 擬合與預測 ───────────────────────────────────────────────────

def test_fit_recovers_a_planted_iv_dependence_and_quantiles_are_ordered():
    sessions, iv = world(n=1500, seed=11, kind="leading")
    m = dr.fit(sessions, iv)
    assert m.n > 1400 and m.b_iv > 0.3 and m.q16 < m.q50 < m.q84
    d = dr.design(sessions, iv)
    X = np.column_stack([np.ones(len(d["y"])), d["lnR"], d["x"]])
    beta = np.linalg.lstsq(X, d["y"], rcond=None)[0]
    assert (m.a, m.b_ref, m.b_iv) == pytest.approx(tuple(beta))
    q = np.quantile(d["y"] - X @ beta, [0.16, 0.5, 0.84])
    assert (m.q16, m.q50, m.q84) == pytest.approx(tuple(q)) and m.n == len(d["y"])
    s2, i2 = world(n=1500, seed=12, kind="null")
    assert abs(dr.fit(s2, i2).b_iv) < 0.15                                                 # 無關的 IV：係數接近 0


def test_fit_needs_enough_history():
    sessions, iv = world(n=200, seed=3)
    assert dr.fit(sessions, iv) is None and dr.fit(sessions, {}) is None


STUDY_IV_END = "2026-10-12"            # 發現 54 跑的時候 data/taifex/vrp 的最後一天


@pytest.mark.skipif(not (dr.IV_DIR.exists() and any(dr.IV_DIR.glob("iv_*.csv"))), reason="本機沒有 data/taifex/vrp")
def test_the_full_sample_fit_reproduces_the_registered_study_coefficients_on_real_data():
    import json
    res = ROOT / "data" / "iv_range_result.json"
    if not res.exists():
        pytest.skip("沒有 data/iv_range_result.json")
    from core.intraday_context import load_sessions
    from core.market_store import MarketStore
    old = json.loads(res.read_text(encoding="utf-8"))["tests"]["range"]["coef"]
    iv = {d: v for d, v in dr.load_iv_history().items() if d <= STUDY_IV_END}                  # 期交所資料現在每天自動更新（發現 65）：只用登記研究當時的資料
    m = dr.fit(load_sessions(MarketStore(), "TXF"), iv)
    assert m.b_iv == pytest.approx(old["x_iv"], abs=1e-6) and m.b_ref == pytest.approx(old["lnR"], abs=1e-6)                # 與已登記的研究同一個模型


def test_project_formula_and_monotonicity():
    m = dr.DayModel(a=-2.0, b_ref=0.2, b_iv=0.8, q16=-0.3, q50=-0.02, q84=0.35, n=100)
    mu = -2.0 + 0.2 * math.log(500) + 0.8 * 9.0
    p = dr.project(m, 500.0, 9.0)
    assert p == {"lo": round(math.exp(mu - 0.3), 1), "mid": round(math.exp(mu - 0.02), 1), "hi": round(math.exp(mu + 0.35), 1)} and p["lo"] < p["mid"] < p["hi"]
    assert dr.project(m, 600.0, 9.0)["mid"] > p["mid"] and dr.project(m, 500.0, 9.5)["mid"] > p["mid"]


# ── 即時的指數 ───────────────────────────────────────────────────

NOW = datetime(2026, 10, 20, 8, 40, tzinfo=TPE).timestamp()
H = 3600.0


def vix_db(tmp_path, rows):
    c = make_db(tmp_path / "intel.db")
    for d, v, fetched in rows:
        obs(c, "taifex", "vixtw.close", d, v, fetched)
    c.close()
    return tmp_path / "intel.db"


def test_live_vix_uses_only_the_latest_day_strictly_before_today(tmp_path):
    p = vix_db(tmp_path, [("2026-10-16", 26.37, NOW - 60 * H), ("2026-10-19", 29.69, NOW - 15 * H), ("2026-10-20", 31.0, NOW - 100)])
    assert dr.live_vix("2026-10-20", NOW, p) == (("2026-10-19", 29.69), None)                    # 今天的（就算已經在庫裡）不用
    assert dr.live_vix("2026-10-19", NOW, p) == (("2026-10-16", 26.37), None)
    got, why = dr.live_vix("2026-10-16", NOW, p)
    assert got is None and "還沒有" in why                                                         # 沒有更早的


def test_live_vix_staleness_missing_and_unreadable_never_raise(tmp_path):
    p = vix_db(tmp_path, [("2026-10-19", 29.69, NOW - 15 * H)])
    assert dr.live_vix("2026-10-23", NOW, p)[0] == ("2026-10-19", 29.69)                          # 4 天：還可以
    got, why = dr.live_vix("2026-10-24", NOW, p)
    assert got is None and "太舊" in why and "10-19" in why                                        # 5 天：不用
    got, why = dr.live_vix("2026-10-20", NOW, tmp_path / "nope.db")
    assert got is None and "找不到" in why
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 50)
    got, why = dr.live_vix("2026-10-20", NOW, bad)
    assert got is None and "讀不到" in why
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert dr.live_vix("2026-10-20", NOW, empty)[0] is None


def test_live_vix_respects_what_was_known_at_that_time_and_skips_blank_values(tmp_path):
    p = vix_db(tmp_path, [("2026-10-19", 29.69, NOW + 1000)])                                       # 還沒抓到（fetched_at 在未來）
    assert dr.live_vix("2026-10-20", NOW, p)[0] is None
    c = sqlite3.connect(p)
    c.execute("UPDATE observations SET fetched_at=?", (NOW - 1000,))                                # 現在抓到了
    c.commit()
    assert dr.live_vix("2026-10-20", NOW, p)[0] == ("2026-10-19", 29.69)
    c.execute("INSERT INTO observations(source, series, event_date, value, fetched_at, revision, backfilled, missing_reason) VALUES('taifex','vixtw.close','2026-10-19',NULL,?,1,0,'blank_in_source')", (NOW - 100,))
    c.commit()
    c.close()
    got, why = dr.live_vix("2026-10-20", NOW, p)
    assert got is None and why                                                                      # 最新一版是空值：不當 0、也不退回舊值


# ── 組起來 ───────────────────────────────────────────────────────

def test_for_today_applies_the_ratio_and_the_previous_close():
    sessions, iv = world(n=1500, seed=11, kind="leading")
    today = (date.fromisoformat(sessions[-1].date) + timedelta(days=1)).isoformat()
    out = dr.for_today(sessions, today, iv=iv, vix=("2026-10-19", 30.0))
    m = dr.fit(sessions, iv)
    rng = [ic.window_range(s, 0, SESSION_MIN) for s in sessions]
    r20 = float(np.mean(rng[-20:]))
    last = float(sessions[-1].cl[-1])
    assert out["expect"] == dr.project(m, r20, dr.x_of(30.0 * dr.RATIO, last)) and out["r20"] == round(r20, 1) and out["vix"] == 30.0 and out["vix_date"] == "2026-10-19"
    assert out["atm_equiv"] == round(30.0 * 0.93, 2) and out["n"] == m.n and dr.RATIO == 0.93
    assert dr.for_today(sessions, today, iv=iv, vix=("2026-10-19", 40.0))["expect"]["mid"] > out["expect"]["mid"]            # 隱含波動越高，預期越大


def test_for_today_says_why_instead_of_guessing():
    sessions, iv = world(n=1500, seed=11)
    today = "2026-11-02"
    assert dr.for_today([], today, iv=iv, vix=("2026-10-30", 30.0)) == {"note": "沒有歷史日盤"}
    assert "歷史不夠" in dr.for_today(sessions[:100], today, iv=iv, vix=("2026-10-30", 30.0))["note"]
    assert dr.for_today(sessions, today, iv=iv, reason="還沒有前一天的臺指選擇權波動率指數") == {"note": "還沒有前一天的臺指選擇權波動率指數"}
    assert dr.for_today(sessions, today, iv=iv, reason="", vix=None) == {"note": "沒有前一天的臺指選擇權波動率指數"}                    # 沒有原因時的預設說法
    broken = list(sessions)
    broken[-3] = Session(broken[-3].date, np.full(SESSION_MIN, np.nan), np.full(SESSION_MIN, np.nan), broken[-3].op, broken[-3].cl, broken[-3].vol)
    assert "不完整" in dr.for_today(broken, today, iv=iv, vix=("2026-10-30", 30.0))["note"]


def test_for_today_reads_the_real_sources_when_not_injected(tmp_path, monkeypatch):
    sessions, iv = world(n=1500, seed=11, kind="leading")
    today = (date.fromisoformat(sessions[-1].date) + timedelta(days=1)).isoformat()
    p = vix_db(tmp_path, [((date.fromisoformat(today) - timedelta(days=1)).isoformat(), 28.0, NOW - 10 * H)])
    monkeypatch.setenv("INTEL_DB", str(p))
    monkeypatch.setattr(dr, "load_iv_history", lambda *a, **k: iv)
    got = dr.for_today(sessions, today)
    assert "expect" in got and got["vix"] == 28.0
    monkeypatch.setenv("INTEL_DB", str(tmp_path / "nope.db"))
    assert "找不到" in dr.for_today(sessions, today)["note"]


# ── 描述性檢查 ───────────────────────────────────────────────────

def test_coverage_check_on_a_planted_world():
    sessions, iv = world(n=1500, seed=51, kind="leading")
    c = dr.coverage(sessions, iv)
    assert c["n_oos"] == c["n"] - dr.WARM and 0.58 < c["iv"]["coverage"] < 0.78 and 0.58 < c["r20_only"]["coverage"] < 0.78
    assert c["iv"]["mae_log"] < c["r20_only"]["mae_log"] and c["iv"]["width_log"] < c["r20_only"]["width_log"]               # 植入的世界裡 IV 確實更準、區間更窄
    assert c["iv"]["coverage_holdout"] is not None and dr.coverage(sessions[:200], iv) is None
    s_small, i_small = world(n=dr.WARM + 21 + 30, seed=52, kind="leading")                      # 可用樣本約 WARM＋30：不到 WARM＋60，不做
    assert len(dr.design(s_small, i_small)["y"]) < dr.WARM + 60 and dr.coverage(s_small, i_small) is None


def test_substitute_gap_is_zero_when_the_substitute_is_exact_and_small_otherwise():
    sessions, iv = world(n=1500, seed=61, kind="leading")
    m = dr.fit(sessions, iv)
    dates = [s.date for s in sessions]
    vix = {d: iv[d]["iv"] / dr.RATIO for d in dates[-60:-2]}
    g = dr.substitute_gap(m, sessions, iv, vix)
    assert g["n"] > 50 and g["max_abs_pct"] < 0.5                                              # 完全一致的替身（F 也一樣）：差距只剩四捨五入
    noisy = {d: v * 1.05 for d, v in vix.items()}
    g2 = dr.substitute_gap(m, sessions, iv, noisy)
    assert 2 < g2["median_abs_pct"] < 6 and g2["mean_pct"] > 0                                  # 指數高估 5% → 預期振幅約高估 4%
    assert dr.substitute_gap(m, sessions, iv, {}) is None
    # 逐筆對照獨立算的：差距％＝(替身預測 − 真實預測) ÷ 真實預測
    R = ic.ref_ranges(sessions)
    pos = {d: i for i, d in enumerate(dates)}
    expect = []
    for d, v in noisy.items():
        i = pos[d]
        f = float(sessions[i].cl[-1])
        a = dr.project(m, float(R[i + 1]), dr.x_of(iv[d]["iv"], iv[d]["F"]))["mid"]
        b = dr.project(m, float(R[i + 1]), dr.x_of(v * dr.RATIO, f))["mid"]
        expect.append((b - a) / a * 100)
    assert g2["mean_pct"] == pytest.approx(np.mean(expect)) and g2["n"] == len(expect) and g2["max_abs_pct"] == pytest.approx(np.max(np.abs(expect)))
    edge = dict(noisy)
    edge[dates[-1]] = 30.0                                                                  # 最後一天（沒有隔天）
    edge["2020-01-04"] = 30.0                                                               # 不是日盤日
    del_iv = dict(iv)
    gone = dates[-30]
    del del_iv[gone]                                                                        # 有指數但沒有真的 ATM IV
    g3 = dr.substitute_gap(m, sessions, del_iv, edge)
    assert g3["n"] == g2["n"] - 1                                                           # 三種不能比的日子都被略過


# ── 整合進〈時段脈絡〉────────────────────────────────────────────

def ctx_data(day_iv):
    return ic.ContextData("2026-10-20", 525.1, {}, [1.0] * 20, 1600, "2026-10-19", day_iv)


def test_describe_passes_day_iv_during_the_day_session_only():
    now = datetime(2026, 10, 20, 8, 40, tzinfo=TPE)
    d = {"expect": {"lo": 300.0, "mid": 450.0, "hi": 650.0}, "r20": 525.1, "vix": 29.69, "vix_date": "2026-10-19", "atm_equiv": 27.61, "n": 1574}
    assert ic.describe(ctx_data(d), now, None, "day")["day_iv"] == d
    assert ic.describe(ctx_data({"note": "x"}), now, None, "day")["day_iv"] == {"note": "x"}
    assert "day_iv" not in ic.describe(ctx_data(None), now, None, "day")
    assert "day_iv" not in ic.describe(ctx_data(d), now, None, "night") and "day_iv" not in ic.describe(None, now, None, "day")
    assert ic.ContextData("x", None, {}, [], 0, None).day_iv is None                           # 舊的建構方式（沒給）仍然可以


def test_compute_survives_a_failing_day_iv(monkeypatch):
    sessions, _ = world(n=300, seed=2)

    class Store:
        def bars_1m(self, code, since=None):
            out = []
            for s in sessions:
                for i in range(SESSION_MIN):
                    hhmm = (8 * 60 + 45 + i + 1) // 60 * 100 + (8 * 60 + 45 + i + 1) % 60
                    out.append({"date": s.date, "hhmm": hhmm, "open": s.op[i], "high": s.hi[i], "low": s.lo[i], "close": s.cl[i], "volume": 10})
            return out
    monkeypatch.setattr(dr, "for_today", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("壞了")))
    data = ic.compute(Store(), "TXF", "2027-01-01")
    assert data.day_iv == {"note": "計算失敗（細節在後端 log）"} and data.n_days > 250                  # 失敗不影響其他脈絡
    seen = []
    monkeypatch.setattr(dr, "for_today", lambda s, today: seen.append((len(s), today)) or {"note": "ok"})
    assert ic.compute(Store(), "TXF", "2027-01-01").day_iv == {"note": "ok"}
    assert seen == [(data.n_days, "2027-01-01")]                                            # 用的是「今天以前」的日盤、今天的日期


# ── 只顯示：誰可以引用它 ─────────────────────────────────────────

def test_only_the_context_module_uses_it_and_no_trading_path_does():
    allowed = {"intraday_context.py", "iv_day_range.py", "iv_monitor.py"}            # iv_monitor 只借用歷史 IV 檔的讀取函式（回填 iv_history，發現 60）
    for p in list((ROOT / "core").glob("*.py")) + list((ROOT / "api").glob("*.py")) + list((ROOT / "strategies").glob("*.py")) + [ROOT / "main.py"]:
        if p.name in allowed:
            continue
        assert "iv_day_range" not in p.read_text(encoding="utf-8"), p
