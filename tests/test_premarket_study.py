"""盤前跨市場資訊的歷史預檢（core/premarket_study.py，update.md 發現 31 A）。

重點不是「有沒有結論」，而是這套檢驗不會騙人：
- 資料：期交所盤後列的歸屬、合約的選法、R20／目標／基準特徵的定義，用手算的小例子。
- 對齊：美股欄位用哪一個交易日（週一用上週五、休市日缺），用**獨立重寫**的算法逐日比對；植入「只依賴正確那一天」的現象——對齊差一天、或偷看之後的一天，就偵測不到。
- 統計：擴展視窗 OLS 對照另一種算法（正規方程）、不偷看未來的金絲雀、Newey–West 與 Clark–West 對照暴力算法、植入現象偵測得到、沒有現象時假陽性率低。
"""
from __future__ import annotations

import json
import math
import random
import statistics
from datetime import date, timedelta

import numpy as np
import pytest

import core.premarket_log as pm
import core.premarket_study as ps
from core.premarket_log import Bar

HEAD = ("交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,"
        "歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量")


def tx_line(d, code, o, h, l, c, vol, session="一般"):
    return f"{d.replace('-', '/')},TX,{code},{o},{h},{l},{c},0,0,{vol},0,0,-,-,-,-,,{session},0"


def write_tx(path, lines):
    path.write_bytes(("\n".join([HEAD] + lines) + "\n").encode("cp950"))


# ── TX 日行情：盤後列的歸屬與合約的選法 ────────────────────────────

def test_load_tx_days_picks_the_most_active_contract_that_has_all_the_rows(tmp_path):
    rows = [
        tx_line("2017-05-15", "201705", 100, 110, 90, 105, 500), tx_line("2017-05-15", "201706", 200, 210, 190, 205, 20),
        # 5/16：兩個合約都有盤後列；201706 的日盤成交量較小 → 選 201705
        tx_line("2017-05-16", "201705", 106, 120, 100, 115, 900), tx_line("2017-05-16", "201705", 0, 0, 0, 111, 400, "盤後"),
        tx_line("2017-05-16", "201706", 206, 220, 200, 215, 50), tx_line("2017-05-16", "201706", 0, 0, 0, 211, 10, "盤後"),
        # 5/17：201705 沒有盤後列（或成交量 0）→ 只剩 201706
        tx_line("2017-05-17", "201705", 116, 130, 110, 125, 800), tx_line("2017-05-17", "201705", 0, 0, 0, 120, 0, "盤後"),
        tx_line("2017-05-17", "201706", 216, 230, 210, 225, 60), tx_line("2017-05-17", "201706", 0, 0, 0, 221, 12, "盤後"),
        # 5/18：沒有任何合約同時有盤後列與前一天的日盤列（201709 前一天沒有日盤列）→ 這天不算
        tx_line("2017-05-18", "201709", 300, 310, 290, 305, 10), tx_line("2017-05-18", "201709", 0, 0, 0, 301, 5, "盤後"),
        # 5/19：價差單（代碼有斜線）、週選擇權之類的非 6 位數代碼、沒有成交的列：全部略過
        tx_line("2017-05-19", "201706/201709", 1, 2, 0, 1, 50), tx_line("2017-05-19", "201706", 226, 240, 220, 235, 70), tx_line("2017-05-19", "201706", 0, 0, 0, 231, 9, "盤後"),
        tx_line("2017-05-19", "201705", 126, 140, 120, 135, 0),
    ]
    write_tx(tmp_path / "tx_2017_05.csv", rows)
    days = ps.load_tx_days(tmp_path)
    assert [(d.date, d.contract) for d in days] == [(date(2017, 5, 16), "201705"), (date(2017, 5, 17), "201706")]       # 5/18、5/19 都沒有合約同時有盤後列與前一個交易日的日盤列
    d = days[0]
    assert (d.o, d.h, d.l, d.c, d.night, d.prev_close, d.volume) == (106.0, 120.0, 100.0, 115.0, 111.0, 105.0, 900.0)       # 夜盤收盤＝盤後列的收盤、前收＝前一天日盤的收盤
    assert (days[1].o, days[1].c, days[1].night, days[1].prev_close) == (216.0, 225.0, 221.0, 215.0)                          # 201706 的前一天（5/16）日盤收盤是 215


