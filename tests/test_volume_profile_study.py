"""前一日價量分布算出的價位，今天的價格有沒有表現得不一樣（core/volume_profile_study.py，update.md 發現 78）。

判定標準登記在 update.md 發現 78：這裡把登記的定義（格寬、合格條件、對照組、過關條件）逐項釘住，
再用合成的世界驗證檢定本身——植入的磁吸／擋住要偵測得到、無關的世界假陽性率要低、上下不對稱（長期下跌）的世界不能誤判、偷看的金絲雀要大。"""
from __future__ import annotations

import ast
import random
import sqlite3
from datetime import date, datetime, timedelta
from math import sqrt
from pathlib import Path

import pytest

from core import volume_profile_study as st

ROOT = Path(__file__).resolve().parent.parent


def bar(hhmm, o, h, l, c, v):
    return (hhmm, float(o), float(h), float(l), float(c), float(v))


# ── 登記的常數 ────────────────────────────────────────────────────

def test_registered_constants():
    assert (st.BIN_FRAC, st.D_MIN, st.E_MIN, st.MIN_N, st.MIN_EFFECT, st.MIN_BARS, st.BLOCK, st.N_TESTS) == (0.0002, 0.2, 0.05, 300, 0.03, 280, 30, 2)
    assert st.Z == pytest.approx(1.95996, abs=1e-4)
    from core import volume_profile as vp
    assert vp.VA_FRAC == 0.70                                                         # 價值區 70%（和畫面顯示同一份定義）


# ── 價量分布的重建 ────────────────────────────────────────────────

def test_bar_volumes_uniform_and_typical():
    b = [bar(1, 0, 125, 105, 110, 30), bar(2, 0, 107, 107, 107, 4), bar(3, 0, 200, 100, 150, 0)]
    u = st.bar_volumes(b, 10, "uniform")
    assert u == {10: 10 + 4, 11: 10, 12: 10}                                          # 第一根跨 100～110、110～120、120～130 三格各 10；第二根高低相同全放 100～110；量 0 的不算
    t = st.bar_volumes(b, 10, "typical")
    assert t == {11: 30, 10: 4}                                                       # 典型價 (125+105+110)/3 ＝ 113.3 → 110～120 那格
    assert sum(u.values()) == sum(t.values()) == 34


def test_day_levels_bin_size_follows_the_reference_price():
    flat = [bar(i, 0, 100 + 0.0, 100.0, 100.0, 1) for i in range(5)]
    assert st.day_levels(flat, 47000)["pbin"] == 9 and st.day_levels(flat, 12000)["pbin"] == 2 and st.day_levels(flat, 100)["pbin"] == 1        # 0.02%，至少 1 點
    assert st.day_levels(flat, 47000, 0.0003)["pbin"] == 14
    assert st.day_levels(flat, 48000)["pbin"] == 10                                    # 9.6 → 10：四捨五入，不是截斷成 9
    assert st.day_levels([], 47000) is None and st.day_levels([bar(1, 0, 10, 9, 9, 0)], 47000) is None


def test_day_levels_by_hand():
    # 格寬 10（ref 50000 × 0.0002）：量集中在 46300～46310（POC），上面有少量
    bars = [bar(1, 0, 46309, 46300, 46305, 100), bar(2, 0, 46319, 46310, 46315, 30), bar(3, 0, 46329, 46320, 46325, 10), bar(4, 0, 46289, 46280, 46285, 20)]
    lv = st.day_levels(bars, 50000)
    assert lv["pbin"] == 10 and lv["poc"] == 46305.0                                  # POC＝46300～46310 的中心
    assert (lv["val"], lv["vah"]) == (46300, 46330)                                   # 160 的 70% ＝ 112：POC 100，上面兩格 40 > 下面兩格 20 → 往上加（30＋10）→ 140 ≥ 112 就停，下面那格（20）沒加進來
    assert lv["val"] <= lv["poc"] <= lv["vah"]


