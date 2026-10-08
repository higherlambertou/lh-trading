"""破產機率驗證（core/ruin.py、api/routes_risk.py）與成交紀錄的 FIFO 配對（TradeLog.round_trips）。"""
from __future__ import annotations

import time

import pytest

import api.routes_position as routes_position
from core.ruin import (
    bootstrap_ruin, breakeven_win_rate, build_report, expectancy, expected_longest_losing_streak,
    history_stats, losing_streak, lundberg_bound, simulate_ruin, symmetric_ruin,
)
from core.trade_log import TradeLog


# ── 公式（對照《破產機率驗證_需求文件.md》的範例）──────────────────

@pytest.mark.parametrize("p,units,doc", [(0.55, 10, 0.137), (0.55, 5, 0.37), (0.52, 10, 0.448)])
def test_symmetric_formula_matches_the_documents_examples(p, units, doc):
    assert symmetric_ruin(p, units) == pytest.approx(doc, abs=0.005)       # 文件寫「約」；精確值 13.44% / 36.7% / 44.9%


def test_symmetric_formula_edges():
    assert symmetric_ruin(0.5, 10) == 1.0 and symmetric_ruin(0.4, 10) == 1.0     # 沒優勢 → 遲早破產
    assert symmetric_ruin(1.0, 10) == 0.0
    assert symmetric_ruin(0.55, 20) < symmetric_ruin(0.55, 10)                  # 本金越多越安全


def test_monte_carlo_agrees_with_the_formula_when_symmetric():
    mc = simulate_ruin(1000, 0.55, 100, 100, n_trades=3000, n_paths=6000, seed=1)["ruin_prob"]
    assert mc == pytest.approx(symmetric_ruin(0.55, 10), abs=0.03)


def test_lundberg_bound_is_an_upper_bound_for_asymmetric_bets():
    p, a, b, c = 0.80, 20, 60, 600                                              # 期望值 +4/筆
    assert expectancy(p, a, b) > 0
    bound = lundberg_bound(p, a, b, c)
    mc = simulate_ruin(c, p, a, b, n_trades=4000, n_paths=6000, seed=2)["ruin_prob"]
    assert 0 < bound < 1 and mc <= bound + 0.01


def test_nonpositive_expectancy_means_certain_eventual_ruin():
    assert lundberg_bound(0.60, 20, 60, 5000) == 1.0                            # 期望值 -12/筆
    assert lundberg_bound(0.75, 20, 60, 5000) == 1.0                            # 剛好兩平
    assert lundberg_bound(0.9, 20, 60, 0) == 1.0                                # 沒有可虧本金
    assert simulate_ruin(5000, 0.60, 20, 60, n_trades=2000, n_paths=1000)["ruin_prob"] > 0.95


def test_more_capital_or_higher_win_rate_never_increases_ruin():
    r = lambda cap, wr: simulate_ruin(cap, wr, 20, 60, n_trades=1000, n_paths=3000, seed=3)["ruin_prob"]
    assert r(1000, 0.80) >= r(2000, 0.80) >= r(4000, 0.80)
    assert r(2000, 0.76) >= r(2000, 0.80) >= r(2000, 0.85)


def test_breakeven_and_expectancy_include_costs():
    assert breakeven_win_rate(20, 60) == pytest.approx(0.75)                    # 賺賠比 1:3 → 要 75% 才不虧
    assert breakeven_win_rate(20, 60, cost=2) == pytest.approx(0.775)
    assert expectancy(0.65, 20, 60) == pytest.approx(-8.0)
    assert expectancy(0.80, 20, 60, cost=2) == pytest.approx(+2.0)


def test_bootstrap_uses_the_real_pnl_distribution():
    assert bootstrap_ruin(1000, [100, 200, 50], n_trades=200, n_paths=500)["ruin_prob"] == 0.0     # 全是賺的
    assert bootstrap_ruin(1000, [-300, 100], n_trades=200, n_paths=500)["ruin_prob"] > 0.99        # 期望值為負