def test_load_tx_days_ignores_dates_before_the_first_night_session_and_reads_cp950(tmp_path):
    rows = [tx_line("2017-05-12", "201705", 1, 2, 0, 1, 5), tx_line("2017-05-15", "201705", 1, 2, 0, 1, 5), tx_line("2017-05-15", "201705", 0, 0, 0, 1, 5, "盤後"),
            tx_line("2017-05-16", "201705", 1, 3, 0, 2, 5), tx_line("2017-05-16", "201705", 0, 0, 0, 1, 5, "盤後")]
    write_tx(tmp_path / "tx_2017_05.csv", rows)
    assert [d.date for d in ps.load_tx_days(tmp_path)] == [date(2017, 5, 16)]                                                  # 5/15 的盤後列不屬於任何樣本日（它是 5/15 的日盤之前）
    assert [d.date for d in ps.load_tx_days(tmp_path, first=date(2017, 5, 15))] == [date(2017, 5, 15), date(2017, 5, 16)]
    (tmp_path / "tx_bad.csv").write_text("<html>錯誤頁</html>", encoding="cp950")
    with pytest.raises(ValueError):
        ps.load_tx_days(tmp_path)


def test_load_series_and_fetch_write_small_csv_files(tmp_path):
    ok = {"/SOX/historical": json.dumps({"data": {"tradesTable": {"rows": [{"date": "10/12/2026", "close": "12,236.79"}, {"date": "10/09/2026", "close": "12,572.00"}]}}}),
          "/TSM/historical": json.dumps({"data": {"tradesTable": {"rows": [{"date": "10/12/2026", "close": "$444.35"}]}}}),
          "VIX_History.csv": "DATE,OPEN,HIGH,LOW,CLOSE\n10/09/2026,1,1,1,16.1\n10/12/2026,1,1,1,15.21\n"}

    def http(url, headers=None, timeout=25):
        for k, v in ok.items():
            if k in url:
                return v
        raise pm.SourceError("http", url)

    got = ps.fetch(tmp_path, since=date(2026, 10, 1), http=http, pause=0)
    assert got == {"tsm": 1, "sox": 2, "vix": 2}
    assert ps.load_series(tmp_path / "sox.csv") == [Bar(date(2026, 10, 9), 12572.0, "sox"), Bar(date(2026, 10, 12), 12236.79, "sox")]
    assert (tmp_path / "vix.csv").read_text().splitlines() == ["date,close", "2026-10-09,16.1", "2026-10-12,15.21"]
    ok["VIX_History.csv"] = "garbage"
    assert ps.fetch(tmp_path, since=date(2026, 10, 1), http=http, pause=0)["vix"] == -1                                          # 失敗：回報、不覆蓋舊檔
    assert (tmp_path / "vix.csv").read_text().startswith("date,close")


# ── 統計函式：對照另一種算法、不偷看未來 ───────────────────────────

def test_oos_predict_equals_the_normal_equations_and_never_looks_ahead():
    rng = np.random.default_rng(1)
    n, init = 120, 40
    X = np.column_stack([np.ones(n), rng.normal(size=(n, 2))])
    y = X @ np.array([0.5, 1.0, -2.0]) + rng.normal(size=n) * 0.3
    got = ps.oos_predict(y, X, init)
    for i in (init, init + 7, n - 1):
        beta = np.linalg.solve(X[:i].T @ X[:i], X[:i].T @ y[:i])                                                              # 正規方程：另一種算法
        assert got[i - init] == pytest.approx(X[i] @ beta, abs=1e-9)
    y2, X2 = y.copy(), X.copy()
    y2[80:], X2[80:, 1:] = rng.normal(size=n - 80) * 50, rng.normal(size=(n - 80, 2)) * 50                                  # 把第 80 天以後的資料全部換掉
    got2 = ps.oos_predict(y2, X2, init)
    assert np.allclose(got[: 80 - init], got2[: 80 - init])                                                                   # 第 80 天以前的預測一模一樣；第 80 天自己的預測也只用到 80 以前
    assert got[80 - init] == pytest.approx(X[80] @ np.linalg.solve(X[:80].T @ X[:80], X[:80].T @ y[:80]), abs=1e-9)