def test_poc_tie_is_broken_toward_the_reference_price():
    bars = [bar(1, 0, 52309, 52300, 52305, 50), bar(2, 0, 52409, 52400, 52405, 50)]      # 兩格同量（格寬 10）
    assert st.day_levels(bars, 52000)["poc"] == 52305.0                                 # 前一日收盤在下面：取離它近的（下面那格）
    assert st.day_levels(bars, 52500)["poc"] == 52405.0                                 # 在上面：取上面那格


# ── 80% 規則（只描述）────────────────────────────────────────────

def day_bars(opens=130.0, closes=None, fill=110.0):
    """300 根；第 30×(j+1)−1 根的收盤是 closes[j]。"""
    closes = closes or [fill] * 10
    out = []
    for i in range(300):
        c = closes[(i + 1) // 30 - 1] if (i + 1) % 30 == 0 else fill
        out.append(bar(846 + i, opens if i == 0 else fill, fill + 0.5, fill - 0.5, c, 10))
    return out


LV = {"val": 100.0, "vah": 120.0}


def test_rule80_cases():
    assert st.rule80(day_bars(opens=110.0), LV) is None                                # 開盤在價值區內：不適用
    assert st.rule80(day_bars(opens=100.0), LV) is None and st.rule80(day_bars(opens=120.0), LV) is None      # 剛好在邊緣也算在內
    above = [125, 118, 110, 112, 112, 112, 112, 112, 112, 112]
    b = day_bars(130.0, above)
    assert st.rule80(b, LV) == {"above": True, "entered": True, "reached": False}      # 連兩個收盤（118、110）在區內，之後沒碰到下緣
    b2 = list(b)
    b2[150] = bar(1000, 112, 113, 99.0, 112, 10)                                       # 第 90 根之後有一根低點 99 ≤ VAL
    assert st.rule80(b2, LV) == {"above": True, "entered": True, "reached": True}
    b3 = list(b)
    b3[40] = bar(900, 112, 113, 99.0, 112, 10)                                         # 低點在「進入」之前（第 40 根）：不算
    assert st.rule80(b3, LV)["reached"] is False
    assert st.rule80(day_bars(130.0, [125, 118, 125, 118, 125, 118, 125, 118, 125, 118]), LV) == {"above": True, "entered": False, "reached": False}   # 從沒連兩個在區內
    below = day_bars(90.0, [95, 105, 110, 112, 112, 112, 112, 112, 112, 112])
    below[200] = bar(1100, 112, 121.0, 111, 112, 10)
    assert st.rule80(below, LV) == {"above": False, "entered": True, "reached": True}  # 從下方進來，要碰到上緣


# ── 建立每天的資料 ────────────────────────────────────────────────

def synthetic_bars(dates, seed=0, base=20000.0):
    rng = random.Random(seed)
    out, p = {}, base
    for d in dates:
        bars_ = []
        for i in range(300):
            o = p
            p = max(100.0, p + rng.gauss(0, 6))
            bars_.append(bar(846 + i, o, max(o, p) + rng.random() * 2, min(o, p) - rng.random() * 2, p, rng.randint(1, 50)))
        out[d] = bars_
    return out


def trading_days(n, start=date(2024, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_build_days_fields_and_skips():
    days = trading_days(9)
    bars = synthetic_bars(days)
    del bars[days[2]]                                                                   # 第 3 天沒有資料 → 第 4 天的前一天不在
    bars[days[5]] = bars[days[5]][:100]                                                 # 第 6 天不滿 280 根 → 它自己與隔天（前一天太短）都略過
    out, sk = st.build_days(bars, days, monthly={days[7]})                              # 第 8 天是結算日 → 它與隔天都略過
    assert sk == {"no_prev": 2, "roll": 2, "short": 2, "no_volume": 0}, sk              # no_prev：第 1 天（沒有前一天）、第 4 天（前一天沒資料）
    assert [d["date"] for d in out] == [days[1], days[4]]
    d = out[0]
    prev = bars[days[0]]
    assert d["O"] == bars[days[1]][0][1] and d["C"] == bars[days[1]][-1][4]
    assert d["H"] == max(b[2] for b in bars[days[1]]) and d["L"] == min(b[3] for b in bars[days[1]])
    assert d["R"] == max(b[2] for b in prev) - min(b[3] for b in prev)
    lv = st.day_levels(prev, prev[-1][4])
    assert (d["poc"], d["vah"], d["val"]) == (lv["poc"], lv["vah"], lv["val"])         # 用的是「前一天」的價量分布
    lv_d = st.day_levels(bars[days[1]], prev[-1][4])
    assert (d["poc_d"], d["vah_d"], d["val_d"]) == (lv_d["poc"], lv_d["vah"], lv_d["val"]) and d["poc_d"] != d["poc"]


def test_previous_day_is_the_previous_trading_day_in_the_calendar_not_the_previous_bars_day():
    days = trading_days(5)
    bars = synthetic_bars(days)
    del bars[days[1]]
    out, sk = st.build_days(bars, days, set())                                          # 第 3 天（days[2]）的前一個交易日是 days[1]，沒有資料 → 略過；不能拿 days[0] 湊
    assert [d["date"] for d in out] == [days[3], days[4]]
    assert days[2] not in [d["date"] for d in out] and sk["no_prev"] == 2


def test_build_days_skips_a_day_without_volume():
    days = trading_days(3)
    bars = synthetic_bars(days)
    bars[days[0]] = [(b[0], b[1], b[2], b[3], b[4], 0.0) for b in bars[days[0]]]
    out, sk = st.build_days(bars, days, set())
    assert sk["no_volume"] == 1 and [d["date"] for d in out] == [days[2]]


def test_load_bars_reads_only_txf_day_session_valid_rows(tmp_path):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE bars_1m(code TEXT NOT NULL, date TEXT NOT NULL, hhmm INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume INTEGER, PRIMARY KEY(code, date, hhmm))")
    rows = [("TXF", "2026-11-03", 846, 100, 101, 99, 100, 5), ("TXF", "2026-11-03", 847, 100, 101, 99, 100, None), ("TXF", "2026-11-03", 845, 1, 1, 1, 1, 1),
            ("TXF", "2026-11-03", 1346, 1, 1, 1, 1, 1), ("MXF", "2026-11-03", 900, 1, 1, 1, 1, 1), ("TXF", "2026-11-03", 900, 100, 99, 101, 100, 5),
            ("TXF", "2026-11-03", 901, 0, 101, 99, 100, 5), ("TXF", "2026-11-02", 1345, 100, 102, 98, 101, 7)]
    c.executemany("INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    out = st.load_bars(db)
    assert out["2026-11-03"] == [(846, 100.0, 101.0, 99.0, 100.0, 5.0), (847, 100.0, 101.0, 99.0, 100.0, 0.0)] and out["2026-11-02"] == [(1345, 100.0, 102.0, 98.0, 101.0, 7.0)]


# ── 統計量（手算）────────────────────────────────────────────────

def test_strat_stat_by_hand():
    g = {"a": [("d1", 1.0), ("d2", 0.0), ("d3", 1.0), ("d4", 1.0)], "b": [("d5", 0.0), ("d6", -1.0)]}
    r = st.strat_stat(g, {"a": 0.5, "b": 0.5})
    assert r["stat"] == pytest.approx(0.5 * 0.75 + 0.5 * (-0.5))                         # 各組平均 0.75 與 −0.5，等權
    infl_a = [0.5 * (v - 0.75) / 4 for v in (1, 0, 1, 1)]
    infl_b = [0.5 * (v + 0.5) / 2 for v in (0, -1)]
    assert r["se"] == pytest.approx(sqrt(sum(x * x for x in infl_a + infl_b)))
    assert r["t"] == pytest.approx(r["stat"] / r["se"]) and r["n"] == {"a": 4, "b": 2}
    assert st.strat_stat({"a": [("d1", 1.0)], "b": g["b"]}, {"a": 0.5, "b": 0.5}) is None          # 任何一組少於 2 個：沒有結果
    assert st.strat_stat({"a": g["a"], "b": []}, {"a": 0.5, "b": 0.5}) is None


def test_strat_stat_clusters_the_same_day():
    """同一天的兩個值不獨立：影響函數先加總再平方（正相關的兩個值會讓標準誤變大）。"""
    same = {"a": [("d1", 1.0), ("d1", 1.0), ("d2", 0.0), ("d2", 0.0)], "b": [("d3", 0.0), ("d4", 1.0)]}
    apart = {"a": [("d1", 1.0), ("d5", 1.0), ("d2", 0.0), ("d6", 0.0)], "b": [("d3", 0.0), ("d4", 1.0)]}
    w = {"a": 0.5, "b": 0.5}
    assert st.strat_stat(same, w)["stat"] == pytest.approx(st.strat_stat(apart, w)["stat"])
    assert st.strat_stat(same, w)["se"] > st.strat_stat(apart, w)["se"]


def day(o=100.0, h=130.0, l=95.0, c=120.0, r=100.0, poc=125.0, vah=125.0, val=80.0, date_="2024-01-02"):
    return {"date": date_, "O": o, "H": h, "L": l, "C": c, "R": r, "poc": poc, "vah": vah, "val": val, "poc_d": 110.0, "vah_d": 112.0, "val_d": 105.0, "r80": None}


def test_t1_groups_by_hand():
    g = st.t1_groups([day(poc=125.0)])
    assert g == {"above": [("2024-01-02", 1.0)], "below": []}                           # POC 125 在區間內（碰到＝1）；鏡像 2×100−125＝75 沒碰到（低點 95）→ +1
    assert st.t1_groups([day(poc=115.0)]) == {"above": [], "below": []}                 # |O−POC|＝15 < 0.2R＝20：不合格
    assert st.t1_groups([day(poc=120.0)])["above"] == [("2024-01-02", 1.0 - 0.0)]       # 剛好 0.2R 合格（≥）；鏡像 80 沒碰到
    below = st.t1_groups([day(poc=75.0)])                                               # POC 在開盤價下方 25：沒碰到（低點 95）；鏡像 125 碰到 → −1
    assert below == {"above": [], "below": [("2024-01-02", -1.0)]}
    assert st.t1_groups([day(poc=125.0)], "_d") == {"above": [], "below": []}           # 偷看版本用 poc_d（110：距離 10 < 20 不合格）；欄位名要對
    assert st.t1_groups([day(poc=125.0)], "")["above"] != []


def test_t2_groups_by_hand():
    g = st.t2_groups([day(vah=125.0, val=80.0)])
    # VAH 125：距離 25 ≥ 5；實際碰到（≤130）、開盤在下方 → 壓力型；收盤 120 仍在下方 → 反彈 1；鏡像 75 沒碰到（低點 95）
    # VAL 80：實際沒碰到；鏡像 120：碰到、開盤在下方 → 壓力型；收盤 120 剛好在價位上（不算反彈）→ 0
    assert g[("res", "actual")] == [("2024-01-02", 1.0)] and g[("res", "mirror")] == [("2024-01-02", 0.0)]
    assert g[("sup", "actual")] == [] and g[("sup", "mirror")] == []
    near = st.t2_groups([day(vah=102.0, val=10.0)])                                    # VAH 距離 2 < 0.05R＝5：不合格（VAL 離得很遠、實際與鏡像都碰不到）
    assert near[("res", "actual")] == [] and near[("res", "mirror")] == []
    sup = st.t2_groups([day(o=100.0, h=130.0, l=70.0, c=90.0, vah=130.0, val=80.0)])    # 開盤在 VAL(80) 上方 → 支撐型；低點 70 碰到；收盤 90 仍在上方 → 反彈 1
    assert ("2024-01-02", 1.0) in sup[("sup", "actual")]
    broke = st.t2_groups([day(o=100.0, h=130.0, l=70.0, c=75.0, vah=130.0, val=80.0)])  # 收盤 75 跌破 80 → 沒反彈 0
    assert ("2024-01-02", 0.0) in broke[("sup", "actual")]


def test_halves_split_by_date():
    ds = [day(date_=f"2024-01-{i:02d}") for i in (5, 3, 4, 2)]
    a, b = st.halves(ds)
    assert [d["date"] for d in a] == ["2024-01-02", "2024-01-03"] and [d["date"] for d in b] == ["2024-01-04", "2024-01-05"]


# ── 合成世界：檢定本身要有偵測力、沒有假陽性 ──────────────────────

def sim_days(n, seed, magnet=0.0, barrier=0.0, drift=0.0, steps=60):
    rng = random.Random(seed)
    out, d0 = [], datetime(2020, 1, 1)
    for i in range(n):
        R = rng.uniform(400, 900)
        O = 10000 + rng.uniform(-50, 50)
        sig = 0.55 * R
        poc = O + rng.uniform(-0.9, 0.9) * R
        vah, val = poc + rng.uniform(0.1, 0.3) * R, poc - rng.uniform(0.1, 0.3) * R
        p = hi = lo = O
        for _ in range(steps):
            q = p + rng.gauss(drift * R / steps, sig / steps ** 0.5) + (magnet * (poc - p) / steps if magnet else 0.0)
            if barrier:
                for lvl in (vah, val):
                    if (p - lvl) * (q - lvl) < 0 and rng.random() < barrier:
                        hi, lo = max(hi, lvl), min(lo, lvl)                          # 先碰到那個價位
                        q = 2 * lvl - q                                              # 再被擋回原來那一邊（支撐壓力：碰到然後反轉）
            p = q
            hi, lo = max(hi, p), min(lo, p)
        mid = (hi + lo) / 2
        out.append({"date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"), "O": O, "H": hi, "L": lo, "C": p, "R": R, "poc": poc, "vah": vah, "val": val,
                    "poc_d": mid, "vah_d": mid + 10, "val_d": mid - 10, "r80": None})
    return out


def test_planted_magnet_is_detected_by_t1_only():
    ev = st.evaluate(sim_days(900, 1, magnet=6.0))
    assert ev["T1"]["pass"] and ev["T1"]["stat"] > 0.1 and ev["T1"]["t"] > 5, ev["T1"]
    assert not ev["T2"]["pass"]


def test_planted_barrier_is_detected_by_t2_only():
    """價值區邊緣「碰到然後被擋回」：反彈比例明顯高於鏡像價位，T2 要偵測得到、T1（POC 磁吸）不該被帶動。"""
    ev = st.evaluate(sim_days(900, 2, barrier=0.9))
    assert ev["T2"]["pass"] and ev["T2"]["stat"] > 0.1 and ev["T2"]["t"] > 5, ev["T2"]
    assert not ev["T1"]["pass"]


def calibration(n_sims, **world):
    ts1, ts2, hits = [], [], 0
    for s in range(n_sims):
        ev = st.evaluate(sim_days(450, 100 + s, **world))
        ts1.append(ev["T1"]["t"] or 0.0)
        ts2.append(ev["T2"]["t"] or 0.0)
        hits += ev["T1"]["pass"] or ev["T2"]["pass"]
    return hits / n_sims, sum(ts1) / n_sims, sum(ts2) / n_sims, ts1


def test_null_world_false_positive_rate_is_small():
    """無關的世界：兩個檢定任一過關的比例要接近名目（兩個單尾 2.5% ≈ 5%）。700 次模擬量過：4.7%；這裡 150 次，上限放在 11%。"""
    rate, m1, m2, ts1 = calibration(150)
    assert rate <= 0.11, rate
    assert abs(m1) < 0.3 and abs(m2) < 0.3
    sd = (sum((t - m1) ** 2 for t in ts1) / len(ts1)) ** 0.5
    assert 0.8 < sd < 1.25                                                             # 標準誤是準的：t 的標準差約 1


def test_asymmetric_world_does_not_fake_an_effect():
    """長期下跌（上下不對稱）、沒有任何價位效應：分層加上鏡像對照要把偏差抵銷。若直接比「碰到的比例」會大量誤判。"""
    rate, m1, m2, _ = calibration(150, drift=-0.45)
    assert rate <= 0.11, rate
    assert abs(m1) < 0.3 and abs(m2) < 0.3                                              # 平均 t 沒有被偏差推離 0
    naive = sim_days(900, 7, drift=-0.45)
    up = [float(st.touched(d["poc"], d)) for d in naive if d["poc"] > d["O"] and abs(d["O"] - d["poc"]) >= 0.2 * d["R"]]
    dn = [float(st.touched(d["poc"], d)) for d in naive if d["poc"] < d["O"] and abs(d["O"] - d["poc"]) >= 0.2 * d["R"]]
    assert sum(dn) / len(dn) - sum(up) / len(up) > 0.05                                # 對照：上下兩邊的碰到比例本來就差很多（所以要分層、要鏡像）


def test_lookahead_canary_is_huge():
    """偷看（用今天自己的價位）：今天的 POC 一定在今天的區間裡，T1 統計量要遠大於門檻；沒有就是管線有問題。"""
    ds = sim_days(500, 9)
    for d in ds:                                                                      # 讓今天的 POC 離開盤夠遠（合格），而且在區間內
        d["poc_d"] = (d["H"] + d["L"]) / 2 + (0.25 * d["R"] if d["H"] - d["O"] > 0.3 * d["R"] else 0)
    ev = st.evaluate(ds, "_d")
    assert ev["T1"]["stat"] is not None and ev["T1"]["stat"] > 0.1 and (ev["T1"]["t"] or 0) > 3 * st.Z
    assert abs(st.evaluate(ds)["T1"]["t"]) < 4                                          # 沒偷看的版本沒有這種效果


def continue_dates(ds):
    d0 = datetime(2020, 1, 1)
    for i, d in enumerate(ds):
        d["date"] = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
    return ds


def test_pass_needs_all_four_conditions():
    ok = st.evaluate(sim_days(900, 1, magnet=6.0))["T1"]
    assert ok["pass"]
    # ① 合格日不到 300：t 夠大也不過
    few = st.evaluate(sim_days(120, 1, magnet=6.0))["T1"]
    assert few["t"] >= st.Z and few["n_events"] < st.MIN_N and (few["first"] or 0) > 0 and (few["second"] or 0) > 0 and few["stat"] >= st.MIN_EFFECT and not few["pass"]
    # ② 後半是負的：整體 t 夠大也不過
    mixed = st.evaluate(continue_dates(sim_days(450, 400, magnet=8.0) + sim_days(450, 401, magnet=-2.0)))["T1"]
    assert mixed["t"] >= st.Z and mixed["first"] > 0 and mixed["second"] < 0 and mixed["n_events"] >= st.MIN_N and mixed["stat"] >= st.MIN_EFFECT and not mixed["pass"]
    # ③ 統計量不到 3 個百分點：t 夠大、兩半都是正的也不過（顯著但小到沒用）
    tiny = st.evaluate(sim_days(8000, 7, magnet=0.02))["T1"]
    assert tiny["t"] >= st.Z and tiny["first"] > 0 and tiny["second"] > 0 and tiny["n_events"] >= st.MIN_N and 0 < tiny["stat"] < st.MIN_EFFECT and not tiny["pass"]
    # ④ t 不到門檻：無關的世界
    null = st.evaluate(sim_days(600, 5))["T1"]
    assert null["t"] is not None and null["t"] < st.Z and not null["pass"]
    t2_few = st.evaluate(sim_days(120, 2, barrier=0.9))["T2"]
    assert t2_few["n_events"] < st.MIN_N and not t2_few["pass"]


def test_second_half_exactly_zero_does_not_pass():
    """前半很強、後半剛好是 0（沒有效果）：整體很顯著也不過關（兩半都要 > 0，不是 ≥ 0）。"""
    ds = []
    for i in range(400):
        above = i % 2 == 0
        first = i < 200
        if first:
            ds.append(day(h=130.0, l=95.0, poc=125.0) if above else day(h=110.0, l=70.0, poc=75.0))          # 實際碰到、鏡像沒碰到：+1
        else:
            ds.append(day(h=130.0, l=70.0, poc=125.0) if above else day(h=130.0, l=70.0, poc=75.0))          # 兩個都碰到：0
        ds[-1]["date"] = (datetime(2020, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
    r = st.evaluate(ds)["T1"]
    assert r["first"] == 1.0 and r["second"] == 0.0 and r["stat"] == 0.5 and r["t"] >= st.Z and r["n_events"] == 400 and not r["pass"]


def test_t2_sample_size_counts_only_the_actual_touched_levels():
    r = st.evaluate(sim_days(220, 2, barrier=0.9))["T2"]
    assert r["n_events"] < st.MIN_N <= sum(r["n"].values())                               # 實際碰到的不到 300；加上鏡像的才超過 300
    assert r["t"] >= st.Z and r["first"] > 0 and r["second"] > 0 and r["stat"] >= st.MIN_EFFECT and not r["pass"]


def test_canary_threshold():
    from core.volume_profile_study import canary_ok
    assert canary_ok(None) is False and canary_ok(0.0) is False and canary_ok(3 * st.Z - 0.01) is False and canary_ok(3 * st.Z + 0.01) is True


# ── 描述、整合、報告 ──────────────────────────────────────────────

def test_describe_structure_on_synthetic_days():
    ds = sim_days(400, 4, magnet=2.0)
    ds[0]["r80"] = {"above": True, "entered": True, "reached": True}
    ds[1]["r80"] = {"above": False, "entered": True, "reached": False}
    ds[2]["r80"] = {"above": True, "entered": False, "reached": False}
    d = st.describe(ds)
    assert set(d["t1_by_distance"]) == {"0.2~0.4R", "0.4~0.7R", ">0.7R"} and sum(v["n"] for v in d["t1_by_distance"].values()) <= 400
    assert d["rule80"] == {"open_outside_va": 3, "entered": 2, "reached": 1, "rate": 0.5, "from_above": {"entered": 1, "reached": 1}, "from_below": {"entered": 1, "reached": 0}}
    assert set(d["t2_rates"]) == {"res_actual", "res_mirror", "sup_actual", "sup_mirror"} and set(d["by_year"]) == {"2020", "2021"}
    assert d["t1_by_side"]["above"]["n"] + d["t1_by_side"]["below"]["n"] > 100
    assert st.describe([]) == {}


def test_run_end_to_end_on_synthetic_bars_and_report():
    days = trading_days(90)
    bars = synthetic_bars(days, seed=3)
    r = st.run(bars, days, monthly=set())
    assert r["n"] == 89 and r["first"] == days[1] and set(r["verdict"]) == {"T1", "T2"} and set(r["sensitivity"]) == {"typical_0.0002", "uniform_0.00015", "uniform_0.0003"}
    assert r["canary"]["T1_stat"] is not None and r["skipped"]["roll"] == 0 and r["canary"]["ok"] is st.canary_ok(r["canary"]["T1_t"])
    sens = r["sensitivity"]
    assert all(v["days"] == r["n"] for v in sens.values())
    assert len({round(v["T1"]["stat"], 9) for v in sens.values()} | {round(r["T1"]["stat"], 9)}) > 1            # 三種重建方式真的換了設定（結果不全一樣）
    text = st.format_report(r)
    assert "T1 磁吸" in text and "T2 邊緣反應" in text and "金絲雀" in text and "80% 規則" in text and "敏感度" in text and "nan" not in text.lower()
    small = st.run(synthetic_bars(trading_days(10)), trading_days(10), set())
    assert small["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "樣本不夠" in st.format_report(small)
    rolled = st.run(bars, days, monthly=set(days[::5]))                                # 每 5 天一個結算日：該天與隔天都略過
    assert rolled["n"] < r["n"] and rolled["skipped"]["roll"] > 0


# ── 純離線研究：策略、風控、下單模組不得引用 ──────────────────────

def test_study_is_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name == "volume_profile_study.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "volume_profile_study" not in " ".join(names), p
