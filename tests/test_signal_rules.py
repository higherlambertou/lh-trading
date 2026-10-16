"""順勢爆量訊號的定義（update.md 發現 43）：窗口合計、基準（只用當天以前）、偵測（門檻邊界、冷卻、不偷看）、進出場的結果計算。純函式，不碰資料庫。"""
from __future__ import annotations

import numpy as np
import pytest

import core.signal_rules as sr


# ── 窗口合計 ─────────────────────────────────────────────────

def test_rolling_sum_basic_and_nan_windows():
    out = sr.rolling_sum(np.array([1.0, 2, 3, 4, 5, 6]), 3)
    assert np.isnan(out[:2]).all() and list(out[2:]) == [6, 9, 12, 15]
    v = np.array([1.0, 2, np.nan, 4, 5, 6, 7])
    out = sr.rolling_sum(v, 3)
    assert np.isnan(out[2]) and np.isnan(out[3]) and np.isnan(out[4]) and list(out[5:]) == [15, 18]     # 窗口內有 NaN 就是 NaN
    assert np.isnan(sr.rolling_sum(np.array([1.0, 2]), 3)).all()


# ── 基準：前 20 個時段、同一個位置的中位數 ──────────────────────

def test_baseline_median_of_the_previous_sessions_only():
    w = np.vstack([np.full(4, float(v)) for v in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120)])
    b = sr.baseline(w, n_base=20, min_base=10)
    assert np.isnan(b[:10]).all()                                       # 前 10 個時段沒有足夠的歷史
    assert b[10, 0] == 55.0 and b[11, 0] == 60.0                         # 第 10 列＝前 10 列的中位數（不含自己）
    w2 = w.copy()
    w2[11] = 1e9                                                         # 當天自己再怎麼大都不影響當天的基準
    assert sr.baseline(w2)[11, 0] == 60.0


def test_baseline_uses_only_the_latest_n_sessions_and_needs_enough_values():
    w = np.vstack([np.full(3, 1000.0)] * 5 + [np.full(3, 10.0)] * 20 + [np.full(3, 0.0)])
    assert sr.baseline(w, n_base=20, min_base=10)[25, 0] == 10.0        # 只看最近 20 個
    w3 = np.vstack([np.full(3, 5.0)] * 12)
    w3[:5, 1] = np.nan                                                   # 第 1 欄前 5 個時段沒值
    b = sr.baseline(w3, n_base=20, min_base=10)
    assert b[11, 0] == 5.0 and np.isnan(b[11, 1])                        # 該欄只有 6 個值 < 10 → 沒基準


def test_ratio_of_guards_zero_and_missing_base():
    r = sr.ratio_of(np.array([10.0, 10.0, 10.0]), np.array([5.0, 0.0, np.nan]))
    assert r[0] == 2.0 and np.isnan(r[1]) and np.isnan(r[2])


# ── 偵測 ─────────────────────────────────────────────────────

def series(n=60, base=10000.0):
    return np.full(n, base)


def test_detect_threshold_and_ratio_edges():
    c = series()
    c[20:] = 10010.0                                       # 在第 20 分鐘跳 0.10%（整數價格，邊界不會被浮點誤差吃掉）
    r = np.full(60, 2.0)
    ev = sr.detect(c, r, 0.0010)
    assert [e.m for e in ev] == [20] and ev[0].direction == 1 and ev[0].move == pytest.approx(0.0010)         # 門檻是 ≥
    assert sr.detect(c, r, 0.00101) == []
    assert sr.detect(c, np.full(60, 1.999), 0.0010) == []                                                      # 量比也是 ≥ 2.0
    down = series(); down[20:] = 9988.0
    e = sr.detect(down, r, 0.0010)[0]
    assert e.direction == -1 and e.move < 0


def test_detect_needs_valid_data_and_enough_history():
    c = series(); c[20] = 10020.0                           # 只有第 20 分鐘一根尖刺（之後回到原價），這樣少一根資料就不會被下一分鐘補上
    r = np.full(60, 3.0)
    assert [e.m for e in sr.detect(c, r, 0.001)] == [20]
    bad = c.copy(); bad[15] = np.nan                        # W 分鐘前的收盤沒有 → 第 20 分鐘算不出（尖刺回落的第 25 分鐘是另一次反向訊號，冷卻之外就看得到）
    assert [e.m for e in sr.detect(bad, r, 0.001)] == [25]
    assert [e.m for e in sr.detect(bad, r, 0.001, cooldown=0)] == [25]
    bad2 = c.copy(); bad2[20] = np.nan                      # 這一分鐘沒有收盤：第 20 分鐘與第 25 分鐘（要和第 20 分鐘比）都算不出
    assert sr.detect(bad2, r, 0.001) == []
    r2 = r.copy(); r2[20] = np.nan                          # 沒有量比 → 沒訊號
    assert [e.m for e in sr.detect(c, r2, 0.001)] == [25]
    early = series(); early[3:] = 10100.0                   # 前 W 分鐘沒有「W 分鐘前」可比：最早只能是第 W 分鐘
    assert [e.m for e in sr.detect(early, np.full(60, 9.0), 0.001)] == [5]