def test_losing_streaks_and_history_stats():
    assert losing_streak([1, -1, -2, -3, 4, -1]) == (3, -6.0)
    assert losing_streak([5, 6]) == (0, 0.0)
    # 對照模擬（1500 條路徑平均）：公平硬幣 1024 次 9.30、勝率 65%/1000 筆 6.31、勝率 75%/1000 筆 4.70
    assert expected_longest_losing_streak(0.5, 1024) == pytest.approx(9.3, abs=0.2)
    assert expected_longest_losing_streak(0.65, 1000) == pytest.approx(6.3, abs=0.2)
    assert expected_longest_losing_streak(0.75, 1000) == pytest.approx(4.7, abs=0.2)
    assert expected_longest_losing_streak(1.0, 1000) == 0.0
    st = history_stats([200, -600, 200, 200, -600, -100])
    assert st["n"] == 6 and st["win_rate"] == pytest.approx(0.5) and st["longest_losing_streak"] == 2
    assert st["avg_win"] == 200 and st["avg_loss"] == pytest.approx(433.3, abs=0.1) and st["worst_trade"] == -600
    assert history_stats([]) is None


def test_report_shape_determinism_and_capacity():
    kw = dict(cost=20.0, n_trades=300, n_paths=800, win_rates=(0.6, 0.8))
    a = build_report(51482, 0.65, 200, 600, **kw)
    b = build_report(51482, 0.65, 200, 600, **kw)
    assert a == b                                                               # 固定 seed → 可重現
    assert a["per_trade"]["breakeven_win_rate"] == pytest.approx(0.775)
    assert a["capacity"]["affordable_losses"] == 51482 // 620
    assert set(a["ruin"]) == {"0.5x", "1x", "2x"} and [g["win_rate"] for g in a["grid"]] == [0.6, 0.8]
    assert a["ruin"]["1x"]["formula"] is None                                   # 賺賠不對稱：不給對稱公式
    sym = build_report(1000, 0.55, 100, 100, n_trades=300, n_paths=800, win_rates=(0.55,))
    assert sym["ruin"]["1x"]["formula"] == pytest.approx(0.1344, abs=1e-3)
    assert a["inputs"]["source"] == "parameters"
    assert build_report(1000, 0.5, 100, 100, n_trades=50, n_paths=500, win_rates=(0.5,), pnls=[100, -100])["inputs"]["source"] == "history"


# ── API ──────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from api import routes_risk
    monkeypatch.setenv("SIMULATION", "true")
    monkeypatch.setitem(routes_position._cache, "margin", {"equity": 51482.0})
    t = TradeLog(tmp_path / "risk.db")
    monkeypatch.setattr(routes_risk, "trade_log", t)
    return TestClient(main.app), routes_risk, t


def test_api_defaults_capital_to_account_equity(client):
    c, _, _ = client
    r = c.get("/api/risk/ruin?trades=200&paths=500&tp_pts=20&sl_pts=60&win_rate=0.65&cost_pts=2").json()
    assert r["inputs"]["capital"] == 51482.0 and r["defaults"]["capital_from_equity"] is True
    assert r["per_trade"]["win"] == 200 and r["per_trade"]["loss"] == 600 and r["per_trade"]["cost"] == 20
    assert r["per_trade"]["breakeven_win_rate"] == pytest.approx(0.775)
    assert r["history"] is None and r["history_used"] is False


def test_api_scales_by_quantity_and_point_value_and_validates(client):
    c, _, _ = client
    r = c.get("/api/risk/ruin?capital=100000&qty=2&point_value=50&trades=100&paths=500").json()
    assert r["per_trade"]["win"] == 20 * 50 * 2 and r["inputs"]["capital"] == 100000
    assert c.get("/api/risk/ruin?win_rate=1.5").status_code == 422
    assert c.get("/api/risk/ruin?tp_pts=0").status_code == 422
    assert c.get("/api/risk/ruin?trades=999999").status_code == 422


