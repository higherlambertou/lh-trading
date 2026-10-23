"""週選跨式買方的時機（core/weekly_straddle_study.py，update.md 發現 71）。

合成世界：指數每天的波動 σ 是會持續的（log σ 是 AR(1)），選擇權用 Black-76 定價。
「無關」的世界：市場知道明天的真實 σ（再加 10% 的波動溢酬），所以最近走多少對報酬沒有資訊；
「植入」的世界：市場一律用長期平均的 σ 定價，最近走得多的時候跨式就是便宜的。
"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from core import option_backtest as ob
from core import weekly_straddle_study as st
from core.iv_monitor import black76

ROOT = Path(__file__).resolve().parent.parent


def _next_month(c: str) -> str:
    y, m = int(c[:4]), int(c[4:])
    return f"{y + (m == 12)}{m % 12 + 1:02d}"


def world(seed=0, n_days=1500, kind="null", vrp=1.1, start=date(2013, 1, 7)):
    rng = np.random.default_rng(seed)
    days, d = [], start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    mu, phi, eta = np.log(0.012), 0.97, 0.12
    lv = np.full(n_days, mu)
    for i in range(1, n_days):
        lv[i] = mu + phi * (lv[i - 1] - mu) + eta * rng.standard_normal()
    sig = np.exp(lv)                                                     # sig[i]：第 i-1 天收盤到第 i 天收盤的波動
    S = np.empty(n_days)
    S[0] = 10000.0
    for i in range(1, n_days):
        S[i] = S[i - 1] * np.exp(sig[i] * rng.standard_normal() - sig[i] ** 2 / 2)
    tx = {}
    for i, x in enumerate(days):
        c = f"{x.year}{x.month:02d}"
        tx[x.isoformat()] = st.TxDay(c, {c: float(S[i]), _next_month(c): float(S[i])})
    rows, fsp = [], {}
    for i in range(25, n_days):
        x = days[i]
        if x.weekday() != 2:
            continue
        wk = (x.day - 1) // 7 + 1
        code = f"{x.year}{x.month:02d}" if wk == 3 else f"{x.year}{x.month:02d}W{wk}"
        fsp[code] = (x, float(S[i]))
        j, F = i - 1, float(S[i - 1])
        s_mkt = (sig[i] * vrp if kind != "planted" else np.exp(mu) * vrp) * np.sqrt(252)
        k0 = round(F / 50) * 50
        for m in range(-10, 11):
            k = k0 + 50 * m
            for right in ("C", "P"):
                px = max(round(black76(F, k, 1 / 252, s_mkt, 0.0, right == "C"), 1), 0.1)
                rows.append({"date": days[j].isoformat(), "expiry": code, "kind": "M" if wk == 3 else "W", "session": "G", "right": right,
                             "strike": float(k), "F": F, "settle": px, "close": None, "bid": max(px - 1, 0.1), "ask": px + 1})
    return st.group_chain(rows), fsp, tx


# ── 讀檔 ─────────────────────────────────────────────────────────

TX_HEADER = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def tx_row(d, code, close, vol, sess="一般"):
    return f"{d},TX,{code}  ,100,120,90,{close},1,1%,{vol},101,10,100,101,200,50,,{sess},"


def test_load_tx_keeps_day_session_month_contracts_and_picks_the_most_active(tmp_path):
    lines = [TX_HEADER, tx_row("2026/10/01", "202610", 100, 5000), tx_row("2026/10/01", "202611", 104, 100),
             tx_row("2026/10/01", "202610", 999, 99999, "盤後"),                     # 夜盤：不算
             tx_row("2026/10/01", "202610/202611", 3, 9999), tx_row("2026/10/01", "202610W1", 500, 99999),   # 價差、週合約：不算
             tx_row("2026/10/02", "202610", 110, 10), tx_row("2026/10/02", "202611", 115, 20),
             "2026/10/05,TX,202610  ,-,-,-,-,-,-,0,-,-,-,-,-,-,,一般,"]                 # 沒成交：不算
    (tmp_path / "tx_2026_10.csv").write_bytes(("\n".join(lines) + "\n").encode("cp950"))
    tx = st.load_tx(tmp_path)
    assert tx == {"2026-10-01": st.TxDay("202610", {"202610": 100.0, "202611": 104.0}), "2026-10-02": st.TxDay("202611", {"202610": 110.0, "202611": 115.0})}
    assert st.daily_moves(tx)[1] == [None, 11.0]                                       # 換月：11 月合約 104 → 115


def test_describe_splits_by_kind_cpi_quintiles_and_recent_points():
    def tr(i, ret, ratio, kind="W", cpi=False):
        e = (date(2024, 1, 3) + timedelta(days=7 * i)).isoformat()
        return {"expiry": e, "kind": kind, "ret": ret, "ret_sens": ret - 0.01, "ratio": ratio, "fsp": 110.0, "F": 100.0, "mid": 20.0,
                "payoff": 20.0 * (1 + ret), "cost": 20.0, "asks": 2, "cpi": cpi}
    trades = [tr(i, -0.5 + 0.01 * i, 0.5 + 0.01 * i, kind="M" if i % 4 == 0 else "W", cpi=i % 10 == 0) for i in range(120)]
    d = st.describe(trades)
    assert d["all"]["n"] == 120 and d["by_kind"]["M"]["n"] == 30 and d["by_kind"]["W"]["n"] == 90 and d["by_kind"]["F"] == {"n": 0}
    assert d["cpi"]["yes"]["n"] == 12 and d["cpi"]["no"]["n"] == 108
    assert [q["n"] for q in d["quintiles"]] == [24] * 5 and d["quintiles"][0]["mean"] < d["quintiles"][-1]["mean"]
    sig = [t for t in trades if t["ratio"] > 1]
    assert d["signal"]["n"] == len(sig) == 69 and d["all"]["share_signal"] == pytest.approx(69 / 120)
    assert d["realized_over_implied"]["median"] == pytest.approx(0.5) and d["asks_used"] == 1.0
    rec = [t for t in trades if (date.fromisoformat(trades[-1]["expiry"]) - date.fromisoformat(t["expiry"])).days < st.RECENT_DAYS]
    assert d["recent"]["n"] == len(rec) < 120 and d["recent"]["pts_net"] == pytest.approx(np.mean([t["payoff"] - t["cost"] for t in rec]))
    assert d["all"]["mean_capped"] == pytest.approx(d["all"]["mean"]) and d["by_year"]["2024"]["n"] == 52
    big = st.describe([tr(i, 9.0 if i == 0 else -0.5, 1.5) for i in range(20)])
    assert big["all"]["mean_capped"] == pytest.approx((5.0 - 0.5 * 19) / 20)              # 截在 +500%
    assert st.describe([tr(0, 0.1, 1.2, cpi=None)])["cpi"] is None


# ── 規則 ──────────────────────────────────────────────────────────

def small(moves=None, entry_rows=True):
    """22 個交易日（含 1 個假日空檔），最後一天到期；每天收盤照 moves 走。"""
    days, d = [], date(2026, 9, 1)
    while len(days) < 22:
        if d.weekday() < 5 and d != date(2026, 9, 28):
            days.append(d)
        d += timedelta(days=1)
    moves = moves or [10.0 * (i % 3 + 1) for i in range(22)]
    c, tx = 46000.0, {}
    for i, x in enumerate(days):
        c += moves[i] if i else 0
        tx[x.isoformat()] = st.TxDay("202610", {"202610": c, "202611": c + 5})
    e, j = days[-1], days[-2]
    q = {(46000.0, "C"): ob.Q(110.0, None, 100.0, 112.0), (46000.0, "P"): ob.Q(95.0, None, 90.0, 300.0),     # 賣權收盤賣價離譜 → 結算價 × 1.02
         (46100.0, "C"): ob.Q(60.0, None, 59.0, 61.0), (46100.0, "P"): ob.Q(145.0, None, 144.0, 146.0),
         (45900.0, "C"): ob.Q(170.0, None, 169.0, 171.0)}                                                    # 45900 沒有賣權：不能當價平
    chain = {(j.isoformat(), "202610W5"): {"F": 46050.0, "kind": "W", "q": q}} if entry_rows else {}
    return chain, {"202610W5": (e, 46321.0)}, tx, days


def test_one_trade_by_hand():
    chain, fsp, tx, days = small()
    trades, skipped = st.build_trades(chain, fsp, tx, cpi=[])
    assert skipped == {"no_entry_rows": 0, "no_atm": 0, "no_rm20": 0} and len(trades) == 1
    t = trades[0]
    assert t["entry"] == days[-2].isoformat() and t["expiry"] == days[-1].isoformat() and t["kind"] == "W"
    assert t["K"] == 46000.0                                                          # 46000 與 46100 一樣近（F＝46050）→ 取低的
    cost = (112.0 + 0.4 + 0.112) + (95.0 * 1.02 + 0.4 + 0.001 * 95.0 * 1.02)
    assert t["cost"] == pytest.approx(cost) and t["asks"] == 1
    assert t["payoff"] == pytest.approx(321.0 * 0.999) and t["ret"] == pytest.approx(321.0 * 0.999 / cost - 1)
    mv = [10.0 * (i % 3 + 1) for i in range(22)]
    assert t["rm20"] == pytest.approx(np.mean(mv[1:21])) and t["ratio"] == pytest.approx(np.mean(mv[1:21]) / cost)   # 進場日（第 21 天）含
    assert t["ratio_peek"] == pytest.approx(np.mean(mv[2:22]) / cost)                # 金絲雀＝往後挪一天
    assert t["cost_sens"] == pytest.approx(sum(ob.buy_cost(s * 1.015) for s in (110.0, 95.0))) and t["cpi"] is False


def test_skips_are_counted():
    chain, fsp, tx, days = small(entry_rows=False)
    assert st.build_trades(chain, fsp, tx)[1]["no_entry_rows"] == 1
    chain, fsp, tx, days = small()
    del chain[(days[-2].isoformat(), "202610W5")]["q"][(46000.0, "P")], chain[(days[-2].isoformat(), "202610W5")]["q"][(46100.0, "P")]
    assert st.build_trades(chain, fsp, tx)[1]["no_atm"] == 1
    chain, fsp, tx, days = small()
    del tx[days[1].isoformat()]                                                       # 進場日以前不到 20 個交易日
    assert st.build_trades(chain, fsp, tx)[1]["no_rm20"] == 1
    assert st.build_trades(*small()[:3], first="2027-01-01")[0] == []


def test_entry_is_the_previous_trading_day_not_the_previous_calendar_day():
    chain, fsp, tx, days = small()
    e = date.fromisoformat(days[-1].isoformat())
    fsp = {"202610W5": (e + timedelta(days=3), 46321.0)}                              # 到期日之前三天都沒交易（TX 日曆沒有）→ 進場仍是最後一個交易日
    chain = {(days[-1].isoformat(), "202610W5"): next(iter(chain.values()))}
    t = st.build_trades(chain, fsp, tx)[0][0]
    assert t["entry"] == days[-1].isoformat() and t["ratio_peek"] is None              # 到期日不在日曆裡：金絲雀算不出來


def test_settlement_prices_are_required_for_the_atm_strike():
    g = {"F": 100.0, "q": {(100.0, "C"): ob.Q(None, 5.0, None, None), (100.0, "P"): ob.Q(4.0, None, None, None),
                           (110.0, "C"): ob.Q(1.0, None, None, None), (110.0, "P"): ob.Q(11.0, None, None, None)}}
    assert st.pick_atm(g) == 110.0                                                    # 100 只有收盤價、沒有結算價
    assert st.pick_atm({"F": 100.0, "q": {}}) is None


def test_moves_use_the_same_contract_across_a_roll():
    tx = {"2026-10-20": st.TxDay("202610", {"202610": 100.0, "202611": 130.0}),
          "2026-10-21": st.TxDay("202611", {"202611": 128.0}),
          "2026-10-22": st.TxDay("202611", {"202611": 140.0}),
          "2026-10-23": st.TxDay("202612", {"202612": 150.0})}
    assert st.daily_moves(tx) == (sorted(tx), [None, 2.0, 12.0, None])               # 換月那天用新合約自己的前一天（130 → 128），不是 100 → 128
    assert st.rm([None, 2.0, 12.0], 2, n=2) == 7.0 and st.rm([None, 2.0, 12.0], 1, n=2) is None and st.rm([1.0], 5, n=1) is None


def test_cpi_window_is_entry_close_to_expiry_settlement():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tpe = ZoneInfo("Asia/Taipei")
    at = lambda y, m, d, h, mi: datetime(y, m, d, h, mi, tzinfo=tpe).timestamp()     # noqa: E731
    assert st.in_window([at(2026, 10, 13, 20, 30)], "2026-10-13", "2026-10-14")
    assert st.in_window([at(2026, 10, 14, 13, 30)], "2026-10-13", "2026-10-14")
    assert not st.in_window([at(2026, 10, 13, 13, 45), at(2026, 10, 14, 13, 31)], "2026-10-13", "2026-10-14")


# ── 統計 ──────────────────────────────────────────────────────────

def test_newey_west_reduces_to_the_iid_formula_without_lags_and_widens_with_autocorrelation():
    rng = np.random.default_rng(1)
    y = rng.standard_normal(400) + 0.3
    r = st.mean_test(y, lag=0)
    assert r["t"] == pytest.approx(y.mean() / (y.std() / np.sqrt(400)))
    x = rng.standard_normal(400)
    yy = 0.5 * x + rng.standard_normal(400)
    s = st.slope_test(yy, x, lag=0)
    xc = x - x.mean()
    e = yy - yy.mean() - s["b"] * xc
    assert s["b"] == pytest.approx(np.polyfit(x, yy, 1)[0]) and s["t"] == pytest.approx(s["b"] / (np.sqrt(np.sum(xc ** 2 * e ** 2)) / np.sum(xc ** 2)))
    ar = np.cumsum(rng.standard_normal(400)) * 0.05 + 0.1                             # 自相關很強：NW 的 t 應該比較小
    assert abs(st.mean_test(ar, lag=10)["t"]) < abs(st.mean_test(ar, lag=0)["t"])
    assert st.mean_test(np.array([1.0, 2.0]))["t"] is None and st.slope_test(np.ones(5), np.ones(5))["t"] is None


def test_t2_needs_enough_trades_and_both_halves_positive():
    mk = lambda rets, ratio=2.0: [{"ret": r, "ratio": ratio} for r in rets]          # noqa: E731
    good = mk([0.5, 0.4, 0.6, 0.45] * 20)
    assert st.t2(good)["pass"] and st.t2(good)["n"] == 80
    assert not st.t2(mk([0.5, 0.4, 0.6, 0.45] * 10))["pass"]                          # 40 筆 < 60
    assert not st.t2(mk([-0.1] * 40 + [1.5] * 40))["pass"]                            # 前半是負的
    assert st.t2(mk([0.5] * 80, ratio=0.9))["n"] == 0 and st.t2(mk([0.5] * 80, ratio=1.0))["n"] == 0    # 門檻是 > 1


def test_t1_needs_both_halves():
    rng = np.random.default_rng(3)
    x = np.exp(rng.standard_normal(200))
    tr = [{"ret": float(np.log(v) + 0.5 * rng.standard_normal()), "ratio": float(v)} for v in x]
    assert st.t1(tr)["pass"]
    flip = [{"ret": -t["ret"], "ratio": t["ratio"]} for t in tr[:100]] + tr[100:]      # 前半的關係反過來
    assert not st.t1(flip)["pass"]
    weak = [{"ret": -0.05 * np.log(t["ratio"]) + 0.5 * rng.standard_normal(), "ratio": t["ratio"]} for t in tr[:100]]
    strong = [{"ret": 3 * np.log(t["ratio"]) + 0.5 * rng.standard_normal(), "ratio": t["ratio"]} for t in tr[100:]]
    r = st.t1(weak + strong)                                                          # 整體很顯著，但前半是負的 → 不過
    assert r["t"] > 3 and r["b_first"] < 0 < r["b_second"] and not r["pass"]


def test_bonferroni_threshold_and_exact_newey_west():
    assert st.Z == pytest.approx(1.95996, abs=1e-4)                                   # 單尾 0.05 ÷ 2 個檢定
    u = np.array([1.0, -2.0, 3.0, 0.5])
    g0, g1, g2 = (u @ u) / 4, (u[1:] @ u[:-1]) / 4, (u[2:] @ u[:-2]) / 4
    assert st.nw_var(u, lag=2) == pytest.approx(g0 + 2 * (2 / 3) * g1 + 2 * (1 / 3) * g2)
    y = np.random.default_rng(12).standard_normal(80) * 0.5 + 0.1                    # t≈1.90：沒校正（1.645）會過、校正後不過
    r = st.t2([{"ret": float(v), "ratio": 2.0} for v in y])
    assert 1.645 < r["t"] < st.Z and r["first"] > 0 and r["second"] > 0 and not r["pass"]


# ── 合成世界：植入、無關、偷看 ──────────────────────────────────────

def test_planted_world_passes_both_tests():
    for seed in (1, 2):                                                              # 樣本大小跟真實資料差不多（約 600 筆）
        r = st.run(*world(seed=seed, n_days=3000, kind="planted", vrp=1.0))
        assert r["verdict"] == {"T1": "過關", "T2": "過關"}, (r["T1"], r["T2"])
        assert r["n"] > 550 and r["T2"]["n"] >= st.MIN_T2


def test_null_world_false_positive_rate_is_small():
    fp = 0
    sims = 120
    for s in range(sims):
        r = st.run(*world(seed=100 + s, n_days=900, kind="null"))
        fp += r["T1"]["pass"] or r["T2"]["pass"]
    assert fp / sims <= 0.06, fp
    fair = st.run(*world(seed=7, kind="null", vrp=1.0))                               # 定價公平：買方平均就是 −成本
    assert -0.15 < fair["describe"]["all"]["mean"] < 0.0 and not fair["T2"]["pass"]


def test_canary_sees_the_future_and_the_signal_does_not():
    chain, fsp, tx = world(seed=2, kind="null")
    r = st.run(chain, fsp, tx)
    assert r["canary"]["ok"] and r["canary"]["t"] > st.Z and not r["T1"]["pass"]
    full = st.build_trades(chain, fsp, tx)[0]
    for t in full[::25]:                                                              # 資料截到進場日、最後結算價亂改：訊號一樣
        cut = {d: v for d, v in tx.items() if d <= t["entry"]}
        again = st.build_trades(chain, {t["code"]: (fsp[t["code"]][0], fsp[t["code"]][1] * 1.05)}, cut)[0]
        assert len(again) == 1 and again[0]["ratio"] == pytest.approx(t["ratio"]) and again[0]["entry"] == t["entry"]
        assert again[0]["ret"] != pytest.approx(t["ret"])


def test_report_and_empty():
    r = st.run(*world(seed=4, kind="planted"), cpi=[])
    text = st.format_report(r)
    assert "T1" in text and "金絲雀" in text and "五分位" in text and "CPI" in text and "nan" not in text
    e = st.run({}, {}, {})
    assert e["verdict"] == {"T1": "沒有樣本", "T2": "沒有樣本"} and "0 筆" in st.format_report(e)


def test_not_imported_by_trading_modules():
    for d in ("strategies", "api", "core"):
        for p in (ROOT / d).rglob("*.py"):
            if p.name in ("weekly_straddle_study.py", "drift_timing_study.py", "night_reversal_study.py", "call_put_study.py", "intraday_momentum_study.py"):   # 其他離線研究借用檢定與資料定義（發現 72～75）
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            names = [x.name for n in ast.walk(tree) if isinstance(n, ast.Import) for x in n.names]
            names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [x.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for x in n.names]
            assert "weekly_straddle_study" not in " ".join(names), p