def test_cooldown_blocks_the_next_thirty_minutes():
    c = np.cumsum(np.full(200, 12.0)) + 10000.0              # 一路穩定上漲：每分鐘 12 點，5 分鐘 60 點
    r = np.full(200, 5.0)
    ms = [e.m for e in sr.detect(c, r, 0.004)]
    assert ms[:3] == [5, 5 + 31, 5 + 62]                       # 發出之後 30 分鐘（m+1..m+30）不發，第 31 分鐘才可以再發
    assert [e.m for e in sr.detect(c, r, 0.004, cooldown=0)][:3] == [5, 6, 7]


def test_detect_never_looks_ahead():
    """金絲雀：改掉某一分鐘「之後」的所有資料，不能改變那一分鐘（含）以前的訊號。"""
    rng = np.random.default_rng(3)
    c = 10000.0 * np.cumprod(1 + rng.normal(0, 0.0006, 300))
    r = rng.uniform(1.0, 4.0, 300)
    base = sr.detect(c, r, 0.0008)
    assert len(base) > 3
    for cut in (50, 120, 200):
        c2, r2 = c.copy(), r.copy()
        c2[cut + 1:] = rng.uniform(5000, 20000, 299 - cut)
        r2[cut + 1:] = rng.uniform(0.0, 9.0, 299 - cut)
        assert [e for e in sr.detect(c2, r2, 0.0008) if e.m <= cut] == [e for e in base if e.m <= cut]


# ── 進出場的結果 ─────────────────────────────────────────────

def test_outcome_long_and_short_by_hand():
    highs = [101.0, 103.0, 102.0, 106.0, 104.0]
    lows = [99.0, 100.0, 99.5, 101.0, 100.0]
    closes = [100.5, 102.0, 101.0, 105.0, 103.0]
    o = sr.outcome(100.0, highs, lows, closes, 1, 3, cost=0.0001)
    assert o.horizon == 3 and o.gross == pytest.approx(0.01) and o.net == pytest.approx(0.0099)         # 第 3 分鐘收盤 101
    assert o.mfe == pytest.approx(0.03) and o.mae == pytest.approx(0.01)                                  # 前 3 分鐘最高 103、最低 99
    s = sr.outcome(100.0, highs, lows, closes, -1, 3, cost=0.0001)
    assert s.gross == pytest.approx(-0.01) and s.net == pytest.approx(-0.0101)
    assert s.mfe == pytest.approx(0.01) and s.mae == pytest.approx(0.03)                                  # 空單：低 99 有利、高 103 不利
    assert sr.outcome(100.0, highs, lows, closes, 1, 5).horizon == 5
    assert sr.outcome(100.0, highs, lows, closes, 1, 6) is None                                           # 序列不夠長
    assert sr.outcome(0.0, highs, lows, closes, 1, 3) is None and sr.outcome(100.0, [np.nan] * 5, lows, closes, 1, 3) is None


def test_cost_and_thresholds_are_in_percent_of_the_reference_price(monkeypatch):
    monkeypatch.delenv("SHADOW_COST_PTS", raising=False)
    assert sr.cost_pts() == 4.0 and sr.cost_pct() == pytest.approx(4.0 / 47000)
    assert sr.threshold_pct(30) == pytest.approx(30 / 47000) and [sr.threshold_pct(p) * 47000 for p in sr.POINTS] == pytest.approx([30, 40, 50])
    monkeypatch.setenv("SHADOW_COST_PTS", "6")
    assert sr.cost_pts() == 6.0
    for bad in ("abc", "-1"):
        monkeypatch.setenv("SHADOW_COST_PTS", bad)
        assert sr.cost_pts() == 4.0
    default = sr.outcome(100.0, [101.0], [99.0], [101.0], 1, 1)
    assert default.net == pytest.approx(default.gross - 4.0 / 47000)


# ── 停損停利（update.md 發現 44）─────────────────────────────

def flat(n=30, px=100.0):
    return np.full(n, px), np.full(n, px), np.full(n, px), np.full(n, px)


def br(direction=1, mod=None, tp=0.02, sl=0.01, cost=0.0, n=30):
    o, h, lo, c = flat(n)
    if mod:
        mod(o, h, lo, c)
    return sr.bracket(100.0, direction, o, h, lo, c, tp, sl, 30, cost)


def test_bracket_take_profit_stop_loss_and_time_by_hand():
    def tp_at_3(o, h, l, c): h[3] = 102.5
    b = br(mod=tp_at_3)
    assert (b.reason, b.minutes) == ("tp", 4) and b.gross == pytest.approx(0.02)                       # 停利價 102、用停利價出場（不算 102.5）
    def sl_at_2(o, h, l, c): l[2] = 98.9
    b = br(mod=sl_at_2)
    assert (b.reason, b.minutes) == ("sl", 3) and b.gross == pytest.approx(-0.01)
    def time_exit(o, h, l, c): c[29] = 100.5
    b = br(mod=time_exit)
    assert (b.reason, b.minutes) == ("time", 30) and b.gross == pytest.approx(0.005)                  # 第 30 分鐘的收盤
    def in_entry_bar(o, h, l, c): h[0] = 102.0
    assert br(mod=in_entry_bar).minutes == 1                                                           # 進場那一分鐘本身也算（從開盤進場後碰到）