def test_api_needs_a_capital_source(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setitem(routes_position._cache, "margin", None)
    assert c.get("/api/risk/ruin?trades=100&paths=500").status_code == 422
    assert c.get("/api/risk/ruin?capital=50000&trades=100&paths=500").status_code == 200


def test_api_history_is_used_only_with_enough_trades(client, monkeypatch):
    c, routes_risk, t = client
    few = c.get("/api/risk/ruin?use_history=true&trades=100&paths=500").json()
    assert few["history_used"] is False and few["history_note"] and "30" in few["history_note"]

    trips = [{"pnl": 200.0 if i % 4 else -600.0} for i in range(40)]
    monkeypatch.setattr(t, "round_trips", lambda mode=None, since_ts=None: {"trips": trips, "open": [], "n_fills": 80})
    many = c.get("/api/risk/ruin?use_history=true&trades=100&paths=500").json()
    assert many["history_used"] is True and many["inputs"]["source"] == "history"
    assert many["history"]["n"] == 40 and many["history"]["longest_losing_streak"] == 1
    assert c.get("/api/risk/trips").json()["stats"]["n"] == 40


# ── FIFO 配對：成交紀錄 → 完整交易 ────────────────────────────────

@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATION", "true")
    t = TradeLog(tmp_path / "rt.db")
    t.start()
    yield t
    t.stop()


def fill(t: TradeLog, tid: str, action: str, qty: int, price: float, *, strategy="scalp", reason="entry",
         contract="TMF", state=None):
    t.set_market_tag({"market_state": state} if state else {})
    with t.context(strategy=strategy, reason=reason):
        t.record_order(contract=contract, action=action, qty=qty, order_type="IOC", trade_id=tid, status="PendingSubmit")
    t.record_event({"state": "FuturesDeal", "trade_id": tid, "price": price, "quantity": qty})
    time.sleep(0.003)                                           # 讓成交時間嚴格遞增


def drain(t: TradeLog):
    t.stop()
    t.start()


def test_round_trips_pair_fills_fifo_and_compute_real_pnl(tlog):
    fill(tlog, "a1", "Buy", 1, 100, state="REVERT")
    fill(tlog, "a2", "Sell", 1, 120, reason="tp")                              # 多單停利 +20 點 = +200 元
    fill(tlog, "b1", "Sell", 1, 130)
    fill(tlog, "b2", "Buy", 1, 140, reason="sl")                               # 空單停損 -10 點 = -100 元
    fill(tlog, "c1", "Buy", 2, 100)
    fill(tlog, "c2", "Sell", 1, 105, reason="tp")                              # FIFO：同一筆 2 口拆成兩筆交易
    fill(tlog, "c3", "Sell", 1, 95, reason="sl")
    fill(tlog, "o1", "Buy", 1, 800, contract="TXO49800J6")                      # 選擇權：略過
    drain(tlog)
    res = tlog.round_trips(mode="sim")
    got = [(t["side"], t["qty"], t["entry"], t["exit"], t["pts"], t["pnl"], t["reason"]) for t in res["trips"]]
    assert got == [(1, 1, 100, 120, 20.0, 200.0, "tp"), (-1, 1, 130, 140, -10.0, -100.0, "sl"),
                   (1, 1, 100, 105, 5.0, 50.0, "tp"), (1, 1, 100, 95, -5.0, -50.0, "sl")]
    assert res["trips"][0]["market_state"] == "REVERT" and res["trips"][0]["strategy"] == "scalp"
    assert res["open"] == [] and res["n_fills"] == 7


def test_round_trips_handle_reversal_and_leave_the_remainder_open(tlog):
    fill(tlog, "r1", "Buy", 1, 100)
    fill(tlog, "r2", "Sell", 2, 110)                                            # 先平多（+10 點），多出的 1 口變成空單
    drain(tlog)
    res = tlog.round_trips(mode="sim")
    assert [(t["pts"], t["pnl"]) for t in res["trips"]] == [(10.0, 100.0)]
    assert res["open"] == [{"mode": "sim", "contract": "TMF", "side": -1, "qty": 1, "price": 110.0}]


def test_round_trips_ignore_unfilled_orders_and_other_modes(tlog, monkeypatch):
    with tlog.context(strategy="scalp"):
        tlog.record_order(contract="TMF", action="Buy", qty=1, order_type="IOC", trade_id="u1", status="PendingSubmit")
    monkeypatch.setenv("SIMULATION", "false")
    fill(tlog, "l1", "Buy", 1, 100)
    fill(tlog, "l2", "Sell", 1, 101)
    monkeypatch.setenv("SIMULATION", "true")
    drain(tlog)
    assert tlog.round_trips(mode="sim")["trips"] == []                          # 沒成交的單、別的 mode 都不算
    assert len(tlog.round_trips(mode="live")["trips"]) == 1
