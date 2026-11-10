"""日資料小檢定（core/daily_indicator_study.py，update.md 發現 82）：z 分數只用今天以前的資料、目標對齊（T 的資訊 → T 之後的時段）、結算日的找法、
三個過關條件、植入已知現象找得到、隨機漫步上的假陽性率、「PCR 只是對當天走勢的反應」的金絲雀、只跑一次、不被交易路徑引用也不用 intel_reader。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

import core.daily_indicator_study as di
from core.drift_timing_study import Day

ROOT = Path(di.__file__).resolve().parent.parent


def day(o, c, front="202601", extra=None):
    opens, closes = {front: o}, {front: c}
    for k, (eo, ec) in (extra or {}).items():
        opens[k], closes[k] = eo, ec
    return Day(front, opens, closes, {}, {})


def bdays(n, start="2005-01-03"):
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ── z 分數 ──────────────────────────────────────────────────────

def test_zscore_uses_only_the_previous_window_and_excludes_today():
    ds = [f"d{i}" for i in range(8)]
    s = dict(zip(ds, [1.0, 2.0, 3.0, 4.0, 10.0, 6.0, 7.0, 8.0]))
    z = di.zscores(s, ds, warm=3)
    assert list(z) == ["d3", "d4", "d5", "d6", "d7"]                                  # 前 3 天不夠暖機
    assert z["d3"] == pytest.approx((4.0 - 2.0) / 1.0)                                  # 窗口 [1,2,3]：平均 2、標準差 1（n−1）
    assert z["d5"] == pytest.approx((6.0 - np.mean([3.0, 4.0, 10.0])) / np.std([3.0, 4.0, 10.0], ddof=1))
    s2 = dict(s); s2["d7"] = 1e9                                                        # 改掉「今天」的值，今天以前的 z 完全不變
    z2 = di.zscores(s2, ds, warm=3)
    assert all(z2[d] == z[d] for d in ["d3", "d4", "d5", "d6"]) and z2["d7"] > 1e6


def test_default_window_is_250_trading_days():
    ds = bdays(300)
    rng = np.random.default_rng(0)
    z = di.zscores({d: float(v) for d, v in zip(ds, rng.normal(size=300))}, ds)
    assert di.WARM == 250 and len(z) == 50 and min(z) == ds[250]                         # 前 250 天暖機：第 251 天才有


def test_zscore_skips_missing_and_constant_windows():
    ds = [f"d{i}" for i in range(8)]
    s = {d: v for d, v in zip(ds, [1.0, float("nan"), 2.0, 3.0, None, 4.0, 5.0, 6.0]) if v is not None}
    z = di.zscores(s, ds, warm=3)
    assert "d1" not in z and "d4" not in z and z["d5"] == pytest.approx((4.0 - 2.0) / 1.0)     # 窗口＝前 3 個「有值」的日子 [1,2,3]
    flat = di.zscores({d: 5.0 for d in ds}, ds, warm=3)
    assert flat == {}                                                                      # 沒有變異：不給 z


# ── 日報酬、結算日 ────────────────────────────────────────────────

def test_day_returns_use_the_front_contract_and_skip_bad_prices():
    days = {"2025-01-02": day(100.0, 101.0), "2025-01-03": day(0.0, 5.0), "2025-01-06": day(100.0, 99.0, extra={"202602": (50.0, 80.0)}),
            "2025-01-07": day(-1.0, 5.0), "2025-01-08": day(100.0, -2.0)}
    r = di.day_returns(days)
    assert r["2025-01-02"] == pytest.approx(0.01 * 47000) and r["2025-01-06"] == pytest.approx(-0.01 * 47000)
    assert set(r) == {"2025-01-02", "2025-01-06"}                                         # 價格是 0 或負的整天不算


def test_settlement_is_the_last_day_a_monthly_contract_trades_and_the_current_one_is_excluded():
    days = {"2026-01-14": day(1, 1, extra={"202601": (1, 1), "202602": (1, 1)}), "2026-01-21": day(1, 1, extra={"202601": (1, 1), "202602": (1, 1)}),
            "2026-01-22": day(1, 1, front="202602", extra={"202603": (1, 1)}), "2026-02-18": day(1, 1, front="202602", extra={"202603": (1, 1)}),
            "2026-02-19": day(1, 1, front="202603", extra={"202604": (1, 1)}), "2026-02-20": day(1, 1, front="202603", extra={"202604": (1, 1)})}
    days["2026-01-14"] = Day("202601", {"202601": 1.0, "202602": 1.0}, {"202601": 1.0, "202602": 1.0}, {}, {})
    s = di.settlement_dates(days)
    assert s == ["2026-01-21", "2026-02-18"]                                            # 202603 在最後一天還在交易：還沒到期、不算
    assert di.settlement_dates({}) == []


# ── 對齊 ────────────────────────────────────────────────────────

def world(rng, n=1400, beta_next=0.0, beta_night=0.0, basis_beta=0.0, sett_pts=0.0, contemporaneous=0.0, start="2005-01-03"):
    """合成的世界。日報酬 D、PCR（成交量比、未平倉比）、加權指數、夜盤報酬 N；可以植入各種關係。"""
    dates = bdays(n, start)
    sd_ret = 100.0
    D = {}
    pcr_v, pcr_o, taiex, n17 = {}, {}, {}, {}
    base = {"v": rng.normal(0, 1, n), "o": rng.normal(0, 1, n), "b": rng.normal(0, 1, n)}
    noise = rng.normal(0, sd_ret, n)
    nnoise = rng.normal(0, sd_ret, n)
    # 先決定 PCR（z 的來源）：用獨立的雜訊；z 本身依 250 天窗口算，這裡用 ±1 SD 的雜訊近似，效果大小以 z 為單位足夠
    settle = {dates[i] for i in range(20, n, 21)}
    out = {}
    for i, d in enumerate(dates):
        pcr_v[d] = 100.0 + 10.0 * base["v"][i]
        pcr_o[d] = 100.0 + 10.0 * base["o"][i]
    zv = di.zscores(pcr_v, dates)
    zo = di.zscores(pcr_o, dates)
    prev_settle = {dates[i - 1] for i in range(1, n) if dates[i] in settle}
    for i, d in enumerate(dates):
        r = noise[i]
        if i > 0 and dates[i - 1] in zv:
            r += beta_next * zv[dates[i - 1]]                                             # 昨天的 PCR z → 今天日盤
        if d in settle:
            r += sett_pts
        D[d] = r
        n17[d] = nnoise[i] + (beta_night * zo.get(d, 0.0))                                   # 當天的 PCR 未平倉 z → 當晚（17:01 起）
    for i, d in enumerate(dates):
        if contemporaneous and i >= 0:
            pcr_v[d] += -contemporaneous * D[d] / sd_ret * 10.0                              # PCR 只是對「當天」走勢的反應
    code, k = {}, 0
    for d in dates:
        code[d] = f"{k:06d}"                                                               # 每個合約到結算日為止；結算日的下一天換下一個合約
        if d in settle:
            k += 1

    def mk():
        return {d: day(47000.0, 47000.0 * (1 + D[d] / 47000.0), front=code[d]) for d in dates}
    days = mk()
    # 基差：從指數與期貨收盤算；植入的關係放在 D 上（下一天）：basis_z → 下一天的報酬
    if basis_beta:
        b = rng.normal(0, 1, n)
        for i, d in enumerate(dates):
            if i + 1 < n:
                D[dates[i + 1]] += basis_beta * b[i]
        days = mk()
        for i, d in enumerate(dates):
            taiex[d] = 47000.0 * (1 - 0.01 * b[i])                                         # 基差 ∝ b（期貨收盤固定為 47000 × (1 + D/47000)）
    else:
        for i, d in enumerate(dates):
            taiex[d] = 47000.0 * (1 + 0.001 * rng.normal())
    return days, pcr_v, pcr_o, taiex, n17


def test_planted_pcr_to_next_day_is_found_and_only_t2():
    days, v, o, t, n = world(np.random.default_rng(1), beta_next=40.0)
    r = di.run(days, v, o, t, n)
    assert r["tests"]["T2"]["pass"] and r["tests"]["T2"]["b"] == pytest.approx(40.0, abs=12) and "T2" in r["passed"]
    assert r["tests"]["T2"]["quintile_spread"] > 40 and r["tests"]["T2"]["same_sign"]
    assert not r["tests"]["T1"]["pass"] and not r["tests"]["T4"]["pass"] and not r["tests"]["T5"]["pass"]


def test_planted_oi_to_tonight_is_found_and_only_t3():
    days, v, o, t, n = world(np.random.default_rng(2), beta_night=40.0)
    r = di.run(days, v, o, t, n)
    assert r["tests"]["T3"]["pass"] and not r["tests"]["T1"]["pass"] and not r["tests"]["T2"]["pass"] and not r["tests"]["T4"]["pass"]


def test_planted_settlement_day_effect_is_found_and_only_t5():
    days, v, o, t, n = world(np.random.default_rng(3), n=2400, sett_pts=120.0)
    r = di.run(days, v, o, t, n)
    assert r["tests"]["T5"]["pass"] and r["tests"]["T5"]["b"] == pytest.approx(120.0, abs=45) and r["tests"]["T5"]["events"] >= 100
    assert not r["tests"]["T6"]["pass"] and not r["tests"]["T7"]["pass"]


def test_planted_basis_to_next_day_is_found_and_only_t8():
    days, v, o, t, n = world(np.random.default_rng(4), basis_beta=40.0)
    r = di.run(days, v, o, t, n)
    assert r["tests"]["T8"]["pass"] and not r["tests"]["T2"]["pass"] and not r["tests"]["T5"]["pass"]


# ── 對齊與金絲雀 ─────────────────────────────────────────────────

def test_a_pcr_that_only_reacts_to_the_same_day_has_no_predictive_power():
    """PCR 只是對「當天」走勢的反應（沒有預測力）：對齊正確時 T2 不該過；故意錯一天（拿當天的報酬當目標）就會大幅顯著——證明檢定抓得到對齊的錯誤。"""
    hits = 0
    for seed in range(12):
        days, v, o, t, n = world(np.random.default_rng(100 + seed), contemporaneous=1.0)
        hits += di.run(days, v, o, t, n)["tests"]["T2"]["pass"]
    assert hits == 0
    days, v, o, t, n = world(np.random.default_rng(100), contemporaneous=1.0)
    dates = sorted(days)
    zs = di.zscores(v, dates)
    D = di.day_returns(days)
    ds = [d for d in dates if d >= di.START and d in zs]
    wrong = {"dates": ds, "x": np.array([zs[d] for d in ds]), "y": np.array([D[d] for d in ds])}              # 故意錯一天：同一天
    c = di.check(wrong, "slope")
    assert c["pass"] and abs(c["t"]) > 10


def test_targets_are_strictly_after_the_information():
    """手算：T2 的第 k 筆，x 是第 k 天的 z、y 是第 k+1 個交易日的日盤報酬；最後一天沒有下一天、不進樣本。"""
    days, v, o, t, n = world(np.random.default_rng(5), n=700)
    built = di.build(days, v, o, t, n)
    dates = sorted(days)
    D = di.day_returns(days)
    zs = di.zscores(v, dates)
    b = built["T2"]
    for d, x, y in list(zip(b["dates"], b["x"], b["y"]))[:5] + list(zip(b["dates"], b["x"], b["y"]))[-3:]:
        nxt = dates[dates.index(d) + 1]
        assert x == zs[d] and y == D[nxt]
    assert b["dates"][-1] != dates[-1]
    t1 = built["T1"]                                                                           # T1：同一天的夜盤（17:01 起）
    assert t1["dates"][0] >= di.START and all(d in n for d in t1["dates"])
    assert all(y == n[d] for d, y in zip(t1["dates"], t1["y"]))


def test_basis_is_a_percentage_of_the_index_then_z_scored():
    dates = bdays(8, "2025-03-03")
    taiex = {d: 10000.0 * (1 + i) for i, d in enumerate(dates)}                                  # 指數水位一路變大：基差要除以指數才可比
    pct = [0.001, 0.003, 0.002, 0.004, 0.001, 0.005, 0.002, 0.003]                              # 期貨比指數高的比例
    days = {d: day(100.0, taiex[d] * (1 + p), front="202503") for d, p in zip(dates, pct)}
    want = di.zscores(dict(zip(dates, pct)), dates, warm=3)
    raw = di.zscores({d: days[d].close["202503"] - taiex[d] for d in dates}, dates, warm=3)    # 沒除以指數的話 z 會完全不同
    b = di.build(days, {}, {}, taiex, {}, start=dates[0], warm=3)["T8"]
    assert list(b["dates"]) == dates[3:-1]                                                       # 最後一天沒有下一個交易日
    assert b["x"] == pytest.approx([want[d] for d in dates[3:-1]], abs=1e-9)
    assert not np.allclose([raw[d] for d in dates[3:-1]], b["x"], atol=1e-6)


def test_settlement_dummies_respect_the_start_date():
    rng = np.random.default_rng(9)
    days, v, o, t, n = world(rng, n=300)
    dates = sorted(days)
    cut = dates[150]
    b = di.build(days, v, o, t, n, start=cut)
    assert all(d >= cut for d in b["T5"]["dates"]) and b["T5"]["dates"][0] == cut
    assert any(d < cut for d in di.build(days, v, o, t, n, start=dates[0])["T5"]["dates"])


def test_halves_split_at_the_middle():
    n = 3000
    x = np.random.default_rng(1).normal(size=n)
    y = np.where(np.arange(n) < n // 2, 1.0, 2.0) * x                                            # 前半斜率 1、後半斜率 2（沒有雜訊）
    r = di.check({"dates": bdays(n, "2006-01-02"), "x": x, "y": y}, "slope")
    assert r["first"] == pytest.approx(1.0) and r["second"] == pytest.approx(2.0)


def test_a_strong_first_half_with_a_reversed_second_half_does_not_pass_even_with_a_big_t():
    n = 3000
    rng = np.random.default_rng(2)
    x = rng.normal(size=n)
    y = np.where(np.arange(n) < n // 2, 100.0, -3.0) * x + rng.normal(0, 100, n)                # 整體斜率約 +48、t 很大；後半是負的
    r = di.check({"dates": bdays(n, "2006-01-02"), "x": x, "y": y}, "slope")
    assert r["t"] > di.Z and r["first"] > 0 > r["second"] and not r["same_sign"] and not r["pass"]


def test_both_halves_must_also_agree_with_the_whole_sample():
    """兩半各自斜率為正、但兩半的水位差讓整體斜率是負的（辛普森悖論）：不算同號。"""
    n = 3000
    rng = np.random.default_rng(4)
    h = n // 2
    x = np.concatenate([rng.normal(-2, 0.5, h), rng.normal(2, 0.5, n - h)])
    y = np.concatenate([50 + 1.0 * x[:h], -50 + 1.0 * x[h:]]) + rng.normal(0, 1, n)
    r = di.check({"dates": bdays(n, "2006-01-02"), "x": x, "y": y}, "slope")
    assert r["first"] > 0 and r["second"] > 0 and r["b"] < 0 and abs(r["t"]) > di.Z and not r["same_sign"] and not r["pass"]


def test_quintile_spread_hand_case():
    x = np.arange(100, dtype=float)
    r = di.check({"dates": bdays(100, "2006-01-02"), "x": x, "y": x.copy()}, "slope")
    assert r["quintile_spread"] == pytest.approx(89.5 - 9.5)                                     # 最高 20%（80～99）平均 89.5 − 最低 20%（0～19）平均 9.5


def test_load_inputs_reads_the_right_series(monkeypatch):
    calls = []
    monkeypatch.setattr(di, "load_tx", lambda: {"d": "tx"})
    monkeypatch.setattr(di, "read_series", lambda path, source, series: calls.append((source, series)) or {series: 1.0})
    import core.night_history as nh
    monkeypatch.setattr(nh, "NightStore", lambda: type("S", (), {"nights": lambda self: {}})())
    tx, vol, oi, taiex, n17 = di.load_inputs()
    assert tx == {"d": "tx"} and vol == {"pcr.vol_ratio_pct": 1.0} and oi == {"pcr.oi_ratio_pct": 1.0} and taiex == {"taiex.close": 1.0} and n17 == {}
    assert calls == [("taifex", "pcr.vol_ratio_pct"), ("taifex", "pcr.oi_ratio_pct"), ("twse", "taiex.close")]


def test_settlement_dummies_mark_the_day_before_on_and_after():
    dates = bdays(40, "2025-03-03")
    days = {d: day(100.0, 100.0, extra={"202503": (100.0, 100.0)} if d <= dates[9] else {}) for d in dates}      # 202503 到 dates[9] 為止
    days = {d: Day("202503" if d <= dates[9] else "202504", {**({"202503": 100.0} if d <= dates[9] else {}), "202504": 100.0}, {**({"202503": 100.0} if d <= dates[9] else {}), "202504": 100.0}, {}, {}) for d in dates}
    b = di.build(days, {}, {}, {}, {}, start=dates[0])
    on, before, after = ([d for d, x in zip(b[k]["dates"], b[k]["x"]) if x == 1] for k in ("T5", "T6", "T7"))
    assert on == [dates[9]] and before == [dates[8]] and after == [dates[10]]


# ── 過關條件 ────────────────────────────────────────────────────

def xy(n, slope, noise=100.0, seed=0, flip_second=False):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 1, n)
    y = slope * x + rng.normal(0, noise, n)
    if flip_second:
        y[n // 2:] = -slope * x[n // 2:] + rng.normal(0, noise, n - n // 2)
    return {"dates": bdays(n, "2006-01-02"), "x": x, "y": y}


def test_check_needs_t_size_and_same_sign_halves():
    good = di.check(xy(3000, 30.0), "slope")
    assert good["pass"] and good["t"] > di.Z and good["same_sign"] and good["quintile_spread"] > 0
    weak = di.check(xy(3000, 3.0, seed=1), "slope")
    assert not weak["pass"] and abs(weak["t"]) < di.Z
    small = di.check(xy(150, 300.0, noise=50.0), "slope")                                        # t 很大但 < 200 天
    assert abs(small["t"]) > di.Z and not small["pass"]
    flip = di.check(xy(3000, 40.0, flip_second=True), "slope")                                   # 前半正、後半負
    assert not flip["pass"] and not flip["same_sign"]
    neg = di.check(xy(3000, -30.0, seed=2), "slope")
    assert neg["pass"] and neg["t"] < -di.Z and neg["b"] < 0                                     # 雙尾：負的也算（方向事先沒押）


def test_dummy_tests_need_100_events_and_none_is_not_enough():
    n = 3000
    rng = np.random.default_rng(3)
    x = np.zeros(n); x[::25] = 1.0                                                              # 120 個事件
    y = rng.normal(0, 100, n) + 90 * x
    t = {"dates": bdays(n, "2006-01-02"), "x": x, "y": y}
    assert di.check(t, "dummy")["pass"] and di.check(t, "dummy")["events"] == 120
    x2 = np.zeros(n); x2[::40] = 1.0                                                            # 75 個事件
    y2 = rng.normal(0, 100, n) + 400 * x2
    r = di.check({"dates": t["dates"], "x": x2, "y": y2}, "dummy")
    assert abs(r["t"]) > di.Z and r["events"] == 75 and not r["pass"]
    empty = di.check({"dates": t["dates"], "x": np.zeros(n), "y": y}, "dummy")                   # 一個事件都沒有
    assert not empty["pass"] and empty["t"] is None
    assert not di.check({"dates": [], "x": np.array([]), "y": np.array([])}, "slope")["pass"]


def test_bonferroni_threshold():
    assert di.N_TESTS == 8 and len(di.TESTS) == 8 and di.Z == pytest.approx(2.734, abs=1e-3)


def test_false_positive_rate_under_the_null_is_small():
    hits = 0
    for seed in range(40):
        days, v, o, t, n = world(np.random.default_rng(500 + seed), n=1000)
        hits += bool(di.run(days, v, o, t, n)["passed"])
    assert hits <= 4, hits                                                                       # 8 個檢定任一個誤報：期望 5%，40 次最多 4 次


# ── 報告、只跑一次、讀資料、靜態 ───────────────────────────────────

def test_report_and_run_once(tmp_path, monkeypatch, capsys):
    days, v, o, t, n = world(np.random.default_rng(6), n=700, beta_next=40.0)
    r = di.run(days, v, o, t, n)
    text = di.format_report(r)
    assert text.startswith("日資料小檢定（8 個、雙尾") and "T2 PCR 成交量比 z → 下一個日盤報酬" in text and "結論：" in text and "NaN" not in text
    monkeypatch.setattr(di, "RESULT_PATH", tmp_path / "r.json")
    calls = []
    monkeypatch.setattr(di, "load_inputs", lambda: calls.append(1) or (days, v, o, t, n))
    assert di.main(["run"]) == 0 and len(calls) == 1 and json.loads((tmp_path / "r.json").read_text())["passed"] == r["passed"]
    di.main(["run"])
    assert len(calls) == 1 and "已經跑過一次" in capsys.readouterr().out
    di.main(["run", "--rerun"])
    assert len(calls) == 2 and di.main([]) == 2


def test_read_series_takes_the_latest_revision_and_skips_missing(tmp_path):
    import sqlite3
    p = tmp_path / "intel.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE observations(source TEXT, series TEXT, event_date TEXT, value REAL, revision INTEGER)")
    c.executemany("INSERT INTO observations VALUES (?,?,?,?,?)", [("taifex", "pcr.x", "2025-01-02", 1.0, 0), ("taifex", "pcr.x", "2025-01-02", 2.0, 1), ("taifex", "pcr.x", "2025-01-03", None, 0),
                                                                    ("taifex", "pcr.x", "2025-01-06", 3.0, 0), ("twse", "pcr.x", "2025-01-02", 9.0, 0)])
    c.commit(); c.close()
    assert di.read_series(p, "taifex", "pcr.x") == {"2025-01-02": 2.0, "2025-01-06": 3.0}


def test_night17_returns_use_bars_121_to_840_and_the_quality_filter():
    bars = [(s, 100.0 if s < 121 else 101.0, 0, 0, 100.0 if s < 121 else (101.5 if s < 721 else 102.5), 1) for s in range(1, 841)]
    r = di.night17_returns({"2025-01-02": bars, "2025-01-03": bars[:500]})
    assert r == {"2025-01-02": pytest.approx((102.5 / 101.0 - 1) * 47000)}                       # 第 121 根開盤 101 → 第 840 根收盤 102.5（03:00 之後的也算）；17:00 之前的 100 不算；不到 600 根的晚上丟掉


def test_not_imported_by_trading_path_and_does_not_use_intel_reader():
    src = (ROOT / "core" / "daily_indicator_study.py").read_text(encoding="utf-8")
    assert "import intel_reader" not in src and "from core.intel_reader" not in src and "from core import intel_reader" not in src
    for sub in ("strategies", "api"):
        for f in (ROOT / sub).glob("*.py"):
            assert "daily_indicator_study" not in f.read_text(encoding="utf-8"), f
    for f in ("main.py", "core/broker.py", "core/risk.py", "core/manual_monitor.py"):
        p = ROOT / f
        if p.exists():
            assert "daily_indicator_study" not in p.read_text(encoding="utf-8"), f
