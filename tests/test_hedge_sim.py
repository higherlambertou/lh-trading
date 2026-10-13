"""用微台對沖跨式 Delta 的歷史模擬（core/hedge_sim.py，update.md 發現 30 C）。

測什麼：
- Delta 的公式（對照 NormalDist 與 options_calc.greeks）、時間（分鐘）、IV 不偷看當天。
- 對沖流程：一個手算得出每一口、每一塊錢的小例子（跳空、換倉、到期日 13:30 之後的行情不算）；H1／H2／H3 各自在哪些根重新對沖（記錄實際被叫用的價格）；
  H3 的門檻邊界；再用一份**獨立重寫**（結構完全不同）的參考實作，對隨機價格路徑逐項比對損益、口數、次數。
- 統計與合成世界：公平定價（實際波動＝隱含）時對沖後的期望報酬為 0（成本前）、對沖大幅降低波動、實際波動更大時對沖後賺錢；成本與次數的單調性。
"""
from __future__ import annotations

import math
import random
import statistics
from datetime import date, datetime, time, timedelta
from statistics import NormalDist

import pytest

import core.hedge_sim as hs
import core.option_backtest as ob
import core.option_history as oh
from core.iv_monitor import black76
from core.market_store import MarketStore
from core.option_backtest import Cycle, Leg, Q
from core.options_calc import greeks