def test_bracket_when_both_levels_are_touched_in_the_same_minute_the_stop_wins():
    def both(o, h, l, c): h[4], l[4] = 103.0, 98.0
    b = br(mod=both)
    assert b.reason == "sl" and b.gross == pytest.approx(-0.01)


def test_bracket_gaps():
    def gap_down(o, h, l, c): o[5], h[5], l[5], c[5] = 97.0, 97.5, 96.5, 97.0
    b = br(mod=gap_down)
    assert b.reason == "sl" and b.gross == pytest.approx(-0.03)                                        # 跳空越過停損價：用開盤價（更差）
    def gap_up(o, h, l, c): o[5], h[5], l[5], c[5] = 105.0, 106.0, 104.5, 105.5
    b = br(mod=gap_up)
    assert b.reason == "tp" and b.gross == pytest.approx(0.02)                                         # 跳空越過停利價：只算停利價，不算多賺的


def test_bracket_short_mirrors_the_long():
    def tp(o, h, l, c): l[6] = 97.5
    assert br(-1, tp).gross == pytest.approx(0.02) and br(-1, tp).reason == "tp"
    def sl(o, h, l, c): h[2] = 101.2
    b = br(-1, sl)
    assert b.reason == "sl" and b.gross == pytest.approx(-0.01)
    def gap(o, h, l, c): o[3], h[3], l[3], c[3] = 103.0, 103.5, 102.5, 103.0
    assert br(-1, gap).gross == pytest.approx(-0.03)
    def t(o, h, l, c): c[29] = 99.0
    assert br(-1, t).gross == pytest.approx(0.01)


def test_bracket_charges_the_cost_once_and_rejects_bad_input():
    def tp(o, h, l, c): h[1] = 103.0
    assert br(mod=tp, cost=0.0001).net == pytest.approx(0.02 - 0.0001)
    o, h, lo, c = flat(29)
    assert sr.bracket(100.0, 1, o, h, lo, c, 0.02, 0.01, 30, 0.0) is None                              # 不夠 30 根
    o, h, lo, c = flat()
    h[3] = np.nan
    assert sr.bracket(100.0, 1, o, h, lo, c, 0.02, 0.01, 30, 0.0) is None
    o, h, lo, c = flat()
    assert sr.bracket(0.0, 1, o, h, lo, c, 0.02, 0.01, 30, 0.0) is None


def test_registered_bracket_grid_and_hold():
    assert sr.TP_GRID == (30, 50, 80) and sr.SL_GRID == (20, 30, 50) and sr.MAX_HOLD == sr.COOLDOWN == 30


def _paths(n_paths, seed, drift_per_min=0.0, sigma_min=0.00018, sub=60, minutes=30):
    """每分鐘切成 sub 個小步的隨機路徑，再合成 1 分 K（高低點是小步裡的極值）。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift_per_min / sub, sigma_min / np.sqrt(sub), (n_paths, minutes * sub))
    P = np.concatenate([np.full((n_paths, 1), 47000.0), 47000.0 * np.exp(np.cumsum(steps, axis=1))], axis=1)
    k = np.arange(minutes)
    seg = np.stack([P[:, i * sub:(i + 1) * sub + 1] for i in k], axis=1)             # (路徑, 分鐘, sub+1)
    return seg[:, :, 0], seg.max(axis=2), seg.min(axis=2), seg[:, :, -1]


@pytest.mark.parametrize("tp,sl", [(0.0006, 0.0004), (0.0010, 0.0006), (0.0017, 0.0011), (0.0004, 0.0010)])
def test_stops_cannot_create_an_edge_from_a_fair_game(tp, sl):
    """理論（選擇停止時間）：價格是公平賭局時，不管停損停利怎麼設，平均毛報酬都是 0——停損停利只改變勝率與賺賠比。
    用 4000 條零漂移的隨機路徑驗證（容許 2 點的離散化與抽樣誤差）；扣成本就是 −成本。"""
    o, h, lo, c = _paths(4000, seed=11)
    gross = [sr.bracket(47000.0, 1, o[i], h[i], lo[i], c[i], tp, sl, 30, 0.0).gross for i in range(4000)]
    assert abs(np.mean(gross)) * sr.REF_PRICE < 2.0
    wins = np.mean([g > 0 for g in gross])
    assert 0.2 < wins < 0.8                                                       # 勝率隨停損停利比例變，但期望值不變


def test_stops_do_capture_an_edge_when_the_path_has_drift():
    """對照：路徑真的有漂移（順勢延續）時，同一套停損停利會得到明顯為正的報酬——這個檢定抓得到『有』的情況。"""
    o, h, lo, c = _paths(2000, seed=12, drift_per_min=0.00012)
    gross = [sr.bracket(47000.0, 1, o[i], h[i], lo[i], c[i], 0.0010, 0.0006, 30, 0.0).gross for i in range(2000)]
    assert np.mean(gross) * sr.REF_PRICE > 8.0