def test_newey_west_standard_error_matches_a_brute_force_double_loop():
    rng = np.random.default_rng(2)
    x = rng.normal(size=60) + np.sin(np.arange(60) / 5)
    for lag in (0, 3, 10):
        m = x.mean()
        n = len(x)
        gamma = lambda l: sum((x[t] - m) * (x[t - l] - m) for t in range(l, n)) / n                                            # noqa: E731
        lrv = gamma(0) + 2 * sum((1 - l / (lag + 1)) * gamma(l) for l in range(1, lag + 1))
        assert ps.nw_se_mean(x, lag) == pytest.approx(math.sqrt(lrv / n))


def test_clark_west_by_hand():
    y = np.array([1.0, 2.0, 3.0, 4.0])
    p0 = np.array([0.0, 2.0, 2.0, 5.0])                                  # 基準的預測
    p1 = np.array([0.5, 2.0, 3.0, 4.0])                                  # 大模型的預測
    e0, e1 = y - p0, y - p1                                              # e0＝[1, 0, 1, −1]、e1＝[0.5, 0, 0, 0]；p0−p1＝[−0.5, 0, −1, 1]
    f = e0 ** 2 - (e1 ** 2 - (p0 - p1) ** 2)                             # [1−(0.25−0.25), 0−(0−0), 1−(0−1), 1−(0−1)]＝[1, 0, 2, 2]
    assert f.tolist() == [1.0, 0.0, 2.0, 2.0]
    r = ps.clark_west(y, p0, p1, lag=0)
    assert r["mean_f"] == 1.25 and r["mspe0"] == 0.75 and r["mspe1"] == pytest.approx(0.0625)
    assert r["improve_pct"] == pytest.approx(1 - 0.0625 / 0.75) and r["adj_improve_pct"] == pytest.approx(1.25 / 0.75)
    se = math.sqrt(0.6875 / 4)                                           # 變異數（除以 n）＝ (0.0625+1.5625+0.5625+0.5625)/4 ＝ 0.6875
    assert r["se"] == pytest.approx(se) and r["z"] == pytest.approx(1.25 / se) and r["z"] == pytest.approx(3.0151, abs=1e-3)
    assert r["mde_pct"] == pytest.approx((ps.Z_CLAIM + ps.Z_POWER) * se / 0.75)
    assert ps.clark_west(y, p0, p0)["mean_f"] == 0.0                                                      # 兩個模型一樣：沒有差別


def test_ols_hac_matches_a_brute_force_sandwich():
    rng = np.random.default_rng(3)
    n = 150
    X = np.column_stack([np.ones(n), rng.normal(size=n)])
    y = 0.4 + 0.8 * X[:, 1] + rng.normal(size=n)
    got = ps.ols_hac(y, X, lag=4)
    beta = np.linalg.solve(X.T @ X, X.T @ y)
    u = y - X @ beta
    S = np.zeros((2, 2))
    for t in range(n):
        S += np.outer(X[t] * u[t], X[t] * u[t])
    for l in range(1, 5):
        for t in range(l, n):
            g = np.outer(X[t] * u[t], X[t - l] * u[t - l])
            S += (1 - l / 5) * (g + g.T)
    inv = np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(inv @ S @ inv))
    assert got["beta"] == pytest.approx(beta.tolist()) and got["t"] == pytest.approx((beta / se).tolist())
    assert 0 < got["r2"] < 1


def test_verdict_thresholds_are_the_preregistered_ones():
    assert ps.N_TESTS == 4 and ps.Z_CLAIM == pytest.approx(2.2414, abs=1e-3) and ps.Z_HINT == pytest.approx(1.6449, abs=1e-3)
    assert (ps.INIT_WINDOW, ps.NW_LAG, ps.R_DAYS) == (500, 10, 20) and ps.STUDY_T.hour == 8 and ps.STUDY_T.minute == 30
    assert ps.verdict(ps.Z_CLAIM) == "過關" and ps.verdict(ps.Z_CLAIM - 0.01) == "線索" and ps.verdict(ps.Z_HINT - 0.01) == "沒有" and ps.verdict(-5.0) == "沒有"
    assert ps.verdict(float("nan")) == "無法檢定"
    assert ps.RANGES == {"sox": (-0.25, 0.25), "tsm": (-0.25, 0.25), "vix": (5.0, 120.0)}