LABELS = [(8 * 60 + 46 + i) // 60 * 100 + (8 * 60 + 46 + i) % 60 for i in range(300)]           # 846 … 1345（1 分 K 的結束時間）
N = NormalDist()


@pytest.fixture(autouse=True)
def _fast_bootstrap(monkeypatch):
    monkeypatch.setattr(ob, "BOOT", 100)


def ref_net_delta(F, K, T, iv):
    """獨立算法：Call 與 Put 的 Delta 各自用 NormalDist 算再相加（Black-76、r＝0）。"""
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return N.cdf(d1) + (N.cdf(d1) - 1.0)


def minutes_left(day, hhmm, expiry):
    return (datetime.combine(expiry, time(13, 30)) - datetime.combine(day, time(hhmm // 100, hhmm % 100))).total_seconds() / 60.0


# ── Delta 與時間 ──────────────────────────────────────────────────

@pytest.mark.parametrize("F,K,T,iv", [(10000, 10000, 30 / 365, 0.2), (10000, 9900, 2 / 365, 0.3), (10100, 9900, 1 / 365, 0.2),
                                      (9800, 10000, 20 / 365, 0.15), (10000, 10500, 0.5, 0.25), (12000, 10000, 5 / 365, 0.2)])
def test_straddle_delta_matches_two_independent_calculations(F, K, T, iv):
    d = hs.straddle_delta(F, K, T, iv)
    assert d == pytest.approx(ref_net_delta(F, K, T, iv), abs=1e-12)
    assert d == pytest.approx(greeks(F, K, T, iv, 0.0, True)["delta"] + greeks(F, K, T, iv, 0.0, False)["delta"], abs=1e-12)


def test_straddle_delta_limits_and_hand_values():
    assert hs.straddle_delta(10000, 10000, 1.0, 0.2) == pytest.approx(math.erf(0.1 / math.sqrt(2)))      # d1 ＝ 0.5σ√T ＝ 0.1；2N(0.1) − 1 ＝ 0.0797
    assert hs.straddle_delta(10000, 10000, 1.0, 0.2) == pytest.approx(0.0797, abs=1e-4)
    assert hs.straddle_delta(10100, 10000, 0.0, 0.2) == 1.0 and hs.straddle_delta(9900, 10000, 0.0, 0.2) == -1.0 and hs.straddle_delta(10000, 10000, 0.0, 0.2) == 0.0
    assert hs.straddle_delta(10100, 10000, 0.01, 0.0) == 1.0                                         # IV 為 0 ＝ 到期的樣子
    assert hs.straddle_delta(20000, 10000, 1 / 365, 0.2) == pytest.approx(1.0) and hs.straddle_delta(5000, 10000, 1 / 365, 0.2) == pytest.approx(-1.0)
    ds = [hs.straddle_delta(F, 10000, 10 / 365, 0.2) for F in range(9000, 11001, 100)]
    assert ds == sorted(ds)                                                                          # 標的愈高淨 Delta 愈大


def test_years_left_counts_minutes_to_1330_on_expiry_day():
    e = date(2020, 6, 17)
    assert hs.years_left(e, 1330, e) == 0.0 and hs.years_left(e, 1345, e) == 0.0                      # 到期日 13:30 之後＝0（不會是負的）
    assert hs.years_left(e, 1300, e) * 365 * 1440 == pytest.approx(30.0)
    assert hs.years_left(e - timedelta(days=1), 1330, e) * 365 == pytest.approx(1.0)
    assert hs.years_left(e - timedelta(days=2), 846, e) * 365 * 1440 == pytest.approx(2 * 1440 + 4 * 60 + 44)


def test_iv_is_taken_from_strictly_before_the_day():
    ivs = {date(2020, 6, 1): 0.30, date(2020, 6, 2): 0.20, date(2020, 6, 4): 0.25}
    assert hs.iv_before(ivs, date(2020, 6, 2)) == 0.30 and hs.iv_before(ivs, date(2020, 6, 3)) == 0.20 and hs.iv_before(ivs, date(2020, 6, 4)) == 0.20
    assert hs.iv_before(ivs, date(2020, 6, 1)) is None and hs.iv_before({}, date(2020, 6, 1)) is None


def test_which_bars_each_scheme_rehedges():
    assert [i for i in range(300) if hs.is_hedge_bar("H1", i, i == 299)] == [299]
    assert [i for i in range(300) if hs.is_hedge_bar("H2", i, i == 299)] == [0, 60, 120, 180, 240, 299]
    assert all(hs.is_hedge_bar("H3", i, i == 299) for i in range(300))


# ── 手算的小例子 ──────────────────────────────────────────────────

K = 9900.0
D0, D1, E = date(2020, 6, 1), date(2020, 6, 2), date(2020, 6, 3)


def cyc(entry=D0, expiry=E, k=K):
    return Cycle("main", "202006", expiry, entry, 10000.0, {(k, "C"): Leg(Q(100.0, None, 99.0, 100.0)), (k, "P"): Leg(Q(100.0, None, 99.0, 100.0))},
                 ((k, "C"), (k, "P")), final_F=10000.0)


def day_bars(price, labels=LABELS):
    return [(h, price(i) if callable(price) else price) for i, h in enumerate(labels)]


def test_h1_by_hand_gap_rehedge_cost_and_the_bars_after_1330_are_ignored():
    ivs = {date(2020, 5, 29): 0.30, D0: 0.20, D1: 0.25}                                            # 進場日用 5/29 的 0.30（不是當天的 0.20）
    bars = {D0.isoformat(): day_bars(10000.0), D1.isoformat(): day_bars(10100.0),
            E.isoformat(): [(h, 10100.0 if h <= 1330 else 99999.0) for h in LABELS]}
    r = hs.hedge_cycle(cyc(), bars, ivs, "H1")
    T0 = minutes_left(D0, 1345, E) / (365 * 1440)
    T1 = minutes_left(D1, 1345, E) / (365 * 1440)
    lots0 = -round(5 * ref_net_delta(10000.0, K, T0, 0.30))                                         # 進場日收盤
    lots1 = -round(5 * ref_net_delta(10100.0, K, T1, 0.20))                                         # 隔天收盤：用 6/1 的 IV 0.20
    assert (lots0, lots1) == (-2, -5)                                                               # 手算：nd＝0.358、0.946
    assert r["pnl_twd"] == pytest.approx(lots0 * 10 * (10100 - 10000))                              # 隔天開盤跳空 100 點，持有 2 口空單；之後全是平的、到期日 13:30 後的 99999 不算
    assert r["trades"] == 2 and r["peak"] == 5 and r["traded"] == 2 + 3 + 5                          # 開倉 2、加到 5（+3）、到期平倉 5


def test_the_iv_of_a_day_is_only_used_from_the_next_day_on():
    bars = {D0.isoformat(): day_bars(10000.0), D1.isoformat(): day_bars(10100.0), E.isoformat(): day_bars(10100.0)}
    base = hs.hedge_cycle(cyc(), bars, {date(2020, 5, 29): 0.30, D0: 0.20, D1: 0.25}, "H1")
    assert hs.hedge_cycle(cyc(), bars, {date(2020, 5, 29): 0.30, D0: 0.20, D1: 0.99}, "H1") == base      # D1 的 IV 只會用在 D2（到期日不調整）：改了也不影響
    moved = hs.hedge_cycle(cyc(), bars, {date(2020, 5, 29): 0.30, D0: 0.50, D1: 0.25}, "H1")             # D0 的 IV 用在 D1 收盤的換倉
    assert moved["traded"] != base["traded"]
    T1 = minutes_left(D1, 1345, E) / (365 * 1440)
    assert -round(5 * ref_net_delta(10100.0, K, T1, 0.50)) == -3 and moved["peak"] == 3                  # 手算：IV 0.50 → 淨 Delta 0.565 → 3 口
    entry_only = hs.hedge_cycle(cyc(), bars, {date(2020, 5, 29): 0.20, D0: 0.20, D1: 0.25}, "H1")
    assert entry_only["pnl_twd"] == pytest.approx(-3 * 10 * 100) and base["pnl_twd"] == pytest.approx(-2 * 10 * 100)      # 進場日收盤用的是 5/29 的 IV：0.30 開 2 口、0.20 開 3 口


def test_expiry_day_closes_at_1330_so_a_move_before_it_counts_and_one_after_does_not():
    ivs = {date(2020, 5, 29): 0.30, D0: 0.20, D1: 0.25}
    flat = {D0.isoformat(): day_bars(10000.0), D1.isoformat(): day_bars(10100.0)}
    up = {**flat, E.isoformat(): [(h, 10200.0 if 1000 <= h <= 1330 else 10100.0) for h in LABELS]}
    late = {**flat, E.isoformat(): [(h, 10100.0 if h <= 1330 else 5000.0) for h in LABELS]}
    r_up, r_late = hs.hedge_cycle(cyc(), up, ivs, "H1"), hs.hedge_cycle(cyc(), late, ivs, "H1")
    base = -2 * 10 * 100
    assert r_up["pnl_twd"] == pytest.approx(base + (-5) * 10 * 100)                                  # 09:46~13:30 漲了 100 點，空 5 口
    assert r_late["pnl_twd"] == pytest.approx(base)                                                  # 13:30 之後掉到 5000 不影響


def test_a_cycle_without_bars_on_entry_or_expiry_or_iv_is_skipped_not_guessed():
    ivs = {date(2020, 5, 29): 0.30}
    bars = {D0.isoformat(): day_bars(10000.0), D1.isoformat(): day_bars(10000.0), E.isoformat(): day_bars(10000.0)}
    assert hs.hedge_cycle(cyc(), bars, ivs, "H1") is not None
    assert hs.hedge_cycle(cyc(), {k: v for k, v in bars.items() if k != D0.isoformat()}, ivs, "H1") is None       # 進場日沒有 1 分 K
    assert hs.hedge_cycle(cyc(), {k: v for k, v in bars.items() if k != E.isoformat()}, ivs, "H1") is None        # 到期日沒有
    assert hs.hedge_cycle(cyc(), bars, {}, "H1") is None                                                           # 沒有任何更早的 IV


def fake_delta(monkeypatch, values):
    """把淨 Delta 換成事先給定的序列（或函式 (第幾次, 標的價) → 值），回傳每次被叫用時的標的價。"""
    calls = []

    def f(F, K_, T, iv):
        calls.append(F)
        i = len(calls) - 1
        return values(i, F) if callable(values) else values[i]

    monkeypatch.setattr(hs, "straddle_delta", f)
    return calls


def test_each_scheme_evaluates_the_delta_only_at_its_own_bars(monkeypatch):
    ivs = {date(2020, 5, 29): 0.30}
    n = 130
    labels = LABELS[:n]
    bars = {d.isoformat(): [(h, 10000.0 + i + 1000 * j) for i, h in enumerate(labels)] for j, d in enumerate((D0, D1, E))}
    seen = {}
    for scheme in ("H1", "H2", "H3"):
        calls = fake_delta(monkeypatch, lambda i, F: 0.0)
        hs.hedge_cycle(cyc(), bars, ivs, scheme)
        seen[scheme] = calls
    entry_last = 10000.0 + n - 1
    d1 = [11000.0 + i for i in range(n)]
    assert seen["H1"] == [entry_last, d1[-1]]                                                         # 進場日收盤、隔天收盤
    assert seen["H2"] == [entry_last] + [d1[i] for i in (0, 60, 120, 129)]
    assert seen["H3"] == [entry_last] + d1                                                            # 進場日只有收盤；隔天每一根；到期日不評估


def test_h3_threshold_adjusts_at_exactly_0_15_and_not_below(monkeypatch):
    ivs = {date(2020, 5, 29): 0.30}
    bars = {D0.isoformat(): [(846 + i, 10000.0) for i in range(3)], D1.isoformat(): [(846, 10000.0), (847, 10010.0), (848, 10020.0)],
            E.isoformat(): [(846, 10050.0), (847, 10050.0), (848, 10050.0)]}
    nds = [0.0,                    # 進場日收盤：沒對沖
           0.14,                   # 差 0.14 < 0.15 → 不動
           0.15,                   # 差剛好 0.15 → 調整：−round(0.75) ＝ −1 口
           0.36]                   # 已對沖 0.2；差 0.16 → 調整：−round(1.8) ＝ −2 口
    calls = fake_delta(monkeypatch, nds)
    r = hs.hedge_cycle(cyc(), bars, ivs, "H3")
    assert len(calls) == 4
    assert r["trades"] == 2 and r["peak"] == 2 and r["traded"] == 1 + 1 + 2                          # 開 1 口、加 1 口、到期平 2 口
    assert r["pnl_twd"] == pytest.approx(-1 * 10 * (10020 - 10010) + -2 * 10 * (10050 - 10020))      # 第 2 根起空 1 口到第 3 根；之後空 2 口到到期日第一根
    for nd, lots in ((0.1499, 0), (0.15, -1)):                                                         # 邊界再單獨確認
        fake_delta(monkeypatch, [0.0, nd, nd, nd])
        assert hs.hedge_cycle(cyc(), bars, ivs, "H3")["peak"] == abs(lots)


# ── 獨立重寫的參考實作：隨機路徑逐項比對 ───────────────────────────

def brute(c, bars, ivs, scheme):
    """把整段時間攤平成事件清單，依序處理（先結算上一段的損益，再決定要不要換倉）；寫法和 hedge_cycle 的逐日迴圈完全不同。"""
    k, expiry = c.straddle[0][0], c.expiry_date
    events = []
    for d in sorted(bars):
        if not (c.entry_day.isoformat() <= d <= expiry.isoformat()):
            continue
        day = date.fromisoformat(d)
        bs = [(h, p) for h, p in bars[d] if h <= 1330 or day != expiry]
        iv = max(((x, v) for x, v in ivs.items() if x < day), default=(None, None))[1]
        for i, (h, p) in enumerate(bs):
            events.append({"day": day, "h": h, "p": p, "i": i, "last": i == len(bs) - 1, "iv": iv})
    lots, pnl, traded, trades, peak = 0, 0.0, 0, 0, 0
    hedged_delta_units = lambda: -lots / 5.0           # noqa: E731
    for n, ev in enumerate(events):
        if n:
            pnl += lots * 10.0 * (ev["p"] - events[n - 1]["p"])
        if ev["day"] == expiry or (ev["day"] == c.entry_day and not ev["last"]):
            continue
        if scheme == "H1":
            act = ev["last"]
        elif scheme == "H2":
            act = ev["last"] or ev["i"] % 60 == 0
        else:
            act = True
        if not act:
            continue
        T = minutes_left(ev["day"], ev["h"], expiry) / (365 * 1440)
        nd = ref_net_delta(ev["p"], k, T, ev["iv"]) if T > 0 else float((ev["p"] > k) - (ev["p"] < k))
        if scheme == "H3" and abs(nd - hedged_delta_units()) < 0.15:
            continue
        target = -round(5 * nd)
        if target != lots:
            traded += abs(target - lots)
            trades += 1
            lots = target
            peak = max(peak, abs(lots))
    traded += abs(lots)
    return {"pnl_twd": pnl, "traded": traded, "trades": trades, "peak": peak}


def random_bars(rng, days, vol_per_min, start=10000.0, labels=LABELS):
    bars, p = {}, start
    for d in days:
        row = []
        for h in labels:
            p *= math.exp(vol_per_min * rng.gauss(0, 1))
            row.append((h, round(p, 1)))
        bars[d.isoformat()] = row
    return bars


def test_hedge_cycle_matches_an_independent_reference_on_random_paths():
    rng = random.Random(1013)
    days = [D0 + timedelta(days=i) for i in range(6)]
    for trial in range(6):
        k = 10000.0 + rng.choice([-300, -100, 0, 100, 300])
        c = cyc(expiry=days[-1], k=k)
        bars = random_bars(rng, days, vol_per_min=rng.choice([0.0002, 0.0005, 0.001]), start=10000.0)
        ivs = {D0 - timedelta(days=1): rng.uniform(0.12, 0.35), **{d: rng.uniform(0.12, 0.35) for d in days[:-1]}}
        for scheme in hs.SCHEMES:
            got, want = hs.hedge_cycle(c, bars, ivs, scheme), brute(c, bars, ivs, scheme)
            assert got["pnl_twd"] == pytest.approx(want["pnl_twd"], abs=1e-6), (trial, scheme)
            assert (got["traded"], got["trades"], got["peak"]) == (want["traded"], want["trades"], want["peak"]), (trial, scheme)
        h3 = hs.hedge_cycle(c, bars, ivs, "H3")
        h1 = hs.hedge_cycle(c, bars, ivs, "H1")
        assert h3["trades"] >= 1 and h1["trades"] >= 1


# ── 損益與統計的串接 ───────────────────────────────────────────────

REC = {"expiry": "202006", "entry": "2020-06-01", "outlay_twd": 10000.0, "opt_twd": 500.0,
       "H1": {"pnl_twd": -200.0, "traded": 10, "trades": 3, "peak": 5}, "H2": {"pnl_twd": 300.0, "traded": 20, "trades": 5, "peak": 5},
       "H3": {"pnl_twd": 100.0, "traded": 40, "trades": 9, "peak": 6}}


def test_roi_is_total_pnl_over_the_outlay_with_cost_per_lot_traded():
    assert hs.roi(REC, "X0") == pytest.approx(0.05)
    assert hs.roi(REC, "H1") == pytest.approx((500 - 200 - 10 * 3 * 10) / 10000)                       # 成本 3 點 × 10 元 × 10 口＝300 元
    assert hs.roi(REC, "H1", 1.0) == pytest.approx((500 - 200 - 100) / 10000) and hs.roi(REC, "H1", 5.0) == pytest.approx((500 - 200 - 500) / 10000)
    assert hs.roi(REC, "H3", 0.0) == pytest.approx((500 + 100) / 10000)


def test_tests_c1_to_c4_by_hand():
    recs = []
    for i, (opt, p1, p2, p3) in enumerate([(500, -200, 300, 100), (-1000, 400, 100, 250), (2000, -900, -500, -100), (-800, 50, 0, 120), (300, -50, 20, -30)]):
        recs.append({**REC, "expiry": f"2020{i:02d}", "opt_twd": float(opt), "H1": {**REC["H1"], "pnl_twd": float(p1)},
                     "H2": {**REC["H2"], "pnl_twd": float(p2)}, "H3": {**REC["H3"], "pnl_twd": float(p3)}})
    res = hs.run_tests({"records": recs, "skipped": ["x"]})
    r0 = [r["opt_twd"] / 10000 for r in recs]
    r1 = [(r["opt_twd"] + r["H1"]["pnl_twd"] - 300) / 10000 for r in recs]
    c1 = res["tests"]["C1"]
    assert c1["mean"] == pytest.approx(statistics.mean(r1)) and c1["t"] == pytest.approx(statistics.mean(r1) / (statistics.stdev(r1) / math.sqrt(5)))
    diff = [a - b for a, b in zip(r1, r0)]
    assert res["tests"]["C4"]["mean"] == pytest.approx(statistics.mean(diff)) and res["tests"]["C4"]["t"] == pytest.approx(statistics.mean(diff) / (statistics.stdev(diff) / math.sqrt(5)))
    assert c1["mde"] == pytest.approx((hs.Z_CLAIM + ob.Z_POWER) * statistics.stdev(r1) / math.sqrt(5))
    d = res["describe"]
    assert d["cycles"] == 5 and d["skipped"] == ["x"] and d["H1"]["sd_ratio"] == pytest.approx(statistics.stdev(r1) / statistics.stdev(r0))
    assert d["H1"]["cost_twd"] == pytest.approx(300.0) and d["H2"]["cost_twd"] == pytest.approx(600.0) and d["H3"]["cost_twd"] == pytest.approx(1200.0)
    assert d["H1"]["hedge_pnl_twd"] == pytest.approx(statistics.mean([-200, 400, -900, 50, -50]))
    assert d["H3"]["sens"]["1.0"]["mean"] == pytest.approx(statistics.mean((r["opt_twd"] + r["H3"]["pnl_twd"] - 400) / 10000 for r in recs))


def test_threshold_is_the_four_test_bonferroni_one():
    assert hs.Z_CLAIM == pytest.approx(2.50, abs=0.005) and hs.N_TESTS == 4
    assert hs.verdict(hs.Z_CLAIM) == "過關" and hs.verdict(-hs.Z_CLAIM) == "過關" and hs.verdict(2.4) == "線索" and hs.verdict(1.9) == "沒有" and hs.verdict(float("nan")) == "無法檢定"
    assert (hs.TMF_MULT, hs.OPT_MULT, hs.LOTS_PER_DELTA, hs.HEDGE_COST_PTS, hs.THRESHOLD, hs.HOUR_BARS, hs.LAST_BAR) == (10.0, 50.0, 5.0, 3.0, 0.15, 60, 1330)


# ── 合成世界：對沖該有的性質 ────────────────────────────────────────

IV = 0.20


def world(seed, n_cycles, vol_mult, n_days=8):
    """每個週期 n_days 個交易日（每天 300 根）；選擇權用 Black-76（IV 固定），標的的真實波動是 vol_mult × IV。回傳 (cycles, bars, ivs)。"""
    rng = random.Random(seed)
    cycles, bars, ivs = [], {}, {}
    per_min = vol_mult * IV / math.sqrt(365 * 1440)                                               # 每分鐘（日曆時間）的波動；跳過夜盤與週末的時間，所以日盤內每根用 1 分鐘
    gap = vol_mult * IV * math.sqrt(1 / 365 * (1 - 300 / 1440))                                    # 一天之中不在日盤的時間，用一個隔夜跳空補
    for i in range(n_cycles):
        entry = date(2020, 4, 1) + timedelta(days=40 * i)
        days = [entry + timedelta(days=j) for j in range(n_days + 1)]
        expiry = days[-1]
        day_rows = {}
        p = 10000.0
        for j, d in enumerate(days):
            if j:
                p *= math.exp(gap * rng.gauss(0, 1))
            row = []
            for h in LABELS:
                p *= math.exp(per_min * rng.gauss(0, 1))
                row.append((h, round(p, 2)))
            day_rows[d.isoformat()] = row
        for d, row in day_rows.items():
            bars[d] = row
        entry_close = day_rows[entry.isoformat()][-1][1]                                           # 進場日收盤買跨式：價格路徑先走完進場日，再用收盤價當進場時的標的價
        k = round(entry_close / 100) * 100.0
        T0 = ((datetime.combine(expiry, time(13, 30)) - datetime.combine(entry, time(13, 45))).total_seconds()) / (365 * 86400)
        legs = {(k, "C"): Leg(Q(*(black76(entry_close, k, T0, IV, 0.0, True),) * 2, None, None)),
                (k, "P"): Leg(Q(*(black76(entry_close, k, T0, IV, 0.0, False),) * 2, None, None))}
        final = [r for r in day_rows[expiry.isoformat()] if r[0] <= 1330][-1][1]
        cycles.append(Cycle("main", f"G{i}", expiry, entry, entry_close, legs, ((k, "C"), (k, "P")), final_F=final))
        ivs[f"G{i}"] = {entry - timedelta(days=1): IV, **{d: IV for d in days}}
    return cycles, bars, ivs


def test_hedging_cuts_the_spread_of_outcomes_and_a_fair_world_has_zero_mean_before_costs():
    cycles, bars, ivs = world(5, 36, 1.0)
    ev = hs.evaluate(cycles, bars, ivs)
    assert len(ev["records"]) == 36 and ev["skipped"] == []
    res = hs.run_tests(ev)["describe"]
    assert all(res[s]["sd_ratio"] < 0.8 for s in hs.SCHEMES), {s: res[s]["sd_ratio"] for s in hs.SCHEMES}      # 對沖掉大部分波動（這個種子約 0.37~0.41；另兩個種子 0.25~0.69）
    free = [(r["opt_twd"] + r["H3"]["pnl_twd"]) / r["outlay_twd"] for r in ev["records"]]            # 選擇權損益（含它自己的手續費與稅）加上對沖損益，不扣對沖成本
    se = statistics.stdev(free) / math.sqrt(len(free))
    assert abs(statistics.mean(free) + 0.005) < 3.5 * se, (statistics.mean(free), se)               # 公平賭局：只剩選擇權進出的 ~0.5% 成本
    assert res["H1"]["traded"] < res["H3"]["traded"] < res["H2"]["traded"]                          # 每天 12 口 < 門檻 17 口 < 每小時 20 口左右：交易愈多、成本愈高
    assert res["H2"]["cost_twd"] > res["H3"]["cost_twd"] > res["H1"]["cost_twd"] > 0


def test_when_the_market_moves_more_than_the_options_imply_the_hedged_straddle_makes_money():
    quiet = hs.run_tests(hs.evaluate(*_ev(7, 1.0)))["describe"]
    wild = hs.run_tests(hs.evaluate(*_ev(7, 1.6)))["describe"]
    assert wild["H2"]["roi"]["mean"] - quiet["H2"]["roi"]["mean"] > 0.3                              # 實際波動多 60%：gamma 賺到的比 theta 多很多
    assert wild["H2"]["roi"]["mean"] > 0.25
    t = hs.run_tests(hs.evaluate(*_ev(7, 1.6)))["tests"]
    assert t["C2"]["verdict"] == "過關" and t["C3"]["verdict"] == "過關"


def _ev(seed, mult):
    cycles, bars, ivs = world(seed, 30, mult)
    return cycles, bars, ivs


def test_evaluate_records_are_in_dollars_not_points():
    cycles, bars, ivs = world(2, 2, 1.0, n_days=4)
    ev = hs.evaluate(cycles, bars, ivs)
    for c, rec in zip(cycles, ev["records"]):
        opt = ob.simulate(c)
        assert rec["outlay_twd"] == pytest.approx(opt["outlay"] * 50) and rec["opt_twd"] == pytest.approx(opt["X0"]["pnl"] * 50)
        assert hs.roi(rec, "X0") == pytest.approx(opt["X0"]["roi"])                                  # 不對沖的 ROI 和 option_backtest 算的一樣
        assert rec["expiry"] == c.expiry and rec["entry"] == c.entry_day.isoformat()
        for s in hs.SCHEMES:
            h = hs.hedge_cycle(c, bars, ivs[c.expiry], s)
            assert rec[s] == h
            assert hs.roi(rec, s) == pytest.approx((opt["X0"]["pnl"] * 50 + h["pnl_twd"] - h["traded"] * 30) / (opt["outlay"] * 50))


def test_evaluate_skips_cycles_before_the_bar_history_and_cycles_with_missing_data():
    cycles, bars, ivs = world(3, 4, 1.0)
    early = cycles[0]
    early.entry_day = date(2020, 3, 20)                                                              # 1 分 K 還沒開始的進場日
    ev = hs.evaluate(cycles, bars, ivs)
    assert [r["expiry"] for r in ev["records"]] == ["G1", "G2", "G3"] and ev["skipped"] == []
    del bars[cycles[2].expiry_date.isoformat()]                                                      # 一個週期的到期日沒有 1 分 K
    ev = hs.evaluate(cycles, bars, ivs)
    assert [r["expiry"] for r in ev["records"]] == ["G1", "G3"] and ev["skipped"] == ["G2"]
    cycles[1].final_F = None                                                                         # 選擇權本身算不出來
    assert hs.evaluate(cycles, bars, ivs)["skipped"] == ["G1", "G2"]


# ── 資料載入與每個到期月的 IV ───────────────────────────────────────

def test_load_bars_reads_txf_minute_closes_in_order(tmp_path):
    st = MarketStore(tmp_path / "m.db")
    st.upsert_bars_1m("TXF", [{"date": "2020-06-02", "hhmm": 847, "open": 1, "high": 1, "low": 1, "close": 10002.0, "volume": 1},
                              {"date": "2020-06-02", "hhmm": 846, "open": 1, "high": 1, "low": 1, "close": 10001.0, "volume": 1},
                              {"date": "2020-06-01", "hhmm": 846, "open": 1, "high": 1, "low": 1, "close": 9990.0, "volume": 1},
                              {"date": "2020-06-01", "hhmm": 847, "open": 1, "high": 1, "low": 1, "close": 0, "volume": 0}])
    st.upsert_bars_1m("TMF", [{"date": "2020-06-01", "hhmm": 846, "open": 1, "high": 1, "low": 1, "close": 5.0, "volume": 1}])
    assert hs.load_bars(tmp_path / "m.db") == {"2020-06-01": [(846, 9990.0)], "2020-06-02": [(846, 10001.0), (847, 10002.0)]}          # 收盤 0 的不要；只要 TXF
    assert hs.load_bars(tmp_path / "m.db", since="2020-06-02") == {"2020-06-02": [(846, 10001.0), (847, 10002.0)]}


def test_expiry_ivs_gives_each_expiry_its_own_series(tmp_path):
    rows = []
    for d, F in ((date(2020, 6, 1), 10000.0), (date(2020, 6, 2), 10100.0)):
        for code, edate, sigma in (("202006", date(2020, 6, 17), 0.20), ("202007", date(2020, 7, 15), 0.28)):
            T = ob.years_to(d, edate)
            for i in range(-8, 9):
                k = round(F / 100) * 100.0 + 100 * i
                for right in ("C", "P"):
                    p = round(black76(F, k, T, sigma, 0.0, right == "C"), 1)
                    rows.append({"date": d.isoformat(), "kind": "M", "expiry": code, "expiry_date": edate.isoformat(), "session": "G", "right": right,
                                 "strike": k, "F": F, "settle": p, "close": p, "bid": p, "ask": p, "volume": 1.0, "oi": 1.0})
    (tmp_path / "opt").mkdir()
    oh.write_rows(tmp_path / "opt" / "chain_2020_06.csv.gz", rows)
    got = hs.expiry_ivs(tmp_path)
    assert set(got) == {"202006", "202007"}
    for code, sigma in (("202006", 0.20), ("202007", 0.28)):
        assert set(got[code]) == {date(2020, 6, 1), date(2020, 6, 2)}
        assert all(v == pytest.approx(sigma, abs=0.0006) for v in got[code].values())
    fixed = hs.expiry_ivs(tmp_path, {"202006": date(2020, 6, 24)})                                  # 到期日更正會讓 T 變長 → 同樣的價格反推出較低的 IV
    assert fixed["202006"][date(2020, 6, 1)] < got["202006"][date(2020, 6, 1)] and fixed["202007"] == got["202007"]


def test_report_lists_every_scheme_test_and_cost_sensitivity():
    cycles, bars, ivs = world(11, 12, 1.0, n_days=5)
    res = hs.run_tests(hs.evaluate(cycles, bars, ivs))
    out = hs.format_report(res)
    for needle in ("X0 不對沖", "H1 每天收盤", "H2 每小時＋收盤", "H3 門檻 0.15", "C1", "C2", "C3", "C4", "成本敏感度", "週期 12 個"):
        assert needle in out
    assert hs.format_report(hs.run_tests({"records": [], "skipped": []})).startswith("沒有可用的週期")
