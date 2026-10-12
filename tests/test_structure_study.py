"""core/structure_study.py：缺口與前日高低的檢驗。

這些數字會被拿來決定「有沒有值得寫成策略的現象」，所以重點是檢驗本身不會騙人：
  - 有現象的資料（植入的續行／回補）要檢得出來，沒有現象的資料（隨機漫步）不能亂報（假陽性率）；
  - 不偷看未來（參考振幅只用前幾天、事件看「收盤」、進場價是突破那根的收盤）；
  - 對照要公平：整體漲勢、時段的漲勢不能被當成『突破的效果』；
  - 向量化的實作要和最笨的逐筆算法一致。全部用手算得出答案或固定種子的合成資料。"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

import numpy as np
import pytest

import core.structure_study as ss
from core.backtest import ref_ranges
from core.market_store import MarketStore


def day_name(k):
    return (date(2026, 1, 5) + timedelta(days=k)).isoformat()


def mkbars(d, closes, wicks=None, start="08:45"):
    """每根 K 棒的開盤＝前一根收盤（連續）；wicks={索引: (高, 低)} 可以單獨把某根的影線拉長。"""
    t0 = datetime.strptime(f"{d} {start}", "%Y-%m-%d %H:%M").timestamp()
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        h, l = max(o, c), min(o, c)
        if wicks and i in wicks:
            h, l = max(h, wicks[i][0]), min(l, wicks[i][1])
        bars.append((t0 + 60 * i, float(o), float(h), float(l), float(c), 10))
        prev = c
    return bars


def build(paths, wicks=None):
    return {day_name(k): mkbars(day_name(k), c, (wicks or {}).get(k)) for k, c in enumerate(paths)}


def gap_days(n, move_rule, seed=1, up_share=0.5, base=1000.0):
    """每天：開盤＝昨收＋跳空 g（正負隨機、50~250 點），開盤到收盤是一條直線，移動量由 move_rule(g, rng) 決定。"""
    rng = random.Random(seed)
    paths, prev = [], base
    for _ in range(n):
        g = (1 if rng.random() < up_share else -1) * rng.uniform(50, 250)
        o = prev + g
        mv = move_rule(g, rng)
        c = list(np.linspace(o, o + mv, 300))
        paths.append(c)
        prev = c[-1]
    return build(paths)


def sgn(x):
    return 1 if x > 0 else -1


# ── 哪些天算數 ────────────────────────────────────────────────────

def test_the_first_days_and_incomplete_days_are_left_out():
    paths = [list(np.linspace(1000, 1010 + k, 300)) for k in range(25)]
    paths[22] = paths[22][:100]                                              # 第 23 天只有 100 根（提早收盤）
    sessions = ss._sessions(build(paths))
    used = [s["date"] for s, _ in ss._usable(sessions)]
    # 前 20 天算不出參考振幅；第 23 天本身不完整；第 24 天的昨天（第 23 天）不完整，缺口與前日高低都沒有意義
    assert used == [day_name(20), day_name(21), day_name(24)]


# ── 缺口 ──────────────────────────────────────────────────────────

def test_gaps_that_always_fill_are_detected_as_reversal():
    g = ss.gap_study(gap_days(60, lambda g, rng: -sgn(g) * rng.uniform(60, 100)))
    a = g["tests"]["all"]
    assert a["t"] < -10 and a["verdict"] == "過關" and -1.4 < a["mean"] < -0.6 and a["points"] < -40       # 平均每天往回補約 1 個 R
    assert all(g["tests"][k]["verdict"] == "過關" for k in ("small", "medium", "large"))


def test_gaps_that_always_continue_are_detected_as_continuation():
    g = ss.gap_study(gap_days(60, lambda g, rng: sgn(g) * rng.uniform(60, 100)))
    assert g["tests"]["all"]["t"] > 10 and g["tests"]["all"]["verdict"] == "過關" and g["tests"]["all"]["mean"] > 0.6


def test_gaps_unrelated_to_the_day_are_not_flagged_and_the_false_positive_rate_is_small():
    # 用真實資料的規模（270 天→約 250 筆、每個分組約 83 筆）：樣本太小時 t 分布的尾巴比常態厚，假陽性率本來就會偏高
    hits, ts = 0, []
    for seed in range(40):
        g = ss.gap_study(gap_days(270, lambda g, rng: rng.gauss(0, 80), seed=seed))
        for st in g["tests"].values():
            ts.append(st["t"])
            hits += st["verdict"] == "過關"
    assert hits <= 4, hits                                                      # 160 個檢定，每個 0.6% → 預期約 1 個
    assert abs(float(np.mean(ts))) < 0.3 and 0.75 < float(np.std(ts)) < 1.3    # t 值大致是標準常態


def test_the_overall_up_drift_is_removed_so_it_cannot_masquerade_as_continuation():
    """每天日內都漲約 1 個 R，而且 75% 是上跳空；沒扣整體漲勢的話，續行的平均會是 +0.5。"""
    g = ss.gap_study(gap_days(70, lambda g, rng: 40 + rng.gauss(0, 8), up_share=0.75))
    assert 0.8 < g["drift_R"] < 1.2
    assert abs(g["tests"]["all"]["mean"]) < 0.15 and g["tests"]["all"]["verdict"] != "過關"


def test_gap_buckets_are_equal_sized_thirds_ordered_by_gap_size_and_days_without_a_gap_are_skipped():
    paths, prev = [], 1000.0
    for k in range(60):
        g = 0.0 if k in (30, 31) else (1 if k % 2 else -1) * (50 + 5 * k)
        o = prev + g
        c = list(np.linspace(o, o + 20 + k % 7, 300))
        paths.append(c)
        prev = c[-1]
    g = ss.gap_study(build(paths))
    t = g["tests"]
    sizes = [t[k]["n"] for k in ("small", "medium", "large")]
    assert sum(sizes) == t["all"]["n"] == g["n"] and max(sizes) - min(sizes) <= 1
    assert g["n"] == 60 - 20 - 2                                                # 扣掉前 20 天與兩個沒有缺口的日子
    assert t["small"]["abs_G"][1] <= t["medium"]["abs_G"][0] <= t["medium"]["abs_G"][1] <= t["large"]["abs_G"][0]


def test_the_fill_rate_is_the_share_of_days_that_trade_back_to_yesterdays_close():
    filled = ss.gap_study(gap_days(50, lambda g, rng: -sgn(g) * (abs(g) + 30)))       # 一定補到昨收
    held = ss.gap_study(gap_days(50, lambda g, rng: sgn(g) * 80))                      # 往跳空方向走，不會回到昨收
    assert filled["tests"]["all"]["fill_rate"] == 1.0 and held["tests"]["all"]["fill_rate"] == 0.0


def test_too_little_data_is_an_error_not_a_result():
    with pytest.raises(ValueError, match="太少"):
        ss.gap_study(gap_days(25, lambda g, rng: 0.0))
    with pytest.raises(ValueError, match="太少"):
        ss.level_study(gap_days(25, lambda g, rng: 0.0))


# ── 前日高低 ──────────────────────────────────────────────────────

def walk_days(n, seed, cont=0.0, tod_drift=0.0, sigma=1.5):
    """隨機漫步的日子；cont≠0＝突破（收盤）之後的 60 根每根多走 cont（植入『續行』或『失敗』）；tod_drift＝前 100 根每根的漲勢（時段效應，和突破無關）。"""
    rng = random.Random(seed)
    paths, pdh, pdl, px = [], None, None, 1000.0
    for _ in range(n):
        closes, ramp_until, ramp_sign, up, dn = [], -1, 0, False, False
        for j in range(300):
            step = rng.gauss(0, sigma) + (tod_drift if j < 100 else 0.0) + (ramp_sign * cont if j <= ramp_until else 0.0)
            px += step
            closes.append(px)
            if pdh is not None and not up and px > pdh:
                up, ramp_until, ramp_sign = True, j + 60, 1
            elif pdl is not None and not dn and px < pdl:
                dn, ramp_until, ramp_sign = True, j + 60, -1
        paths.append(closes)
        pdh, pdl = max(closes), min(closes)
        px = closes[-1] + rng.gauss(0, 5)
    return build(paths)


def test_a_planted_continuation_after_breaks_is_detected_in_both_directions():
    v = ss.level_study(walk_days(120, seed=3, cont=0.8))
    for name in ("up_30", "up_60", "down_30", "down_60"):
        st = v["tests"][name]
        assert st["n"] > 20 and st["t"] > 5 and st["verdict"] == "過關" and st["points"] > 0, (name, st)


def test_a_planted_failure_after_breaks_is_detected_as_negative():
    v = ss.level_study(walk_days(120, seed=3, cont=-0.8))
    assert all(v["tests"][k]["t"] < -5 for k in ("up_30", "up_60", "down_30", "down_60"))


def test_random_walks_do_not_produce_fake_break_effects():
    """對照的設計是這項檢驗最容易出錯的地方：用同一天的時間點當對照會讓『突破後失敗』看起來很顯著。隨機漫步上，t 要大致是標準常態。"""
    ts, hits = [], 0
    for seed in range(25):
        v = ss.level_study(walk_days(270, seed=seed))
        for st in v["tests"].values():
            ts.append(st["t"])
            hits += st["verdict"] == "過關"
    assert hits <= 3, hits                                                      # 100 個檢定，預期不到 1 個
    assert abs(float(np.mean(ts))) < 0.35 and 0.7 < float(np.std(ts)) < 1.4, (np.mean(ts), np.std(ts))


def test_a_morning_trend_that_has_nothing_to_do_with_breaks_is_controlled_for():
    """早盤每根都在漲（時段效應）→ 向上突破之後的移動原始值是正的；但其他天的同一時段也在漲，扣掉對照就沒有『突破的效果』。"""
    ts, raw = [], []
    for seed in range(20):
        v = ss.level_study(walk_days(80, seed=seed, tod_drift=0.4))
        ts.append(v["tests"]["up_30"]["t"])
        raw.append(v["tests"]["up_30"]["mean_raw"])
    assert float(np.mean(raw)) > 0.1                                         # 原始值明顯是正的（容易被誤認為突破有效）
    assert abs(float(np.mean(ts))) < 0.8                                     # 扣掉同時段的對照後，不顯著


def test_the_event_is_the_first_close_beyond_the_level_and_the_entry_is_that_close():
    """A 日最高 1050；B 日在第 50 根收在 1051（突破），之後每根 +1 到 1101 再持平。X30＝c[80]-c[50]＝30、X60＝c[110]-c[50]＝50（再除以當天的 R）。"""
    a = [1000.0] * 50 + list(np.linspace(1000, 1050, 51)[1:]) + [1050.0] * 200                 # 第 50~99 根爬到 1050，之後持平
    b = [1000.0] * 50 + list(np.arange(1051, 1102)) + [1101.0] * 199                          # 第 50 根 1051，第 100 根 1101
    assert len(a) == len(b) == 300
    wicks = {k: {3: (1200.0, 1000.0)} for k in range(1, 60, 2)}             # B 日第 3 根的影線刺到 1200（只有影線、收盤沒有）→ 不算突破
    days = build([a if k % 2 == 0 else b for k in range(60)], wicks)
    v = ss.level_study(days)
    refs = ref_ranges(days, 20)
    keys = list(days)
    b_days = [i for i in range(21, 60) if i % 2 == 1]
    want30 = [30.0 / refs[i] for i in b_days]
    # 前日高低：B 日的昨天是 A 日（最高 1050）→ 突破；A 日的昨天是 B 日（最高因為影線 1200，不會被突破）→ 沒有事件
    assert v["days_with_up_break"] == len(b_days) and v["days_with_down_break"] == 0
    assert v["tests"]["up_30"]["n"] == len(b_days) and v["tests"]["up_30"]["mean_raw"] == pytest.approx(float(np.mean(want30)))
    assert v["tests"]["up_60"]["mean_raw"] == pytest.approx(float(np.mean([50.0 / refs[i] for i in b_days])))
    assert keys[21] == day_name(21)


def test_events_in_the_last_minutes_are_dropped_and_forward_windows_are_cut_at_the_close():
    def day(break_at):
        p = [1000.0] * 300
        for j in range(break_at, 300):
            p[j] = 1100.0
        return p
    flat = [1000.0] * 300
    for brk, expect_events in ((294, True), (295, False)):                      # 300 根：最後一根索引 299；299-5＝294 之內才算
        days = build([flat if k % 2 == 0 else day(brk) for k in range(60)])
        v = ss.level_study(days)
        assert (v["days_with_up_break"] > 0) == expect_events, brk
    # 第 270 根突破、看 60 分鐘 → 超過收盤，用收盤價（只有 29 根可以看）
    days = build([flat if k % 2 == 0 else [1000.0] * 270 + list(np.linspace(1001, 1100, 30)) for k in range(60)])
    v = ss.level_study(days)
    refs = ref_ranges(days, 20)
    b_days = [i for i in range(21, 60) if i % 2 == 1]
    assert v["tests"]["up_60"]["mean_raw"] == pytest.approx(float(np.mean([(1100.0 - 1001.0) / refs[i] for i in b_days])))


def brute_level(days, k, window=ss.NULL_WINDOW):
    """最笨的逐筆算法（逐天逐根迴圈）當對照。"""
    sess = ss._sessions(days)
    pairs = ss._usable(sess)
    data = [(np.array([b[4] for b in s["bars"]]), s["ref"], p["high"], p["low"]) for s, p in pairs]

    def fwd(c, r, j):
        return (c[min(j + k, len(c) - 1)] - c[j]) / r

    out = {1: [], -1: []}
    for i, (c, r, hi, lo_) in enumerate(data):
        last = len(c) - 1
        for dirn, hit in ((1, [j for j in range(len(c)) if c[j] > hi]), (-1, [j for j in range(len(c)) if c[j] < lo_])):
            if not hit or hit[0] > last - ss.TAIL_BARS:
                continue
            b = hit[0]
            lo, up = max(0, b - window), min(last - ss.TAIL_BARS, b + window)
            vals = [fwd(c2, r2, j) for i2, (c2, r2, _, _) in enumerate(data) if i2 != i for j in range(lo, min(up, len(c2) - 1 - ss.TAIL_BARS) + 1)]
            out[dirn].append((dirn * fwd(c, r, b), dirn * float(np.mean(vals))))
    return out


def test_the_vectorised_study_matches_a_brute_force_implementation():
    days = walk_days(45, seed=11, cont=0.3)
    v = ss.level_study(days, horizons=(30, 60))
    for k in (30, 60):
        ref = brute_level(days, k)
        for dirn, name in ((1, f"up_{k}"), (-1, f"down_{k}")):
            st = v["tests"][name]
            assert st["n"] == len(ref[dirn])
            assert st["mean_raw"] == pytest.approx(float(np.mean([x for x, _ in ref[dirn]])), rel=1e-9)
            assert st["mean_null"] == pytest.approx(float(np.mean([nl for _, nl in ref[dirn]])), rel=1e-9)


def test_the_study_is_deterministic():
    days = walk_days(60, seed=5)
    assert ss.level_study(days) == ss.level_study(days) and ss.gap_study(days) == ss.gap_study(days)


# ── 判定與報告 ────────────────────────────────────────────────────

@pytest.mark.parametrize("t,expected", [(2.74, "過關"), (-2.74, "過關"), (2.70, "線索"), (-2.1, "線索"), (2.0, "線索"), (1.99, "沒有"), (0.0, "沒有")])
def test_verdict_thresholds(t, expected):
    assert ss.verdict(t) == expected
    assert 2.73 < ss.Z_CLAIM < 2.74 and ss.N_TESTS == 8                          # 事先固定的：8 個檢定，Bonferroni 後雙尾 5%


def test_summary_names_the_passing_tests_and_warns_when_the_number_of_tests_changed():
    full = {"gap": {"tests": {n: {"verdict": "沒有"} for n in ("all", "small", "medium", "large")}},
            "levels": {"tests": {n: {"verdict": "沒有"} for n in ("up_30", "up_60", "down_30", "down_60")}}}
    assert "過關 0 個" in ss.summary(full) and "沒有任何檢定過關" in ss.summary(full) and "不同" not in ss.summary(full)
    full["levels"]["tests"]["up_30"]["verdict"] = "過關"
    full["gap"]["tests"]["all"]["verdict"] = "線索"
    text = ss.summary(full)
    assert "過關 1 個 ['levels:up_30']" in text and "線索 1 個 ['gap:all']" in text and "沒有任何檢定過關" not in text
    assert "和事先固定的 8 個不同" in ss.summary({"gap": full["gap"]})                # 只跑缺口＝只有 4 個，Bonferroni 門檻要重算


# ── 命令列 ────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    ms = MarketStore(tmp_path / "ms.db")
    rows = []
    for d, bars in walk_days(60, seed=2).items():
        for ts, o, h, l, c, v in bars:
            end = datetime.fromtimestamp(ts + 60)
            rows.append({"date": d, "hhmm": end.hour * 100 + end.minute, "open": o, "high": h, "low": l, "close": c, "volume": v})
    ms.upsert_bars_1m("TXF", rows)
    return str(tmp_path / "ms.db")


def test_cli_prints_both_studies_and_the_verdict_and_writes_json(db, tmp_path, capsys):
    out = tmp_path / "o.json"
    assert ss.main(["--db", db, "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "缺口" in text and "前日高低" in text and "判定（8 個檢定" in text and "實際跑了 8 個" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"gap", "levels"} and set(data["gap"]["tests"]) == {"all", "small", "medium", "large"}
    assert set(data["levels"]["tests"]) == {"up_30", "up_60", "down_30", "down_60"}


def test_cli_runs_one_study_and_reports_errors(db, tmp_path, capsys):
    assert ss.main(["--db", db, "gap"]) == 0
    text = capsys.readouterr().out
    assert "缺口" in text and "前日高低" not in text and "和事先固定的 8 個不同" in text
    assert ss.main(["--db", str(tmp_path / "empty.db")]) == 2
    assert "沒有 K 棒資料" in capsys.readouterr().err


def test_a_close_exactly_at_the_previous_high_or_low_is_not_a_break():
    a = [1000.0] * 50 + [1050.0] * 250                                        # 昨天：最高 1050、最低 1000
    on_high = [1000.0] * 50 + [1050.0] * 250                                   # 今天收盤剛好等於 1050，從沒超過
    on_low = [1000.0] * 300
    v = ss.level_study(build([a if k % 2 == 0 else on_high for k in range(60)]))
    assert v["days_with_up_break"] == 0 and v["days_with_down_break"] == 0     # 要『超過』才算，等於不算
    v = ss.level_study(build([a if k % 2 == 0 else on_low for k in range(60)]))
    assert v["days_with_down_break"] == 0                                      # 收盤剛好等於昨天最低 1000，不算向下突破


def late_break_days(n, seed, lengths=(285, 290, 300)):
    """每天長度不同（285~300 根，都算完整的一天）；奇數天在第 272 根往上跳 → 收盤前不遠才突破，對照視窗會碰到『其他天比較短』的尾端。"""
    rng = random.Random(seed)
    paths, px = [], 1000.0
    for k in range(n):
        m, closes = rng.choice(lengths), []
        for j in range(m):
            px += rng.gauss(0, 1.5) + (60.0 if (k % 2 == 1 and j == 272) else 0.0)
            closes.append(px)
        paths.append(closes)
        px = closes[-1] + rng.gauss(0, 5)
    return build(paths)


def test_the_study_matches_brute_force_when_days_have_different_lengths_and_events_are_late():
    days = late_break_days(50, seed=3)
    v = ss.level_study(days, horizons=(30, 60))
    assert v["days_with_up_break"] >= 5                                              # 確實有晚一點的事件
    for k in (30, 60):
        ref = brute_level(days, k)
        for dirn, name in ((1, f"up_{k}"), (-1, f"down_{k}")):
            st = v["tests"][name]
            assert st["n"] == len(ref[dirn])
            assert st["mean_null"] == pytest.approx(float(np.mean([nl for _, nl in ref[dirn]])), rel=1e-9)


# ── 獨立驗證（confirm）：事先固定的單一單尾檢定 ─────────────────────────────

def test_the_confirm_threshold_is_one_sided_five_percent_and_needs_ten_points():
    assert 1.644 < ss.Z_ONE_SIDED < 1.646 and ss.CONFIRM_MIN_POINTS == 10.0


def test_pooled_excess_is_the_combination_of_up_and_down_events():
    days = walk_days(60, seed=11, cont=0.3)
    v = ss.level_study(days, horizons=(30,))
    ref = brute_level(days, 30)
    both = [x - nl for d in (1, -1) for x, nl in ref[d]]
    pooled = v["pooled"]["30"]
    assert pooled["n"] == len(both) == v["tests"]["up_30"]["n"] + v["tests"]["down_30"]["n"]
    assert pooled["mean"] == pytest.approx(float(np.mean(both)), rel=1e-9)


def test_confirm_passes_when_breaks_fail_and_says_the_opposite_when_they_continue():
    fail = ss.confirm(walk_days(120, seed=3, cont=-0.8))
    assert fail["decision"] == "通過" and fail["t"] < -5 and fail["points"] < 0 and fail["horizon"] == 30
    cont = ss.confirm(walk_days(120, seed=3, cont=0.8))
    assert cont["decision"] == "相反方向顯著" and cont["big_enough"] is False                 # 續行不是我們要驗證的方向，更不能當成通過
    assert fail["up_events"] > 10 and fail["down_events"] > 10


def test_a_failure_smaller_than_ten_points_does_not_count_as_big_enough():
    c = ss.confirm(walk_days(120, seed=3, cont=-0.8))
    assert c["big_enough"] == (c["points"] <= -10.0)
    weak = ss.confirm(walk_days(270, seed=3, cont=-0.05))                                   # 很小的失敗傾向：方向對，但幅度不夠
    assert weak["points"] > -10.0 and weak["big_enough"] is False


def test_confirm_false_positive_rate_on_random_walks_is_about_five_percent_one_sided():
    hits = 0
    for seed in range(40):
        hits += ss.confirm(walk_days(270, seed=seed))["decision"] == "通過"
    assert hits <= 8, hits                                                                  # 預期約 2 個（5%）；只做一次的單一檢定，不是 8 個


def test_confirm_uses_only_the_window_it_is_given(tmp_path, capsys):
    """前半是『突破後失敗』、後半是『突破後續行』的資料：指定哪一段，就只看那一段。"""
    ms = MarketStore(tmp_path / "ms.db")
    first, second = walk_days(150, seed=5, cont=-0.8), walk_days(150, seed=6, cont=0.8)
    rows, k = [], 0
    for part in (first, second):
        for _, bars in part.items():
            d = (date(2020, 3, 2) + timedelta(days=k)).isoformat()
            k += 1
            for ts, o, h, l, c, v in bars:
                end = datetime.fromtimestamp(ts + 60)
                rows.append({"date": d, "hhmm": end.hour * 100 + end.minute, "open": o, "high": h, "low": l, "close": c, "volume": v})
    ms.upsert_bars_1m("TXF", rows)
    db = str(tmp_path / "ms.db")
    d_first_end, d_second_start = (date(2020, 3, 2) + timedelta(days=149)).isoformat(), (date(2020, 3, 2) + timedelta(days=150)).isoformat()
    assert ss.main(["confirm", "--db", db, "--start", "2020-03-02", "--end", d_first_end]) == 0
    out1 = capsys.readouterr().out
    assert "[通過]" in out1 and "單一事先固定的檢定" in out1
    assert ss.main(["confirm", "--db", db, "--start", d_second_start, "--end", "2099-01-01"]) == 0
    assert "[相反方向顯著]" in capsys.readouterr().out


def test_confirm_requires_an_explicit_window(tmp_path, capsys):
    MarketStore(tmp_path / "ms.db")
    assert ss.main(["confirm", "--db", str(tmp_path / "ms.db")]) == 2
    assert "--start 和 --end" in capsys.readouterr().err