def test_null_models_rarely_pass_and_a_real_effect_always_does():
    """沒有現象：多個獨立的合成資料集，z ≥ 2.24 的比例要低；有現象：每一次都要過。"""
    hits, n_tests = 0, 0
    for seed in range(120):
        rng = np.random.default_rng(1000 + seed)
        n = 260
        cols = {"y": np.zeros(n), "a": rng.normal(size=n), "p": np.zeros(n), "x": rng.normal(size=n)}
        for i in range(n):
            cols["p"][i] = cols["y"][i - 1] if i else 0.0
            cols["y"][i] = 0.3 * cols["a"][i] + 0.3 * cols["p"][i] + rng.normal() * 0.5
        dates = [date(2018, 1, 1) + timedelta(days=i) for i in range(n)]
        res = ps.compare(cols, dates, "y", ("a", "p"), {"M1_x": ("x",)}, init=120)
        hits += res["models"]["M1_x"]["z"] >= ps.Z_CLAIM
        n_tests += 1
    assert hits / n_tests <= 0.05, hits                                                                    # 名目 1.25%；Newey–West 在小樣本偏寬，放寬到 5%
    for seed in range(5):
        rng = np.random.default_rng(7 + seed)
        n = 400
        x = rng.normal(size=n)
        cols = {"y": 0.5 * x + rng.normal(size=n) * 0.5, "a": rng.normal(size=n), "p": rng.normal(size=n), "x": x}
        res = ps.compare(cols, [date(2018, 1, 1) + timedelta(days=i) for i in range(n)], "y", ("a", "p"), {"M1_x": ("x",)}, init=150)
        assert res["models"]["M1_x"]["z"] > ps.Z_CLAIM and res["models"]["M1_x"]["improve_pct"] > 0.2


def test_compare_refuses_too_little_data():
    n = 100
    cols = {k: np.random.default_rng(0).normal(size=n) for k in ("y", "a", "p", "x")}
    with pytest.raises(ValueError, match="不夠"):
        ps.compare(cols, [date(2018, 1, 1) + timedelta(days=i) for i in range(n)], "y", ("a", "p"), {"M": ("x",)}, init=90)


# ── 合成世界：整條流程（資料 → 對齊 → 特徵 → 樣本外檢定）──────────

LAM, B0 = 0.05, -0.30          # 讓範圍不會爆炸或消失的均值回歸（模擬過）


