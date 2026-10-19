"""事件日分析（發現 50）：反應日對應、分母不偷看、分層排列檢定的假陽性率與檢定力、星期混淆、金絲雀、事件載入。
合成資料用隨機漫步，已知的放大倍數植入在『反應日』上，所以能驗證管線真的抓得到、也不會在沒有效果時亂報。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from core import event_day_study as ed
from core.intraday_context import SESSION_MIN, Session
from tests import intel_fixture as fx

TPE, NY = ZoneInfo("Asia/Taipei"), ZoneInfo("America/New_York")


def fast(monkeypatch, b=1500):
    monkeypatch.setattr(ed, "B_PERM", b)
    monkeypatch.setattr(ed, "B_BOOT", 800)


def make_sessions(n_days=1500, start="2020-09-01", seed=0, amp_days=None, gap_days=None, amp=1.0, gap_amp=1.0, weekday_boost=None):
    """隨機漫步日盤。amp_days／gap_days＝日期字串集合（該日盤的波動／跳空放大 amp／gap_amp 倍）；weekday_boost＝{星期: 倍數}。"""
    rng = np.random.default_rng(seed)
    d = date.fromisoformat(start)
    out, prev_close, vol = [], 10000.0, 1.0
    while len(out) < n_days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        iso = d.isoformat()
        vol = float(np.exp(0.9 * np.log(vol) + rng.normal(0, 0.12)))          # 波動有記憶
        scale = vol * (amp if amp_days and iso in amp_days else 1.0) * ((weekday_boost or {}).get(d.weekday(), 1.0))
        g = rng.normal(0, 25) * vol * (gap_amp if gap_days and iso in gap_days else 1.0) * ((weekday_boost or {}).get(d.weekday(), 1.0))
        o = prev_close + (round(g) if abs(g) >= 1 else 1.0)
        steps = rng.normal(0, 2.0 * scale, SESSION_MIN)
        c = o + np.cumsum(steps)
        op = np.concatenate([[o], c[:-1]])
        hi = np.maximum(op, c) + np.abs(rng.normal(0, 0.5 * scale, SESSION_MIN))
        lo = np.minimum(op, c) - np.abs(rng.normal(0, 0.5 * scale, SESSION_MIN))
        out.append(Session(iso, hi, lo, op, c, np.full(SESSION_MIN, 100.0)))
        prev_close = float(c[-1])
    return out


def starts_of(sessions):
    return ed.session_starts([s.date for s in sessions])


def event_time_for_reaction(date_iso, hours_before=12):
    """造一個事件時間，使它的反應日剛好是 date_iso：開盤前 hours_before 小時。"""
    return datetime.fromisoformat(date_iso).replace(hour=8, minute=45, tzinfo=TPE).timestamp() - hours_before * 3600


# ── 時間對應 ─────────────────────────────────────────────────────

def test_release_time_handles_dst():
    assert datetime.fromtimestamp(ed.release_time("2026-07-14"), ZoneInfo("UTC")).strftime("%H:%M") == "12:30"      # EDT
    assert datetime.fromtimestamp(ed.release_time("2026-01-13"), ZoneInfo("UTC")).strftime("%H:%M") == "13:30"      # EST


def test_reaction_day_of_us_events():
    dates = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"]
    st = ed.session_starts(dates)
    # CPI 週二 08:30 ET＝台北週二 20:30 → 週三日盤
    assert dates[ed.reaction_index(ed.release_time("2026-10-13"), st, dates)] == "2026-10-14"
    # 非農 週五 → 週一日盤
    assert dates[ed.reaction_index(ed.release_time("2026-10-16"), st, dates)] == "2026-10-19"
    # FOMC 週三 14:00 ET＝台北週四 02:00 → 週四
    fomc = datetime(2026, 10, 14, 14, 0, tzinfo=NY).timestamp()
    assert dates[ed.reaction_index(fomc, st, dates)] == "2026-10-15"
    # 日盤開盤前一秒／後一秒
    t0 = st[2]
    assert ed.reaction_index(t0 - 1, st, dates) == 2 and ed.reaction_index(t0 + 1, st, dates) == 3
    assert ed.reaction_index(t0, st, dates) == 3            # 剛好等於開盤時間：還沒有被包進這一天（嚴格晚於）


def test_reaction_day_missing_or_far_is_none():
    dates = ["2026-10-12", "2026-10-13"]
    st = ed.session_starts(dates)
    assert ed.reaction_index(ed.release_time("2026-10-20"), st, dates) is None                  # 資料之後
    dates2 = ["2026-10-01", "2026-10-30"]                                                       # 中間有洞：反應日離事件 > 6 天
    assert ed.reaction_index(ed.release_time("2026-10-05"), ed.session_starts(dates2), dates2) is None


# ── 指標 ─────────────────────────────────────────────────────────

def test_log_ratios_basic_and_edges():
    v = np.arange(1.0, 31.0)
    x = ed.log_ratios(v)
    assert np.isnan(x[:20]).all()
    assert x[20] == pytest.approx(np.log(21 / v[:20].mean()))
    z = v.copy()
    z[25] = 0.0
    assert np.isnan(ed.log_ratios(z)[25])                                            # 值為 0（跳空為 0）→ 剔除
    v2 = np.arange(1.0, 61.0)
    w = v2.copy()
    w[22] = np.nan
    xw = ed.log_ratios(w)
    assert np.isnan(xw[22:43]).all() and np.isfinite(xw[43])                         # 分母窗口裡有缺值 → 那 20 天都不算；窗口移開就恢復


def test_log_ratios_never_peeks_forward():
    rng = np.random.default_rng(1)
    v = np.exp(rng.normal(0, 0.3, 200))
    base = ed.log_ratios(v)
    for i in (30, 100, 150):
        w = v.copy()
        w[i + 1:] = 1e9                                                              # 偷看金絲雀：把『之後』全改成怪值
        assert np.array_equal(ed.log_ratios(w)[:i + 1], base[:i + 1], equal_nan=True)
    w = v.copy()
    w[100] *= 5
    changed = np.flatnonzero(~np.isclose(ed.log_ratios(w), base, equal_nan=True))
    assert changed.min() == 100                                                      # 改第 100 天，最早受影響的就是第 100 天


def test_day_table_gap_and_range():
    s = make_sessions(3, seed=3)
    t = ed.day_table(s)
    assert t["range"][1] == pytest.approx(np.nanmax(s[1].hi) - np.nanmin(s[1].lo))
    assert t["gap_abs"][1] == pytest.approx(abs(s[1].op[0] - s[0].cl[-1]))
    assert np.isnan(t["gap_abs"][0])


# ── 檢定 ─────────────────────────────────────────────────────────

def _world(n_pool=1000, seed=0, wd_effect=None):
    rng = np.random.default_rng(seed)
    wd = rng.integers(0, 5, n_pool)
    x = rng.normal(0, 0.35, n_pool) + (np.array([wd_effect.get(w, 0.0) for w in wd]) if wd_effect else 0.0)
    return x, wd


def test_perm_false_positive_rate_near_alpha():
    hits, sims = 0, 150
    for s in range(sims):
        x, wd = _world(seed=s)
        rng = np.random.default_rng(10_000 + s)
        ev = rng.choice(len(x), 30, replace=False)
        pool = np.setdiff1d(np.arange(len(x)), ev)
        hits += ed.strat_perm_test(x[ev], wd[ev], x[pool], wd[pool], b=400, seed=s)["p"] < 0.05
    assert hits / sims <= 0.10                                                       # 名目 5%


def test_perm_detects_planted_effect():
    x, wd = _world(seed=1)
    ev = np.random.default_rng(2).choice(len(x), 40, replace=False)
    x2 = x.copy()
    x2[ev] += np.log(1.4)
    pool = np.setdiff1d(np.arange(len(x)), ev)
    r = ed.strat_perm_test(x2[ev], wd[ev], x2[pool], wd[pool], b=1000)
    assert r["p"] < 0.005 and r["amp"] == pytest.approx(1.4, rel=0.15) and r["n"] == 40


def test_perm_stratification_removes_weekday_confound():
    # 週一天生就大 0.5；事件全落在週一（沒有真效果）。不分層幾乎每次都誤判，分層維持在名目假陽性率
    strat_hits, naive_hits, sims = 0, 0, 40
    for s in range(sims):
        x, wd = _world(seed=300 + s, wd_effect={0: 0.5})
        ev = np.flatnonzero(wd == 0)[:40]
        pool = np.setdiff1d(np.arange(len(x)), ev)
        strat_hits += ed.strat_perm_test(x[ev], wd[ev], x[pool], wd[pool], b=300, seed=s)["p"] < 0.05
        naive_hits += ed.strat_perm_test(x[ev], np.zeros(len(ev), int), x[pool], np.zeros(len(pool), int), b=300, seed=s)["p"] < 0.05
    assert strat_hits / sims <= 0.15
    assert naive_hits / sims >= 0.9                                                  # 確認這個測試真的有鑑別力


def test_perm_one_sided_and_empty_and_short_pool():
    x, wd = _world(seed=4)
    ev = np.random.default_rng(5).choice(len(x), 30, replace=False)
    x2 = x.copy()
    x2[ev] -= 0.5                                                                    # 變小：單尾 p 要很大
    pool = np.setdiff1d(np.arange(len(x)), ev)
    assert ed.strat_perm_test(x2[ev], wd[ev], x2[pool], wd[pool], b=500)["p"] > 0.95
    assert ed.strat_perm_test(np.array([]), np.array([], int), x, wd)["n"] == 0
    with pytest.raises(ValueError):
        ed.strat_perm_test(np.zeros(3), np.zeros(3, int), np.zeros(2), np.zeros(2, int), b=10)


def test_boot_ci_covers_truth_and_mde_scales():
    rng = np.random.default_rng(6)
    x = rng.normal(np.log(1.3), 0.3, 60)
    lo, hi = ed.boot_amp_ci(x, 0.0, b=2000)
    assert lo < 1.3 < hi and lo > 1.1
    assert ed.mde_amp(0.35, 100, 0.0083) < ed.mde_amp(0.35, 25, 0.0083)
    assert ed.mde_amp(0.35, 30, 0.05) < ed.mde_amp(0.35, 30, 0.0083)


# ── 整條管線（合成日盤）─────────────────────────────────────────

def events_for(sessions, picks):
    """picks＝反應日序號清單 → 事件時間（開盤前 12 小時）。"""
    return [event_time_for_reaction(sessions[i].date) for i in picks]


def pick_days(n_sessions, k, seed, lo=60):
    rng = np.random.default_rng(seed)
    return sorted(rng.choice(np.arange(lo, n_sessions - 1), k, replace=False).tolist())


def test_pipeline_finds_planted_and_ignores_unplanted(monkeypatch):
    fast(monkeypatch)
    base = make_sessions(seed=7)
    n = len(base)
    cpi_i, nfp_i, fomc_i = pick_days(n, 70, 1), pick_days(n, 70, 2), pick_days(n, 70, 3)
    amp_days = {base[i].date for i in cpi_i}
    sess = make_sessions(seed=7, amp_days=amp_days, amp=1.6)                          # 同一條隨機路徑，只有 CPI 反應日的波動放大
    ev = {"CPI": events_for(sess, cpi_i), "NFP": events_for(sess, nfp_i), "FOMC": events_for(sess, fomc_i)}
    r = ed.run(sess, ev)
    assert r["tests"]["CPI/range"]["confirmed"] and r["tests"]["CPI/range"]["useful"]
    assert 1.3 < r["tests"]["CPI/range"]["pooled"]["amp"] < 2.0
    for k in ("NFP", "FOMC"):
        assert not r["tests"][f"{k}/range"]["confirmed"], r["tests"][f"{k}/range"]
        assert not r["tests"][f"{k}/gap"]["confirmed"]
    assert not r["canary"]["suspect"]
    assert set(r["tests"]) == {f"{k}/{m}" for k in ed.EVENTS for m in ed.METRICS}


def test_pipeline_finds_planted_gap_only(monkeypatch):
    fast(monkeypatch)
    base = make_sessions(seed=8)
    cpi_i = pick_days(len(base), 80, 4)
    sess = make_sessions(seed=8, gap_days={base[i].date for i in cpi_i}, gap_amp=3.0)
    r = ed.run(sess, {"CPI": events_for(sess, cpi_i), "NFP": [], "FOMC": []})
    assert r["tests"]["CPI/gap"]["confirmed"] and not r["tests"]["CPI/range"]["confirmed"]


def test_pipeline_null_world_has_few_false_confirmations(monkeypatch):
    fast(monkeypatch, 600)
    conf = 0
    for s in range(6):
        sess = make_sessions(n_days=1300, seed=100 + s)
        ev = {k: events_for(sess, pick_days(len(sess), 60, 200 + 3 * s + j)) for j, k in enumerate(ed.EVENTS)}
        r = ed.run(sess, ev)
        conf += sum(t["confirmed"] for t in r["tests"].values())
    assert conf <= 1                                                                 # 6 世界 × 6 檢定 = 36 次，兩關都過的機率極小


def test_pipeline_weekday_confound_not_confirmed(monkeypatch):
    fast(monkeypatch)
    sess = make_sessions(seed=9, weekday_boost={0: 1.5})                              # 週一天生振幅大
    mondays = [i for i, s in enumerate(sess) if date.fromisoformat(s.date).weekday() == 0 and i > 60][:80]
    r = ed.run(sess, {"NFP": events_for(sess, mondays), "CPI": [], "FOMC": []})
    assert not r["tests"]["NFP/range"]["confirmed"] and r["tests"]["NFP/range"]["explore"]["p"] > 0.01


def test_canary_flags_a_method_that_sees_the_day_before(monkeypatch):
    fast(monkeypatch)
    base = make_sessions(seed=11)
    picks = {k: pick_days(len(base), 70, 30 + j) for j, k in enumerate(ed.EVENTS)}
    prev_days = {base[i - 1].date for v in picks.values() for i in v}
    sess = make_sessions(seed=11, amp_days=prev_days, amp=1.7, gap_days=prev_days, gap_amp=3.0)   # 前一天才放大
    r = ed.run(sess, {k: events_for(sess, v) for k, v in picks.items()})
    assert r["canary"]["suspect"] and r["canary"]["n_significant"] >= ed.CANARY_FAIL


def test_exclusions_settlement_and_all_react(monkeypatch):
    fast(monkeypatch)
    sess = make_sessions(n_days=400, seed=12)
    b = ed.build(sess, {k: [] for k in ed.EVENTS})
    assert b["settle"] and all(date.fromisoformat(sess[i].date).weekday() == 2 for i in list(b["settle"])[:5])
    i = sorted(b["settle"])[5]
    days = ed._days(b, {i, i + 1}, ed.START, None, "range")
    assert i not in days                                                              # 結算日不進事件
    # 事件日也是其他事件的反應日時：不在對照裡，但留在事件裡
    j = next(i for i in range(100, 300) if i not in b["settle"])
    ev = {"CPI": [event_time_for_reaction(sess[j].date)], "NFP": [event_time_for_reaction(sess[j].date)], "FOMC": []}
    r = ed.run(sess, ev)
    assert r["tests"]["CPI/range"]["pooled"]["n"] == r["tests"]["NFP/range"]["pooled"]["n"] == 1
    b2 = ed.build(sess, ev)
    pool = ed._days(b2, set(range(len(sess))) - set().union(*b2["react"].values()), ed.START, None, "range")
    assert j not in pool and len(pool) == len(ed._days(b2, set(range(len(sess))), ed.START, None, "range")) - 1


# ── 事件載入 ─────────────────────────────────────────────────────

def test_load_events_rules(tmp_path):
    c = fx.make_db(tmp_path / "intel.db")
    fx.event(c, "fomc-1", "FOMC 利率決策", when=1_700_000_000.0, source="fed", revision=0)
    fx.event(c, "fomc-1", "FOMC 利率決策", when=1_700_003_600.0, source="fed", revision=1)               # 改期：取最新版
    fx.event(c, "fomc-min", "FOMC 會議紀錄", when=1_700_100_000.0, source="fed")                           # 紀錄不算
    fx.event(c, "fomc-x", "FOMC 利率決策", when=1_700_200_000.0, source="fed", status="cancelled")          # 取消不算
    for uid, t, d in (("a", ed.CPI_TITLE, "2024-02-09"), ("b", ed.CPI_TITLE, "2024-02-13"), ("c", ed.NFP_TITLE, "2024-01-05"), ("d", ed.NFP_TITLE, "2024-01-10"),
                      ("e", ed.CPI_TITLE, "2024-03-12")):
        fx.event(c, uid, t, when=None, date=d, source="fred_hist", backfilled=1)
    fx.event(c, "bls-1", ed.CPI_TITLE, when=1_700_300_000.0, date="2024-03-12", source="bls")               # bls 來源不讀（日期與 fred_hist 重複）
    c.close()
    ev = ed.load_events(tmp_path / "intel.db")
    assert ev["FOMC"] == [1_700_003_600.0]
    assert [datetime.fromtimestamp(t, NY).date().isoformat() for t in ev["CPI"]] == ["2024-02-13", "2024-03-12"]     # 02-09 是修訂資料的發布
    assert [datetime.fromtimestamp(t, NY).date().isoformat() for t in ev["NFP"]] == ["2024-01-05"]
    assert all(datetime.fromtimestamp(t, NY).strftime("%H:%M") == "08:30" for t in ev["CPI"] + ev["NFP"])


def test_load_events_is_read_only(tmp_path):
    fx.make_db(tmp_path / "intel.db").close()
    ed.load_events(tmp_path / "intel.db")
    with pytest.raises(sqlite3.OperationalError):
        c = sqlite3.connect(f"file:{tmp_path / 'intel.db'}?mode=ro", uri=True)
        c.execute("INSERT INTO schema_meta VALUES('x','y')")


def test_study_not_imported_by_trading_modules():
    import pathlib
    root = pathlib.Path(ed.__file__).resolve().parent.parent
    for p in list((root / "strategies").glob("*.py")) + list((root / "core").glob("*.py")) + list((root / "api").glob("*.py")) + [root / "main.py"]:
        if p.name == "event_day_study.py":
            continue
        assert "event_day_study" not in p.read_text(encoding="utf-8"), p


# ── 描述與輸出 ───────────────────────────────────────────────────

def test_descriptive_and_report_on_planted_world(monkeypatch):
    import json
    fast(monkeypatch)
    base = make_sessions(seed=21)
    cpi_i = pick_days(len(base), 70, 5)
    sess = make_sessions(seed=21, amp_days={base[i].date for i in cpi_i}, amp=1.8)
    r = ed.run(sess, {"CPI": events_for(sess, cpi_i), "NFP": [], "FOMC": []})
    d = r["descriptive"]["CPI"]["range"]
    assert d["n"] >= 55 and "NFP" in r["descriptive"] and r["descriptive"]["NFP"] == {}
    assert d["ratio_event"]["P50"] > 1.3 * d["ratio_ctrl"]["P50"]                       # 事件日的中位比值明顯較大
    assert d["ratio_event"]["P50"] < d["ratio_event"]["P80"] < d["ratio_event"]["P95"]
    assert d["points_median_event"] > 1.3 * d["points_median_ctrl"]
    assert set(d["yearly"]) >= {"2021", "2022", "2023"} and sum(q["n"] for q in d["yearly"].values()) == d["n"]
    assert all(1.2 < q["amp"] < 2.6 for q in d["yearly"].values() if q["n"] >= 8)
    assert d["mde_explore"] > 1.0 and d["sd_log"] > 0
    json.dumps(r, default=float)                                                         # 可存檔
    text = ed.format_report(r)
    assert "CPI/range" in text and "金絲雀" in text and "有用" in text and "2021:" in text


def test_pipeline_ranges_and_gaps_use_their_own_columns():
    s = make_sessions(60, seed=2)
    b = ed.build(s, {k: [] for k in ed.EVENTS})
    assert np.allclose(b["x"]["range"], ed.log_ratios(ed.day_table(s)["range"]), equal_nan=True)
    assert np.allclose(b["x"]["gap"], ed.log_ratios(ed.day_table(s)["gap_abs"]), equal_nan=True)
    assert not np.allclose(b["x"]["range"], b["x"]["gap"], equal_nan=True)


# ── 登記的常數與判定規則（用假的檢定結果單獨驗證）──────────────────

def test_registered_constants_are_locked():
    assert (ed.START, ed.SPLIT, ed.N_BASE, ed.N_TESTS, ed.B_PERM, ed.B_BOOT, ed.SEED) == ("2021-01-01", "2025-01-01", 20, 6, 20000, 5000, 20261019)
    assert ed.ALPHA_EXPLORE == pytest.approx(0.05 / 6) and ed.ALPHA_HOLDOUT == 0.05
    assert ed.MIN_AMPLIFY == 1.10 and ed.CANARY_FAIL == 4 and ed.MAX_REACTION_DAYS == 6
    assert ed.EVENTS == ("FOMC", "CPI", "NFP") and ed.METRICS == ("range", "gap")


def test_start_time_and_reaction_gap_boundaries():
    assert ed.session_starts(["2026-10-14"]) == [datetime(2026, 10, 14, 0, 45, tzinfo=ZoneInfo("UTC")).timestamp()]       # 台北 08:45
    st = ed.session_starts(["2026-10-01", "2026-10-08"])
    ev = datetime(2026, 10, 2, 12, 0, tzinfo=TPE).timestamp()                                                              # 10/2 → 反應日 10/8＝6 天：可以
    assert ed.reaction_index(ev, st, ["2026-10-01", "2026-10-08"]) == 1
    ev2 = datetime(2026, 10, 1, 12, 0, tzinfo=TPE).timestamp()                                                             # 10/1 → 10/8＝7 天：資料有洞
    assert ed.reaction_index(ev2, st, ["2026-10-01", "2026-10-08"]) is None


def test_perm_exact_values():
    wd = np.array([0, 0, 1])
    # 對照全是 0、事件平均 > 0 → 對照平均永遠 < 事件平均：p＝1/(b+1)
    r = ed.strat_perm_test(np.array([1.0, 1.0]), np.array([0, 0]), np.zeros(10), np.zeros(10, int), b=99)
    assert r["p"] == pytest.approx(1 / 100) and r["mean_ctrl"] == 0.0 and r["amp"] == pytest.approx(np.exp(1.0))
    # 全部打平（事件平均＝對照平均）→ 每次都算『不小於』：p＝1
    assert ed.strat_perm_test(np.zeros(2), np.zeros(2, int), np.zeros(10), np.zeros(10, int), b=99)["p"] == 1.0
    # 對照的期望值依事件的星期加權：週一 2 天（對照週一平均 1）、週二 1 天（對照週二平均 4）→ (2·1 + 1·4)/3
    pool_x = np.array([1.0, 1.0, 1.0, 4.0, 4.0, 4.0])
    pool_wd = np.array([0, 0, 0, 1, 1, 1])
    r = ed.strat_perm_test(np.array([2.0, 2.0, 5.0]), wd, pool_x, pool_wd, b=50)
    assert r["mean_ctrl"] == pytest.approx(2.0) and r["mean_event"] == pytest.approx(3.0) and r["amp"] == pytest.approx(np.exp(1.0)) and r["n"] == 3
    # 抽樣的對照平均是整數倍 (2·1+4)/3，所以事件平均 3.0 > 2.0 永遠大於對照：p＝1/(b+1)
    assert r["p"] == pytest.approx(1 / 51)


def test_bootstrap_interval_is_95_percent_and_mde_formula():
    x = np.random.default_rng(0).normal(0, 1, 400)
    lo, hi = ed.boot_amp_ci(x, 0.0, b=4000)
    m, se = x.mean(), x.std(ddof=1) / 20
    assert lo == pytest.approx(np.exp(m - 1.96 * se), abs=0.012) and hi == pytest.approx(np.exp(m + 1.96 * se), abs=0.012)
    assert ed.mde_amp(0.35, 30, 0.05) == pytest.approx(np.exp((1.6449 + 0.8416) * 0.35 / np.sqrt(30)), rel=1e-3)


def hand_world(n=300, seed=0):
    rng = np.random.default_rng(seed)
    dates, d = [], date(2021, 1, 4)
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return {"dates": dates, "wd": np.array([date.fromisoformat(x).weekday() for x in dates]), "x": {"range": rng.normal(0, 0.3, n), "gap": rng.normal(0, 0.3, n)},
            "react": {k: set() for k in ed.EVENTS}, "settle": set(), "table": {"range": np.full(n, 100.0), "gap_abs": np.full(n, 10.0)}}


def test_one_test_pool_excludes_given_days():
    b = hand_world(seed=1)
    ev = {40, 80, 120, 160, 200}
    bad = set(range(0, 300, 7)) - ev
    for i in bad:
        b["x"]["range"][i] = 50.0                                                     # 被排除的日子是極端值：沒排除，對照平均會被拉高
    r = ed.one_test(b, ev, "range", ed.START, None, exclude=bad | ev)
    assert abs(r["mean_ctrl"]) < 0.2 and r["n"] == 5
    assert ed.one_test(b, ev, "range", ed.START, None, exclude=ev)["mean_ctrl"] > 1.0


def test_decision_rules_with_scripted_results(monkeypatch):
    calls: list[tuple] = []
    script = {"explore_p": 0.002, "hold_p": 0.03, "lo": 1.12}

    def fake_test(b, ev_idx, metric, lo, hi, exclude):
        calls.append((lo, hi, frozenset(ev_idx), frozenset(exclude)))
        p = script["explore_p"] if (lo, hi) == (ed.START, ed.SPLIT) else script["hold_p"] if (lo, hi) == (ed.SPLIT, None) else 0.3
        return {"n": 10, "mean_event": 0.2, "mean_ctrl": 0.0, "amp": 1.22, "p": p}

    b = hand_world()
    b["react"]["CPI"] = {50, 60}
    b["react"]["NFP"] = {70}
    monkeypatch.setattr(ed, "build", lambda s, e: b)
    monkeypatch.setattr(ed, "one_test", fake_test)
    monkeypatch.setattr(ed, "boot_amp_ci", lambda x, c, **k: (script["lo"], 1.5))

    def verdict():
        calls.clear()
        return ed.run([], {})["verdict"]["CPI/range"]

    assert verdict() == "有用（確認且下限≥1.10）"
    # 事件日用的是各自的反應日；探索期與保留期用的是登記的窗口
    assert (ed.START, ed.SPLIT) in {(c[0], c[1]) for c in calls} and (ed.SPLIT, None) in {(c[0], c[1]) for c in calls}
    canary_calls = [c for c in calls[-6:]]
    assert any(c[2] == frozenset({49, 59}) and {49, 59, 50, 60, 70} <= c[3] for c in canary_calls)                 # 金絲雀＝前一個日盤，對照排除反應日與金絲雀日
    script["lo"] = 1.05
    assert verdict() == "確認但幅度小"
    script["lo"] = 1.12
    script["hold_p"] = 0.06
    assert verdict() == "探索過、保留期沒確認"
    script["hold_p"], script["explore_p"] = 0.03, 0.02
    assert verdict() == "沒過"                                                        # 0.02 沒到 Bonferroni 的 0.0083；保留期再好也不算
    script["explore_p"] = ed.ALPHA_EXPLORE
    assert verdict() == "沒過"                                                        # 剛好等於門檻不算（要小於）


def test_describe_control_excludes_every_event_type_reaction_day():
    b = hand_world(n=400, seed=3)
    b["react"]["CPI"] = set(range(60, 120, 5))
    b["react"]["NFP"] = set(range(61, 400, 6))
    for i in b["react"]["NFP"]:
        b["x"]["range"][i] = 5.0                                                      # 其他事件的反應日是極端值：對照混進去 P95 會爆
    d = ed.describe(b, set().union(*b["react"].values()))["CPI"]["range"]
    assert d["ratio_ctrl"]["P95"] < 3 and d["n"] == len(ed._days(b, b["react"]["CPI"], ed.START, None, "range"))
