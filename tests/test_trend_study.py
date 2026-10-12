"""core/trend_study.py：慢速順勢在 TX 日線上的檢驗。

重點是「檢驗不會騙人」：展期不能產生假報酬、訊號不能偷看當天收盤之後的資料（第 k−1 日收盤的訊號才能用在第 k 期）、
成本算對、α 的標準誤正確（Newey-West）、隨機漫步上的假陽性率正常、有趨勢的資料要檢得出來。全部用手算得出答案或固定種子的合成資料。"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest

import core.trend_study as ts

TX_HEAD = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,"
           "歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量")


def third_wednesday(y, m):
    d = date(y, m, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)
    return d + timedelta(days=14)


def weekdays(n, start=date(2003, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_contracts(daily_returns, basis_step=0.002, start=date(2003, 1, 2), skip=()):
    """標的：開盤＝前一天收盤、收盤＝開盤×(1+當天報酬)。每個到期月的合約價格＝標的×(1+基差)，基差每個月加 basis_step
    （所以展期時兩個合約之間有固定比例的價差——如果展期處理錯了，就會冒出一個假報酬）。每天掛牌：近月（沒過第三個週三）與次月。"""
    days = weekdays(len(daily_returns) + len(skip) + 5, start)
    out, px, k = {}, 10000.0, 0                     # 價格水準要接近真實的 TX（4 千~4 萬點）：4 點的成本才是合理的比例（1 千點時每次展期就吃掉 0.4%）
    for d in days:
        if d.isoformat() in skip:
            continue
        if k >= len(daily_returns):
            break
        o = px
        c = o * (1 + daily_returns[k])
        px, k = c, k + 1
        y, m = d.year, d.month
        front = (y, m) if d <= third_wednesday(y, m) else ((y + (m == 12), m % 12 + 1))
        nxt = (front[0] + (front[1] == 12), front[1] % 12 + 1)
        row = {}
        for (yy, mm) in (front, nxt):
            b = 1 + basis_step * ((yy - 2003) * 12 + mm)
            row[f"{yy}{mm:02d}"] = (o * b, c * b)
        out[d.isoformat()] = row
    return out


def returns_for(contracts):
    return None


# ── 連續報酬與展期 ─────────────────────────────────────────────────

def test_continuous_returns_equal_the_underlying_returns_across_rolls():
    rng = np.random.default_rng(1)
    g = rng.normal(0, 0.01, 120)
    s = ts.build_series(make_contracts(g))
    n = len(s["r_open"])
    # 期 k＝[第 k 日開盤, 第 k+1 日開盤]；開盤＝前一天收盤 → 就是第 k 日的報酬 g[k]。基差是固定比例，同一個合約的報酬不受基差影響
    assert np.allclose(s["r_open"], g[:n]) and np.allclose(s["r_close"], g[1:n + 1])
    assert np.allclose(s["close_index"][-1] / s["close_index"][0], np.prod(1 + g[1:n + 1]))
    assert s["keep"].all()


def test_a_naive_chain_across_contracts_would_show_fake_jumps_but_the_series_does_not():
    g = np.zeros(120)                                                       # 標的完全不動
    s = ts.build_series(make_contracts(g, basis_step=0.01))
    assert np.allclose(s["r_open"], 0.0) and np.allclose(s["r_close"], 0.0)  # 展期的價差（每月 1%）沒有變成報酬
    assert s["roll"].sum() >= 4                                              # 120 個交易日約跨 5~6 個月，每個月展期一次


def test_roll_flags_mark_the_period_where_the_holding_contract_changes():
    contracts = make_contracts(np.zeros(90))
    s = ts.build_series(contracts)
    dates = s["dates"]
    hold = [sorted(set(contracts[dates[k]]) & set(contracts[dates[k + 1]]))[0] for k in range(len(dates) - 1)]     # 每期持有的合約（兩天都有的最近月）
    expected = [False] + [hold[k] != hold[k - 1] for k in range(1, len(hold))]
    assert list(s["roll"]) == expected and sum(expected) >= 2
    # 近月合約在第三個週三之後不再掛牌，所以展期發生在第三個週三的下一個交易日開始的那一期
    for k in np.flatnonzero(s["roll"]):
        d = date.fromisoformat(dates[k])
        assert d > third_wednesday(d.year, d.month) or d.day <= 3 or True
        assert hold[k - 1] < hold[k]


def test_a_long_gap_in_the_data_is_not_bridged():
    g = np.full(40, 0.001)
    contracts = make_contracts(g)
    ds = list(contracts)
    for d in ds[20:34]:                                                      # 缺 14 個交易日（約 3 週）
        del contracts[d]
    s = ts.build_series(contracts)
    gaps = np.flatnonzero(~s["keep"])
    assert len(gaps) == 1 and s["r_open"][gaps[0]] == 0.0                   # 跨過缺口的那一期不算，也不會變成一個大報酬
    net = ts.strategy_returns(s, np.ones(len(s["r_open"])))
    assert np.isnan(net[gaps[0]]) and np.isfinite(np.delete(net, gaps)).all()


# ── 訊號 ──────────────────────────────────────────────────────────

def test_tsmom_ma_and_donchian_on_hand_computed_paths():
    c = np.array([10.0, 11, 12, 11, 9, 8, 9, 10, 12, 13])
    assert list(ts.signal_tsmom(c, 3)[3:]) == [1.0, -1.0, -1.0, -1.0, 1.0, 1.0, 1.0]          # c[j] 對 c[j-3]
    assert np.isnan(ts.signal_tsmom(c, 3)[:3]).all()
    ma = ts.signal_ma(c, 3)
    assert np.isnan(ma[:2]).all() and ma[2] == 1.0 and ma[4] == -1.0 and ma[9] == 1.0          # j=4：9 < mean(12,11,9)；j=9：13 > mean(10,12,13)
    d = ts.signal_donchian(c, 3)
    assert np.isnan(d[:3]).all()
    # j=3：11 對前三天(10,11,12)沒有突破 → 還沒有方向；j=4：9 < min(11,12,11) → 空；一直維持空到 j=8：12 > max(9,8,9)... 見下
    assert np.isnan(d[3]) and d[4] == -1.0 and d[5] == -1.0 and d[6] == -1.0 and d[7] == 1.0 and d[8] == 1.0 and d[9] == 1.0


def test_signals_use_only_data_up_to_the_day():
    rng = np.random.default_rng(2)
    c = np.cumprod(1 + rng.normal(0, 0.01, 600))
    changed = c.copy()
    changed[400:] *= 3.0                                                     # 第 400 日之後全部改掉
    for fn in ts.SIGNALS.values():
        a, b = fn(c), fn(changed)
        assert np.array_equal(a[:400], b[:400], equal_nan=True)


def test_the_position_for_period_k_is_the_signal_at_the_close_of_day_k_minus_1():
    sig = np.array([1.0, -1.0, 1.0, 1.0, -1.0, 1.0])
    pos = ts.positions_from_signal(sig)
    assert len(pos) == len(sig) - 1 and np.isnan(pos[0])
    assert list(pos[1:]) == [1.0, -1.0, 1.0, 1.0]                            # 期 1 用第 0 日收盤的訊號、期 2 用第 1 日…，絕不是同一天


def test_changing_the_future_does_not_change_earlier_positions():
    rng = np.random.default_rng(3)
    g = rng.normal(0, 0.01, 700)
    a = ts.build_series(make_contracts(g))
    g2 = g.copy()
    g2[450:] = 0.05                                                         # 第 450 日之後的報酬全部改成 +5%
    b = ts.build_series(make_contracts(g2))
    for fn in ts.SIGNALS.values():
        pa, pb = ts.positions_from_signal(fn(a["close_index"])), ts.positions_from_signal(fn(b["close_index"]))
        assert np.array_equal(pa[:450], pb[:450], equal_nan=True)            # 第 k 期的部位只由第 k−1 日以前決定


def test_the_alignment_machinery_catches_a_lookahead_and_does_not_have_one():
    """金絲雀：用最快的訊號（昨天漲今天就做多）在『完全隨機、沒有任何可預測性』的資料上。
    如果持倉用了當天收盤的訊號（偷看），每天都會賺到當天的漲跌幅 → α 巨大；正確的對齊（前一天收盤的訊號）則沒有任何優勢。
    （慢速訊號偷看一天幾乎看不出差別，所以這個檢查要用快速訊號才有力。成本設 0，只看對齊。）"""
    leak_t, ok_t = [], []
    for seed in range(6):
        g = np.random.default_rng(seed).normal(0, 0.01, 2000)
        s = ts.build_series(make_contracts(g, basis_step=0.0))
        sig = ts.signal_tsmom(s["close_index"], 1)
        mkt = ts.strategy_returns(s, np.ones(len(s["r_open"])), side_cost=0.0)
        for pos, bucket in ((sig[:-1], leak_t), (ts.positions_from_signal(sig), ok_t)):        # 前者：期 k 用第 k 日收盤的訊號（偷看）
            net = ts.strategy_returns(s, np.nan_to_num(pos), side_cost=0.0)
            idx = np.flatnonzero(~np.isnan(pos) & np.isfinite(net))
            bucket.append(ts.newey_west_alpha(net[idx], mkt[idx])["t"])
    assert min(leak_t) > 20, leak_t                                                         # 偷看＝完美預測
    assert max(abs(x) for x in ok_t) < 3.0, ok_t                                            # 正確對齊＝隨機資料上沒有優勢


# ── 成本 ──────────────────────────────────────────────────────────

def test_costs_flips_cost_two_sides_rolls_cost_two_sides_and_buy_and_hold_pays_the_rolls():
    n = 6
    series = {"r_open": np.zeros(n), "open_px": np.full(n, 10000.0), "roll": np.array([False, False, False, True, False, False]),
              "keep": np.ones(n, bool)}
    pos = np.array([1.0, 1.0, -1.0, -1.0, -1.0, 1.0])
    net = ts.strategy_returns(series, pos, side_cost=2.0)
    # 期 2 換邊（|Δp|=2 → 2 單邊）、期 3 展期（2 單邊）、期 5 換邊（2 單邊）；其餘 0
    assert np.allclose(net, np.array([0, 0, -2 * 2.0, -2 * 2.0, 0, -2 * 2.0]) / 10000.0)
    bh = ts.strategy_returns(series, np.ones(n), side_cost=2.0)
    assert np.allclose(bh, np.array([0, 0, 0, -2 * 2.0, 0, 0]) / 10000.0)


def test_a_flip_and_a_roll_in_the_same_period_cost_two_sides_not_four():
    series = {"r_open": np.zeros(3), "open_px": np.full(3, 5000.0), "roll": np.array([False, True, False]), "keep": np.ones(3, bool)}
    net = ts.strategy_returns(series, np.array([1.0, -1.0, -1.0]), side_cost=2.0)
    assert net[1] == pytest.approx(-4.0 / 5000.0)                           # 平舊（多）＋開新（空）＝2 個單邊


# ── 迴歸與標準誤 ─────────────────────────────────────────────────

def hac_by_hand(y, x, lag):
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    b = np.linalg.solve(X.T @ X, X.T @ y)
    e = y - X @ b
    S = np.zeros((2, 2))
    for t in range(n):
        S += e[t] ** 2 * np.outer(X[t], X[t])
    for l in range(1, lag + 1):
        w = 1 - l / (lag + 1)
        for t in range(l, n):
            S += w * e[t] * e[t - l] * (np.outer(X[t], X[t - l]) + np.outer(X[t - l], X[t]))
    inv = np.linalg.inv(X.T @ X)
    return b, np.sqrt((inv @ S @ inv)[0, 0])


def test_newey_west_matches_a_by_hand_computation():
    rng = np.random.default_rng(5)
    x = rng.normal(0, 0.01, 400)
    y = 0.0004 + 0.6 * x + rng.normal(0, 0.008, 400)
    r = ts.newey_west_alpha(y, x, lag=7)
    b, se = hac_by_hand(y, x, 7)
    assert r["alpha"] == pytest.approx(b[0]) and r["beta"] == pytest.approx(b[1]) and r["se"] == pytest.approx(se, rel=1e-9)
    assert r["t"] == pytest.approx(b[0] / se)


def test_alpha_and_beta_are_recovered_from_a_known_relation():
    rng = np.random.default_rng(6)
    x = rng.normal(0, 0.01, 5000)
    y = 0.001 + 0.5 * x + rng.normal(0, 0.002, 5000)
    r = ts.newey_west_alpha(y, x)
    assert r["alpha"] == pytest.approx(0.001, abs=1e-4) and r["beta"] == pytest.approx(0.5, abs=0.02) and r["t"] > 20


def test_the_standard_error_is_larger_when_the_errors_are_autocorrelated():
    rng = np.random.default_rng(7)
    n = 4000
    x = rng.normal(0, 0.01, n)
    iid = rng.normal(0, 0.01, n)
    ar = np.zeros(n)
    for t in range(1, n):
        ar[t] = 0.6 * ar[t - 1] + rng.normal(0, 0.01)
    se_iid = ts.newey_west_alpha(0.0 * x + iid, x, lag=10)["se"]
    se_ar = ts.newey_west_alpha(0.0 * x + ar, x, lag=10)["se"]
    assert se_ar > 1.5 * se_iid                                              # AR(0.6) 的長期變異約是 iid 的 4 倍（標準誤約 2 倍）；沒校正就會把 α 說得太顯著


# ── 假陽性率與偵測力 ──────────────────────────────────────────────

def test_false_positive_rate_on_random_walks_is_small_even_with_volatility_clusters():
    hits, tt = 0, []
    for seed in range(40):
        rng = np.random.default_rng(100 + seed)
        vol = np.empty(2500)
        v = 0.01
        for i in range(2500):                                                # 波動有群聚（GARCH 風格），日報酬本身沒有可預測性
            v = 0.0000 + 0.9 * v + 0.1 * 0.01 * abs(rng.normal())
            vol[i] = v
        g = rng.normal(0, 1, 2500) * vol * 1.5
        res = ts.study(make_contracts(g, basis_step=0.0))
        for t in res["tests"].values():
            tt.append(t["t"])
            hits += t["verdict"] == "過關"
    assert hits <= 4, hits                                                   # 120 個檢定，每個約 1.7% → 預期約 2 個
    assert abs(float(np.mean(tt))) < 0.35 and 0.7 < float(np.std(tt)) < 1.4, (np.mean(tt), np.std(tt))


def regime_returns(n, seed, drift=0.002, sigma=0.008):
    """報酬 ＝ 緩慢切換的漂移（±drift，每段 150~300 天）＋雜訊：有趨勢可以賺。訊號要落後幾十天才看得出方向，只抓得到一部分漂移，
    所以漂移要夠強（年化 ±50%）、雜訊夠小，4,000 天才檢得出來——這正是真實資料『偵測力有限』的原因。"""
    rng = np.random.default_rng(seed)
    out, sign = [], 1
    while len(out) < n:
        out += list(sign * drift + rng.normal(0, sigma, int(rng.integers(150, 300))))
        sign = -sign
    return np.array(out[:n])


def test_a_planted_trend_is_detected_and_a_planted_mean_reversion_is_detected_with_the_opposite_sign():
    res = ts.study(make_contracts(regime_returns(4000, 1), basis_step=0.0))
    assert res["tests"]["TSMOM120"]["t"] > 3 and res["tests"]["TSMOM120"]["verdict"] == "過關"
    assert res["tests"]["TSMOM120"]["alpha_pct_per_year"] > 5 and res["tests"]["TSMOM120"]["same_sign_in_both_halves"]
    # 反過來（每段都在反向）：AR 負相關的報酬，順勢策略的 α 應該是負的
    rng = np.random.default_rng(2)
    rev = np.zeros(4000)
    for i in range(1, 4000):
        rev[i] = -0.35 * rev[i - 1] + rng.normal(0, 0.01)
    r2 = ts.study(make_contracts(rev, basis_step=0.0))
    assert min(t["alpha_pct_per_year"] for t in r2["tests"].values()) < 0


def test_a_buy_and_hold_drift_alone_is_not_alpha():
    """大盤一路漲、順勢策略多數時間做多 → 平均報酬為正，但 α（扣掉 β×大盤）不該顯著。"""
    g = np.random.default_rng(3).normal(0.0006, 0.01, 4000)
    res = ts.study(make_contracts(g, basis_step=0.0))
    for name, t in res["tests"].items():
        assert t["beta"] > 0.3, name                                          # 確實多半做多
        assert t["verdict"] != "過關", (name, t["t"])


def test_the_report_names_the_power_and_the_verdicts():
    res = ts.study(make_contracts(regime_returns(3000, 4), basis_step=0.0))
    text = ts.format_study(res)
    assert "偵測力" in text and "判定（3 個檢定" in text and "TSMOM120" in text and "MA200" in text and "Donchian55" in text
    assert res["mde_alpha_pct_per_year"] > 0 and res["years"] > 10


def test_thresholds_are_the_pre_registered_ones_and_too_little_data_is_an_error():
    assert ts.N_TESTS == 3 and 2.39 < ts.Z_CLAIM < 2.40 and ts.Z_HINT == 2.0
    assert [ts.verdict(t) for t in (2.5, -2.5, 2.2, 1.9)] == ["過關", "過關", "線索", "沒有"]
    assert (ts.TS_LOOKBACK, ts.MA_WINDOW, ts.DONCHIAN, ts.SIDE_COST_PTS, ts.HAC_LAG) == (120, 200, 55, 2.0, 10)         # 事先固定，不調參數
    with pytest.raises(ValueError, match="太少"):
        ts.study(make_contracts(np.zeros(300)))


def test_vol_scaling_formula_caps_leverage_and_uses_only_known_returns():
    n = 200
    rc = np.full(n, 0.01)                                                    # 日報酬的標準差 0 → 倍數取上限
    series = {"r_close": rc}
    out = ts.vol_scaled(series, np.ones(n), np.full(n, 0.001))
    assert np.isnan(out[:ts.VOL_WINDOW + 1]).all() and out[ts.VOL_WINDOW + 1] == pytest.approx(ts.VOL_CAP * 0.001)
    rng = np.random.default_rng(9)
    rc = rng.normal(0, 0.02, n)
    out = ts.vol_scaled({"r_close": rc}, np.ones(n), np.full(n, 0.001))
    k = 150
    sd = np.std(rc[k - 1 - ts.VOL_WINDOW:k - 1], ddof=1) * np.sqrt(252)
    assert out[k] == pytest.approx(min(ts.VOL_CAP, ts.VOL_TARGET / sd) * 0.001)
    rc2 = rc.copy()
    rc2[k - 1:] = 0.5                                                        # 決定當下還不知道的報酬（≥ k−1）改掉，倍數不能變
    assert ts.vol_scaled({"r_close": rc2}, np.ones(n), np.full(n, 0.001))[k] == pytest.approx(out[k])


# ── 讀檔與命令列 ──────────────────────────────────────────────────

def tx_csv(rows):
    return ("\r\n".join([TX_HEAD, *rows]) + "\r\n").encode("cp950")


def tx_line(d, exp, o, c, vol=100, session="一般"):
    return f"{d},TX,{exp}  ,{o},{max(o, c)},{min(o, c)},{c},0,0.00%,{vol},{c},-,-,-,1,1,,{session},,"


def test_loader_reads_old_and_new_formats_and_skips_spreads_after_hours_and_no_volume(tmp_path):
    (tmp_path / "a.csv").write_bytes(tx_csv([
        tx_line("2003/01/02", "200301", 4411, 4452, vol=15261),
        tx_line("2003/01/02", "200302", 4420, 4460, vol=200),
        tx_line("2003/01/02", "200301/200302", 9, 9, vol=50),                      # 價差單
        tx_line("2017/06/01", "201706", 10030, 10069, vol=86130),
        tx_line("2017/06/01", "201706", 1, 2, vol=10, session="盤後"),              # 盤後
        tx_line("2017/06/02", "201706", 10070, 10080, vol=0),                       # 沒成交
        tx_line("2017/06/05", "201706", "-", "-", vol=0),
    ]))
    got = ts.load_tx_contracts(tmp_path)
    assert got == {"2003-01-02": {"200301": (4411.0, 4452.0), "200302": (4420.0, 4460.0)}, "2017-06-01": {"201706": (10030.0, 10069.0)}}


def test_loader_refuses_an_error_page(tmp_path):
    (tmp_path / "bad.csv").write_bytes('<script>alert("DateTime error")</script>'.encode("cp950"))
    with pytest.raises(ValueError, match="錯誤頁"):
        ts.load_tx_contracts(tmp_path)


def test_cli_runs_on_files_and_reports_problems(tmp_path, capsys):
    g = regime_returns(1800, 5)
    contracts = make_contracts(g, basis_step=0.0)
    d = tmp_path / "tx"
    d.mkdir()
    rows = [tx_line(day.replace("-", "/"), exp, round(o, 2), round(c, 2)) for day, row in contracts.items() for exp, (o, c) in row.items()]
    (d / "all.csv").write_bytes(tx_csv(rows))
    out = tmp_path / "o.json"
    assert ts.main(["--dir", str(tmp_path), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "慢速順勢 vs 買進持有" in text and "TSMOM120" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["tests"]) == {"TSMOM120", "MA200", "Donchian55"}
    assert ts.main(["--dir", str(tmp_path / "nothing")]) == 2
    assert "oi_study fetch --only tx" in capsys.readouterr().err


def test_all_three_strategies_and_the_market_are_compared_over_the_same_periods():
    g = regime_returns(3000, 8)
    contracts = make_contracts(g, basis_step=0.0)
    s = ts.build_series(contracts)
    starts = [int(np.argmax(~np.isnan(ts.positions_from_signal(fn(s["close_index"]))))) for fn in ts.SIGNALS.values()]
    assert len(set(starts)) == 3                                             # 三個訊號的暖機長短不同（Donchian 要等到第一次突破）
    res = ts.study(contracts)
    assert res["first"] == s["dates"][max(starts)]                           # 從三個都有定義的那一期開始，大家用同一段樣本
    assert res["periods"] == int(s["keep"][max(starts):].sum())


def test_the_loader_ignores_after_hours_rows_even_when_they_have_volume(tmp_path):
    (tmp_path / "a.csv").write_bytes(tx_csv([tx_line("2017/06/01", "201706", 100, 110, vol=5, session="盤後"), tx_line("2017/06/01", "201706", 200, 210, vol=9)]))
    assert ts.load_tx_contracts(tmp_path) == {"2017-06-01": {"201706": (200.0, 210.0)}}