def weekdays_from(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_world(tmp_path, *, seed=1, n=1100, coef=(0.30, 0.25, 0.30, 0.0, 0.0), shift=0, noise=0.30):
    """依預登記的定義生成：台指期日盤的 ln((H−L)÷R20) ＝ B0 ＋ ba·a ＋ bp·p ＋ bs·s ＋ bt·t ＋ bv·v ＋ 雜訊（− LAM·ln(R/100) 只為了不爆炸）。
    美股特徵用**自己寫的**算法（不呼叫 premarket_log 的對齊函式）：台股交易日 D 的最近美股收盤＝D−1（週一是 D−3）；那天休市就缺。
    shift：把植入的現象放在正確那一天（0）、前一個美股交易日（−1）、之後一個（+1，T 時還沒收盤）。"""
    rng = random.Random(seed)
    ba, bp, bs, bt, bv = coef
    tw = weekdays_from(date(2018, 1, 2), n)
    us = [d for d in (date(2017, 8, 1) + timedelta(days=i) for i in range(0, (tw[-1] - date(2017, 8, 1)).days + 10)) if pm.us_session_info(d)[0] != "closed"]
    series: dict[str, list[float]] = {}
    for key, base in (("sox", 100.0), ("tsm", 100.0)):
        c, out = base, []
        for k in range(len(us)):
            c *= math.exp(rng.gauss(0, 0.012 * (1 + 0.6 * math.sin(k / 30))))
            out.append(c)
        series[key] = out
    level, vix = 2.8, []
    for k in range(len(us)):
        level = 0.97 * level + 0.03 * 2.8 + rng.gauss(0, 0.06)
        vix.append(math.exp(level))
    series["vix"] = vix

    def feats(idx):
        """獨立算法：session 在 us 裡的位置 idx → (s, t, v)。"""
        out, signed = [], []
        for key in ("sox", "tsm"):
            c = series[key]
            rets = [math.log(c[j] / c[j - 1]) for j in range(idx - 20, idx)]
            z = math.log(c[idx] / c[idx - 1]) / statistics.stdev(rets)
            out.append(math.log1p(abs(z)))
            signed.append(max(-4.0, min(4.0, z)))
        out.append(math.log(vix[idx] / (sum(vix[idx - 20: idx]) / 20)))
        return out + signed

    ranges, y_prev, prev_close = [100.0] * 20, 0.0, 10000.0
    rows = [tx_line((tw[0] - timedelta(days=1)).isoformat(), "209912", 10000, 10010, 9990, 10000, 1000)]            # 第一個樣本日的前一天（只有日盤）
    expected: list[dict] = []
    for i, D in enumerate(tw):
        w_cal = D - timedelta(days=3 if D.weekday() == 0 else 1)
        closed = pm.us_session_info(w_cal)[0] == "closed"
        sess = None if closed else us.index(w_cal)
        s = t = v = rs = rt = 0.0
        if sess is not None:
            s, t, v, rs, rt = feats(sess + shift)
        R = sum(ranges[-20:]) / 20
        gap = rng.gauss(0, 0.5) * R
        a = math.log1p(abs(gap) / R)
        y = B0 + ba * a + bp * y_prev + bs * s + bt * t + bv * v + rng.gauss(0, noise) - LAM * math.log(R / 100)
        rng_pts = R * math.exp(y)
        o = prev_close + rng.gauss(0, 0.2) * R
        low = o - rng.uniform(0.2, 0.8) * rng_pts
        high = low + rng_pts
        c = low + rng.uniform(0.1, 0.9) * rng_pts
        night = prev_close + gap
        rows.append(tx_line(D.isoformat(), "209912", round(o, 4), round(high, 4), round(low, 4), round(c, 4), 1000))
        rows.append(tx_line(D.isoformat(), "209912", 0, 0, 0, round(night, 4), 500, "盤後"))
        rows.append(tx_line(D.isoformat(), "209909", 1, 2, 0.5, 1.5, 3))                                                  # 干擾合約：成交量小、沒有盤後列
        expected.append({"date": D, "s": s, "t": t, "v": v, "rs": rs, "rt": rt, "missing": closed, "R": R, "a": a})
        ranges.append(high - low)
        y_prev, prev_close = y, round(c, 4)
    txd, dd = tmp_path / "tx", tmp_path / "premarket"
    txd.mkdir(), dd.mkdir()
    write_tx(txd / "tx_all.csv", rows)
    for key in ("sox", "tsm", "vix"):
        lines = ["date,close"] + [f"{d.isoformat()},{c}" for d, c in zip(us, series[key])]
        if key == "vix":                                                                                                    # Cboe 在美股休市日也有值：不能被用到
            lines += [f"{d.isoformat()},99.0" for d in (date(2018, 5, 28), date(2018, 7, 4), date(2019, 11, 28))]
        (dd / f"{key}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txd, dd, expected


def test_the_pipeline_reproduces_the_independent_features_day_by_day(tmp_path):
    txd, dd, expected = make_world(tmp_path, seed=3, n=300)
    days, ds = ps._load_all(txd, dd)
    assert len(days) == 300 and [d.date for d in days] == [e["date"] for e in expected]
    c = ds.cols
    for i, e in enumerate(expected):
        assert np.isnan(c["y"][i]) == (i < ps.R_DAYS)                                                          # 前 20 天沒有 R20
        if e["missing"]:
            assert all(np.isnan(c[k][i]) for k in ("s", "t", "v")), e["date"]                                  # 美股休市：缺，不沿用前一天
        else:
            assert (c["s"][i], c["t"][i], c["v"][i]) == pytest.approx((e["s"], e["t"], e["v"]), abs=1e-9), e["date"]
            assert (c["rs"][i], c["rt"][i]) == pytest.approx((e["rs"], e["rt"]), abs=1e-9), e["date"]                    # 方向描述用：有正負號的 報酬÷σ20（截在 ±4）
        if i >= ps.R_DAYS:
            assert c["a"][i] == pytest.approx(e["a"], abs=1e-3)                                                # 價格寫進 CSV 時四捨五入到 4 位
    # 週一用上週五（上面逐日比對已涵蓋）、美股休市日後的第一個台股交易日缺
    holiday_after = [e["date"] for e in expected if e["missing"]]
    assert holiday_after and all(pm.us_session_info(d - timedelta(days=3 if d.weekday() == 0 else 1))[0] == "closed" for d in holiday_after)
    expect_usable = sum(1 for i, e in enumerate(expected) if i >= ps.R_DAYS + 1 and not e["missing"])
    keep = np.all([np.isfinite(c[k]) for k in ("y", "a", "p", "s", "t", "v")], axis=0)
    assert int(keep.sum()) == expect_usable
    assert ds.info["us_closed"] == len(holiday_after)


def test_the_target_and_baseline_are_defined_as_registered(tmp_path):
    txd, dd, _ = make_world(tmp_path, seed=4, n=60)
    days, ds = ps._load_all(txd, dd)
    rng_pts = [d.h - d.l for d in days]
    i = 33
    R = sum(rng_pts[i - 20: i]) / 20
    assert ds.cols["y"][i] == pytest.approx(math.log(rng_pts[i] / R))
    assert ds.cols["a"][i] == pytest.approx(math.log1p(abs(days[i].night - days[i].prev_close) / R))
    assert ds.cols["p"][i] == pytest.approx(ds.cols["y"][i - 1])
    assert ds.cols["dirt"][i] == pytest.approx((days[i].c - days[i].o) / R)
    assert ds.cols["gap"][i] == pytest.approx(float(np.clip((days[i].night - days[i].prev_close) / R, -3, 3)))
    assert ds.cols["pdir"][i] == pytest.approx(ds.cols["dirt"][i - 1])


def run_world(tmp_path, **kw):
    txd, dd, _ = make_world(tmp_path, **kw)
    _, ds = ps._load_all(txd, dd)
    return ps.run_study(ds, init=300), ds


def test_a_planted_effect_on_the_right_us_session_is_found_and_the_others_are_not(tmp_path):
    res, ds = run_world(tmp_path, seed=11, coef=(0.30, 0.25, 0.35, 0.0, 0.0))
    m = res["vol"]["models"]
    assert m["M1_s"]["verdict"] == "過關" and m["M1_s"]["z"] > 6 and m["M1_all"]["verdict"] == "過關"
    assert m["M1_t"]["verdict"] != "過關" and m["M1_v"]["verdict"] != "過關" and m["M1_t"]["z"] < ps.Z_CLAIM and m["M1_v"]["z"] < ps.Z_CLAIM
    assert m["M1_s"]["years_positive"] >= len(m["M1_s"]["by_year"]) - 1 and m["M1_s"]["improve_pct"] > 0.05
    beta = res["descriptive"]["full_sample_ols"]["beta"]                                                         # 常數、a、p、s、t、v：植入的係數要回得來
    assert beta[1] == pytest.approx(0.30, abs=0.07) and beta[2] == pytest.approx(0.25, abs=0.07) and beta[3] == pytest.approx(0.35, abs=0.07)
    assert abs(beta[4]) < 0.1 and abs(beta[5]) < 0.15
    assert res["info"]["usable"] == int(np.all([np.isfinite(ds.cols[k]) for k in ("y", "a", "p", "s", "t", "v")], axis=0).sum())


def test_two_planted_effects_are_both_found(tmp_path):
    res, _ = run_world(tmp_path, seed=12, coef=(0.30, 0.25, 0.0, 0.35, 0.8))
    m = res["vol"]["models"]
    assert m["M1_t"]["verdict"] == "過關" and m["M1_v"]["verdict"] == "過關" and m["M1_all"]["verdict"] == "過關" and m["M1_s"]["z"] < ps.Z_CLAIM


def test_an_effect_planted_on_the_wrong_session_is_not_found(tmp_path):
    """對齊的金絲雀：現象放在前一個美股交易日（過期的資訊）或之後一個（T 時還沒收盤）——正確對齊的流程不能抓到。"""
    for shift in (-1, +1):
        sub = tmp_path / f"s{shift}"
        sub.mkdir()
        res, _ = run_world(sub, seed=13, coef=(0.30, 0.25, 0.60, 0.0, 0.0), shift=shift)
        m = res["vol"]["models"]
        assert all(m[k]["verdict"] != "過關" for k in m) and m["M1_s"]["z"] < 2.0, (shift, {k: m[k]["z"] for k in m})


def test_a_world_without_effects_finds_nothing(tmp_path):
    for seed in (21, 22):
        (tmp_path / str(seed)).mkdir()
        res, _ = run_world(tmp_path / str(seed), seed=seed, coef=(0.30, 0.25, 0.0, 0.0, 0.0))
        assert all(m["verdict"] != "過關" for m in res["vol"]["models"].values()), {k: m["z"] for k, m in res["vol"]["models"].items()}
        assert res["direction"]["models"]["M1_all"].get("verdict") is None                                        # 方向只描述、沒有判定


def test_report_graveyard_and_sanity_text(tmp_path, monkeypatch):
    txd, dd, _ = make_world(tmp_path, seed=14, n=900, coef=(0.30, 0.25, 0.35, 0.0, 0.0))
    days, ds = ps._load_all(txd, dd)
    res = ps.run_study(ds, init=300)
    text = ps.format_report(res)
    for needle in ("M1_all", "M1_s", "M1_t", "M1_v", "Clark–West", "過關", "方向", "不判定", "沒過關不等於證明沒有"):
        assert needle in text
    s = ps.sanity(days, ds)
    assert "TX 樣本日 900 天" in s and "美股休市" in s and "M1" not in s                                             # sanity 不算任何模型
    rows = ps.graveyard_rows(res)
    assert len(rows) == 8 and {r["family"] for r in rows} == {"vol", "dir_desc"}
    assert all(r["stat"]["z"] == r["stat"]["z"] for r in rows) and sum(r["verdict"] == "描述" for r in rows) == 4
    store = pm.PremarketStore(tmp_path / "t.db")
    for r in rows:
        store.add_graveyard(ps.STUDY_NAME, **r)
    assert store.status()["graveyard"] == 8


def test_cli_fetch_sanity_and_run_plumbing(tmp_path, monkeypatch, capsys):
    txd, dd, _ = make_world(tmp_path, seed=15, n=900, coef=(0.30, 0.25, 0.35, 0.0, 0.0))
    assert ps.main(["sanity", "--tx-dir", str(txd), "--data-dir", str(dd)]) == 0
    assert "TX 樣本日 900 天" in capsys.readouterr().out
    monkeypatch.setattr(pm, "_default_store", pm.PremarketStore(tmp_path / "trade_log.db"))
    out_json = tmp_path / "res.json"
    assert ps.main(["run", "--tx-dir", str(txd), "--data-dir", str(dd), "--json", str(out_json)]) == 0
    out = capsys.readouterr().out
    assert "墳場紀錄已寫入 8 筆" in out and "M1_s" in out
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["vol"]["models"]["M1_s"]["verdict"] == "過關" and pm.default_store().status()["graveyard"] == 8


def test_clipping_of_gaps_and_us_returns_and_the_signed_features():
    """夜盤缺口（÷R20）截在 ±3、美股報酬（÷σ20）截在 ±4；沒超過的原樣。"""
    tw = weekdays_from(date(2026, 1, 29), 27)                                                     # …, 2026-03-05（週四）、2026-03-06（週五）
    assert tw[-2:] == [date(2026, 3, 5), date(2026, 3, 6)]
    days = []
    for i, d in enumerate(tw):
        gap = 10 * 100.0 if d == tw[-2] else (-10 * 100.0 if d == tw[-1] else (50.0 if i == len(tw) - 3 else 0.0))
        days.append(ps.Day(d, "209912", 10000.0, 10050.0, 9950.0, 10020.0, 10000.0 + gap, 10000.0, 1000.0))      # 每天的範圍 100 → R20＝100
    series, rnd = {}, random.Random(5)
    for key in ps.SERIES:
        bars, c = [], 100.0
        for k, d in enumerate(x for x in (date(2025, 11, 1) + timedelta(days=j) for j in range(140)) if pm.us_session_info(x)[0] != "closed"):
            r = 0.02 if d == date(2026, 3, 4) else (-0.02 if d == date(2026, 3, 5) else rnd.gauss(0, 0.001))      # σ20 約 0.001，這兩天是 20 倍
            c *= math.exp(r)
            bars.append(Bar(d, c if key != "vix" else 15.0 * math.exp(k % 7 / 50), key))
        series[key] = bars
    ds = ps.build_dataset(days, series)
    c = ds.cols
    assert c["gap"][-2] == 3.0 and c["gap"][-1] == -3.0 and c["gap"][-3] == pytest.approx(0.5)                   # 缺口 10R → 截在 3；0.5R 原樣
    assert c["rs"][-2] == 4.0 and c["rt"][-2] == 4.0 and c["rs"][-1] == -4.0 and c["rt"][-1] == -4.0               # 報酬 +2％／−2％ 約是 σ 的 20 倍 → 截在 ±4
    assert 0 < c["s"][-2] < 10 and c["s"][-2] == pytest.approx(math.log1p(abs(math.log(1.02)) / 0.001), rel=0.4)        # 幅度特徵沒有截尾（取 log1p）
    assert abs(c["rs"][-4]) < 4 and np.isfinite(c["v"][-2])


def test_run_study_wiring_with_hand_built_columns_and_missing_values():
    """直接給欄位（不經資料處理）：每一個目標用到的欄位缺值時，那一天要被排除；基準與候選的欄位要接對。"""
    rng = np.random.default_rng(5)
    n = 700
    cols = {k: rng.normal(size=n) for k in ("a", "p", "s", "t", "v", "gap", "pdir", "rs", "rt")}
    cols["y"] = 0.4 * cols["a"] + 0.3 * cols["p"] + 0.8 * cols["s"] + rng.normal(size=n) * 0.5
    cols["dirt"] = 0.5 * cols["gap"] + 0.4 * cols["pdir"] + 0.9 * cols["rs"] + rng.normal(size=n) * 0.5
    cols["v"][rng.choice(n, 40, replace=False)] = np.nan                                              # VIX 缺：波動與方向都排除這些日子
    cols["rt"][rng.choice(n, 25, replace=False)] = np.nan                                             # 只有方向用到 rt：波動不受影響
    dates = [date(2018, 1, 1) + timedelta(days=i) for i in range(n)]
    ds = ps.Dataset(dates, cols, {"days": n, "us_closed": 0, "us_stale": 0, "flagged": {k: 0 for k in ps.SERIES}, "missing": {k: 0 for k in ps.SERIES}})
    res = ps.run_study(ds, init=300)
    keep_v = np.isfinite(cols["v"])
    keep_d = keep_v & np.isfinite(cols["rt"])
    assert res["info"]["usable"] == int(keep_v.sum()) and res["direction"]["n"] == int(keep_d.sum()) and res["vol"]["n"] == int(keep_v.sum())

    def indep_mspe(y, feats, mask, init):
        X = np.column_stack([np.ones(mask.sum())] + [cols[k][mask] for k in feats])
        yy, errs = y[mask], []
        for i in range(init, len(yy)):
            beta = np.linalg.solve(X[:i].T @ X[:i], X[:i].T @ yy[:i])
            errs.append((yy[i] - X[i] @ beta) ** 2)
        return float(np.mean(errs))

    assert res["vol"]["mspe0"] == pytest.approx(indep_mspe(cols["y"], ("a", "p"), keep_v, 300), rel=1e-9)                       # 基準是 a、p
    assert res["direction"]["mspe0"] == pytest.approx(indep_mspe(cols["dirt"], ("gap", "pdir"), keep_d, 300), rel=1e-9)           # 方向的基準是 gap、pdir
    m = res["vol"]["models"]
    assert m["M1_s"]["verdict"] == "過關" and m["M1_t"]["verdict"] != "過關" and m["M1_v"]["verdict"] != "過關" and m["M1_all"]["verdict"] == "過關"
    assert m["M1_s"]["mspe1"] == pytest.approx(indep_mspe(cols["y"], ("a", "p", "s"), keep_v, 300), rel=1e-9)
    d = res["direction"]["models"]
    assert d["M1_s"]["z"] > ps.Z_CLAIM and abs(d["M1_t"]["z"]) < 3 and "verdict" not in d["M1_s"]                                # 方向：只有描述、沒有判定
    assert d["M1_s"]["mspe1"] == pytest.approx(indep_mspe(cols["dirt"], ("gap", "pdir", "rs"), keep_d, 300), rel=1e-9)
    assert res["descriptive"]["full_sample_ols"]["beta"][3] == pytest.approx(0.8, abs=0.1)


def test_fetch_waits_between_requests(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr(ps.time, "sleep", lambda s: sleeps.append(s))
    http = lambda url, headers=None, timeout=25: (_ for _ in ()).throw(pm.SourceError("network", "x"))      # noqa: E731
    got = ps.fetch(tmp_path, since=date(2026, 10, 1), http=http, pause=1.5)
    assert got == {"tsm": -1, "sox": -1, "vix": -1} and sleeps == [1.5, 1.5, 1.5]                                  # 每個來源一次請求、之間隔 1.5 秒（失敗也一樣）
