"""選擇權試算的資料層與 API：worker 整理鏈報價、broker 呼叫、歷史收盤、/api/options/{calc,close,chain}。

券商與行情都換成替身：鏈用 Black-76 造（帶買賣價差），到期日固定在「現在」之後 8.78 天，所以反推出來的 F 與 IV 看得出對不對。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes_options as ro
import core.options_calc as oc
from core.broker import broker
from core.shioaji_worker import _chain_quotes

NOW = datetime(2026, 10, 12, 18, 44)
EXPIRY = datetime(2026, 10, 21, 13, 30)
DAYS = (EXPIRY - NOW).total_seconds() / 86400
F0, IV0 = 49283.0, 20.3


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 12, 18, 44)


def raw_chain(spread_pct=0.03):
    q = oc.model_quotes(F0, IV0, DAYS, spread_pct=spread_pct, min_spread=1.0)
    return {"delivery_month": "202610", "delivery_date": "2026/10/21",
            "quotes": [{**x, "code": f"TXO{int(x['strike'])}{x['right']}", "volume": 1.0, "total_volume": 10.0, "ts": 1.79e9} for x in q]}


class Fake:
    """替身：計次的 broker.option_chain、固定的 TXF 現價、歷史收盤。"""

    def __init__(self, monkeypatch, chain=None, error=None, closes=None):
        self.calls = []
        self.chain, self.error = chain or raw_chain(), error

        async def option_chain(delivery_month, category="TXO", center=None, max_n=400):
            self.calls.append((delivery_month, category, center))
            if self.error:
                raise self.error
            return self.chain

        hist = closes or []

        async def fake_closes():
            return hist

        monkeypatch.setattr(broker, "option_chain", option_chain)
        monkeypatch.setattr(ro.quote_hub, "last_price_by_prefix", lambda p: 49283.0 if p == "TXF" else None)
        monkeypatch.setattr(ro, "datetime", FixedDatetime)
        monkeypatch.setattr(ro, "_closes", fake_closes)
        ro._chain_cache.clear()
        app = FastAPI()
        app.include_router(ro.router, prefix="/api/options")
        self.client = TestClient(app)

    def post(self, path, **body):
        return self.client.post(f"/api/options/{path}", json=body)


@pytest.fixture
def fake(monkeypatch):
    return Fake(monkeypatch)


def no_nan(resp):
    json.dumps(resp.json(), allow_nan=False)


# ── worker：整理鏈報價 ────────────────────────────────────────────

def contract(code, strike, right, month="202610"):
    return SimpleNamespace(code=code, strike_price=strike, option_right=right, delivery_month=month, delivery_date="2026/10/21")


def snap(code, **kw):
    base = dict(code=code, buy_price=10.0, sell_price=12.0, buy_volume=3, sell_volume=4, close=11.0, volume=2, total_volume=50, ts=1_791_800_000_000_000_000)
    return SimpleNamespace(**{**base, **kw})


def test_chain_quotes_pairs_contracts_with_snapshots_by_code():
    cs = [contract("A", 49300.0, "OptionRight.Call"), contract("B", "49300", SimpleNamespace(value="P")), contract("C", 49400, "CALL")]
    out = _chain_quotes(cs, [snap("B", buy_price=20.0, sell_price=22.0), snap("A"), snap("C", sell_price=0)])        # 快照的順序和合約不同
    assert [(q["code"], q["strike"], q["right"]) for q in out] == [("A", 49300.0, "C"), ("B", 49300.0, "P"), ("C", 49400.0, "C")]
    a = out[0]
    assert (a["bid"], a["ask"], a["close"], a["volume"], a["total_volume"], a["bid_vol"], a["ask_vol"]) == (10.0, 12.0, 11.0, 2.0, 50.0, 3.0, 4.0)
    assert a["ts"] == pytest.approx(1_791_800_000.0) and out[1]["bid"] == 20.0 and out[2]["ask"] == 0.0


def test_chain_quotes_skips_what_it_cannot_understand():
    cs = [contract("A", 49300.0, "OptionRight.Call"), contract("B", "abc", "CALL"), contract("C", 49500, "???"), contract("D", 49600, "PUT")]
    out = _chain_quotes(cs, [snap("A"), snap("B"), snap("C")])                    # D 沒有快照、B 履約價看不懂、C 買賣權看不懂
    assert [q["code"] for q in out] == ["A"]
    assert _chain_quotes([], []) == [] and _chain_quotes(cs, []) == []


def test_chain_quotes_missing_fields_become_zero():
    out = _chain_quotes([contract("A", 49300, "C")], [SimpleNamespace(code="A")])
    assert out[0]["bid"] == 0.0 and out[0]["ask"] == 0.0 and out[0]["ts"] == 0.0


# ── broker 與資料庫 ───────────────────────────────────────────────

def test_broker_option_chain_asks_the_worker_once_with_a_longer_timeout(monkeypatch):
    seen = []

    async def fake_acall(method, timeout=8.0, **kw):
        seen.append((method, timeout, kw))
        return {"quotes": []}

    monkeypatch.setattr(broker, "_acall", fake_acall)
    assert asyncio.run(broker.option_chain("202610", "TXO", center=49283.0, max_n=300)) == {"quotes": []}
    assert seen == [("option_chain", 25.0, {"delivery_month": "202610", "category": "TXO", "center": 49283.0, "max_n": 300})]


def test_day_closes_takes_the_last_bar_of_each_day(tmp_path):
    from core.market_store import MarketStore
    st = MarketStore(str(tmp_path / "m.db"))
    rows = []
    for d, closes in (("2026-03-02", [100, 101, 102]), ("2026-03-03", [110, 111]), ("2026-03-04", [120])):
        for i, c in enumerate(closes):
            rows.append({"date": d, "hhmm": 846 + i, "open": c, "high": c, "low": c, "close": float(c), "volume": 1})
    st.upsert_bars_1m("TXF", rows)
    st.upsert_bars_1m("MXF", [{"date": "2026-03-02", "hhmm": 900, "open": 1, "high": 1, "low": 1, "close": 999.0, "volume": 1}])
    assert st.day_closes("TXF") == [102.0, 111.0, 120.0] and st.day_closes("MXF") == [999.0] and st.day_closes("NOPE") == []


def test_parse_expiry_uses_the_contract_date_and_falls_back_to_the_third_wednesday():
    assert ro.parse_expiry("2026/10/21", "202610") == datetime(2026, 10, 21, 13, 30)
    assert ro.parse_expiry("2026-10-23", "202610") == datetime(2026, 10, 23, 13, 30)             # 週選：交割日不一定是第三個週三
    assert ro.parse_expiry("", "202610") == datetime(2026, 10, 21, 13, 30) and ro.parse_expiry("garbage", "202611") == datetime(2026, 11, 18, 13, 30)


# ── POST /calc ────────────────────────────────────────────────────

def test_calc_straddle_on_the_live_chain(fake):
    r = fake.post("calc", kind="straddle", delivery_month="202610", lots=2, budget_twd=300000)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "live" and d["delivery_month"] == "202610" and d["expiry"] == "2026-10-21T13:30"
    assert d["F"] == pytest.approx(F0, abs=1.0) and d["atm_iv"] == pytest.approx(IV0, abs=0.1) and d["days"] == pytest.approx(DAYS, abs=0.01)
    legs = d["legs"]
    assert [l["right"] for l in legs] == ["C", "P"] and legs[0]["strike"] == legs[1]["strike"]
    assert d["cost"]["pts"] == pytest.approx(legs[0]["ask"] + legs[1]["ask"], abs=0.1) and d["cost"]["lots"] == 2
    assert d["cost"]["max_lots"] == int(300000 // d["cost"]["per_lot_twd"]) and len(d["breakeven"]) == 2
    assert fake.calls == [("202610", "TXO", 49283.0)]                    # 一次查整條鏈，中心用 TXF 現價
    no_nan(r)


@pytest.mark.parametrize("body", [
    {"kind": "strangle", "select": "delta", "delta": 0.25},
    {"kind": "strangle", "select": "move", "k": 0.5},
    {"kind": "strangle", "select": "move", "k": 1.0, "move_pts": 600},
    {"kind": "call", "select": "delta", "delta": 0.4},
    {"kind": "put", "select": "move", "k": -0.2},
])
def test_calc_selection_modes(fake, body):
    r = fake.post("calc", delivery_month="202610", **body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["legs"]) == (2 if body["kind"] == "strangle" else 1) and d["cost"]["pts"] > 0
    if "move_pts" in body:
        assert d["expected_move"]["ref_source"] == "user" and d["expected_move"]["ref_pts"] == 600
        assert abs(d["legs"][0]["strike"] - (d["F"] + 600)) <= 50
    no_nan(r)


def test_calc_attaches_the_history_comparison_when_closes_exist(monkeypatch):
    import math
    import random
    rng = random.Random(2)
    closes = [30000.0]
    for _ in range(1500):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 0.011)))
    f = Fake(monkeypatch, closes=closes)
    d = f.post("calc", kind="straddle", delivery_month="202610").json()
    assert d["prob"]["hist"]["n"] > 1000 and d["prob"]["hist"]["h_days"] == oc.trading_days(DAYS / 365)
    assert Fake(monkeypatch).post("calc", kind="straddle", delivery_month="202610").json()["prob"]["hist"] is None


def test_calc_in_manual_mode_never_touches_the_broker(fake):
    r = fake.post("calc", kind="strangle", manual={"F": 49300, "iv": 21.0, "days": 9.0, "spread_pct": 0.02})
    assert r.status_code == 200, r.text
    d = r.json()
    assert fake.calls == [] and d["source"] == "manual" and d["delivery_month"] is None
    assert d["F"] == pytest.approx(49300, abs=1.0) and d["atm_iv"] == pytest.approx(21.0, abs=0.1) and "手動模式" in d["warnings"][0]
    no_nan(r)


def test_calc_input_validation(fake):
    assert fake.post("calc", kind="iron_condor", delivery_month="202610").status_code == 422
    assert fake.post("calc", kind="straddle").status_code == 400                              # 沒選到期月、也沒手動
    assert fake.post("calc", kind="straddle", delivery_month="202610", lots=0).status_code == 422
    assert fake.post("calc", kind="strangle", delivery_month="202610", delta=1.5).status_code == 422
    assert fake.post("calc", kind="straddle", manual={"F": -1, "iv": 20, "days": 5}).status_code == 422
    assert fake.post("calc", kind="straddle", manual={"F": 49000, "iv": 20, "days": 0}).status_code == 422
    assert fake.calls == []


def test_calc_reports_what_went_wrong_with_the_broker(monkeypatch):
    for err, code in ((asyncio.TimeoutError(), 503), (RuntimeError("Worker 尚未連線"), 503), (ValueError("找不到選擇權到期月: TXO 209912"), 404)):
        f = Fake(monkeypatch, error=err)
        r = f.post("calc", kind="straddle", delivery_month="202610")
        assert r.status_code == code and r.json()["detail"], (err, r.text)
    assert "手動模式" in Fake(monkeypatch, error=RuntimeError("x")).post("calc", kind="straddle", delivery_month="202610").json()["detail"]


def test_calc_with_no_usable_quotes_suggests_manual_mode(monkeypatch):
    dead = {"delivery_month": "202610", "delivery_date": "2026/10/21",
            "quotes": [{"strike": 49300.0, "right": "C", "bid": 0.0, "ask": 0.0, "close": 0.0, "ts": 0}, {"strike": 49300.0, "right": "P", "bid": 0.0, "ask": 0.0, "close": 0.0, "ts": 0}]}
    r = Fake(monkeypatch, chain=dead).post("calc", kind="straddle", delivery_month="202610")
    assert r.status_code == 422 and "手動模式" in r.json()["detail"]


def test_calc_after_expiry_is_refused(monkeypatch):
    stale = raw_chain()
    stale["delivery_date"] = "2026/10/01"
    r = Fake(monkeypatch, chain=stale).post("calc", kind="straddle", delivery_month="202610")
    assert r.status_code == 422 and "已到期" in r.json()["detail"]


def test_a_leg_that_cannot_be_bought_is_reported(monkeypatch):
    ch = raw_chain()
    for q in ch["quotes"]:
        if q["right"] == "P":
            q["ask"] = 0.0                                              # 賣權全部沒有賣價
    r = Fake(monkeypatch, chain=ch).post("calc", kind="put", delivery_month="202610", select="delta", delta=0.3)
    assert r.status_code == 422 and "賣權" in r.json()["detail"]


def test_chain_is_cached_for_a_few_seconds(fake, monkeypatch):
    for _ in range(3):
        assert fake.post("calc", kind="straddle", delivery_month="202610").status_code == 200
    assert len(fake.calls) == 1
    assert fake.post("calc", kind="straddle", delivery_month="202611").status_code == 200       # 別的月份不共用快取
    assert len(fake.calls) == 2
    t = [1_000_000.0]
    monkeypatch.setattr(ro.time, "time", lambda: t[0])
    ro._chain_cache.clear()
    fake.post("calc", kind="straddle", delivery_month="202610")
    n = len(fake.calls)
    t[0] += ro.CHAIN_TTL - 0.5
    fake.post("calc", kind="straddle", delivery_month="202610")
    assert len(fake.calls) == n
    t[0] += 1.0
    fake.post("calc", kind="straddle", delivery_month="202610")
    assert len(fake.calls) == n + 1


# ── GET /chain ────────────────────────────────────────────────────

def test_chain_summary(fake):
    r = fake.client.get("/api/options/chain", params={"delivery_month": "202610", "rows": 4})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["F"] == pytest.approx(F0, abs=1.0) and d["atm_iv_pct"] == pytest.approx(IV0, abs=0.1)
    assert d["em_sigma_pts"] == pytest.approx(oc.expected_move(F0, IV0 / 100, DAYS / 365), rel=0.01)
    assert d["straddle_pts"] == pytest.approx(d["em_sigma_pts"] * 0.7979, abs=0.2) and len(d["rows"]) == 8
    ks = [x["strike"] for x in d["rows"]]
    assert ks == sorted(ks) and min(ks) < d["F"] < max(ks) and set(d["rows"][0]["call"]) <= {"bid", "ask", "mid", "iv", "delta", "spread_pct"}
    no_nan(r)


# ── POST /close ───────────────────────────────────────────────────

def legs_body(c_entry=600.0, p_entry=600.0, k=49300):
    return [{"right": "C", "strike": k, "qty": 1, "entry": c_entry}, {"right": "P", "strike": k, "qty": 1, "entry": p_entry}]


def test_close_uses_live_quotes_for_each_leg(fake):
    r = fake.post("close", legs=legs_body(c_entry=640, p_entry=640), delivery_month="202610")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [a["id"] for a in d["actions"]] == ["hold", "close_0", "close_1", "all"] and d["source"] == "live"
    ch = oc.build_chain(raw_chain()["quotes"], DAYS / 365, hint=49283.0)
    row = next(x for x in ch["rows"] if x["strike"] == 49300.0)
    assert d["legs"][0]["bid"] == pytest.approx(row["call"]["bid"], abs=0.1) and d["legs"][1]["bid"] == pytest.approx(row["put"]["bid"], abs=0.1)
    close_put = next(a for a in d["actions"] if a["id"] == "close_1")
    assert close_put["realized_pts"] == pytest.approx(row["put"]["bid"] - 640, abs=0.1)
    assert close_put["net_basis_pts"] == pytest.approx(1280 - row["put"]["bid"], abs=0.1)
    assert not any("找不到報價" in w for w in d["warnings"])
    no_nan(r)


def test_close_typed_prices_win_over_the_chain(fake):
    legs = legs_body()
    legs[1].update(bid=100.0, ask=104.0)
    d = fake.post("close", legs=legs, delivery_month="202610").json()
    assert d["legs"][1]["bid"] == 100.0 and d["legs"][1]["ask"] == 104.0 and d["legs"][0]["bid"] != 100.0


def test_close_leg_not_on_the_chain_falls_back_to_the_model_with_a_warning(fake):
    d = fake.post("close", legs=legs_body(k=49325), delivery_month="202610").json()               # 49325 不是 50 點間距的履約價
    assert any("找不到報價" in w for w in d["warnings"]) and d["legs"][0]["mid"] > 0 and d["legs"][0]["bid"] == d["legs"][0]["ask"]


def test_close_in_manual_mode(fake):
    legs = legs_body(c_entry=50, p_entry=50, k=100)
    legs[0].update(bid=78.0, ask=82.0)
    legs[1].update(bid=20.0, ask=20.0)
    r = fake.post("close", legs=legs, manual={"F": 130, "iv": 25, "days": 20})
    assert r.status_code == 200, r.text
    d = r.json()
    a = next(x for x in d["actions"] if x["id"] == "close_1")
    assert fake.calls == [] and d["source"] == "manual" and a["realized_pts"] == -30.0 and a["net_basis_pts"] == 80.0
    assert [b["level"] for b in a["breakeven"]] == [180.0] and "手動模式" in d["warnings"][0]       # 需求文件的例子
    no_nan(r)


def test_close_validation(fake):
    assert fake.post("close", legs=[], delivery_month="202610").status_code == 422
    assert fake.post("close", legs=[{"right": "X", "strike": 100, "entry": 5}], delivery_month="202610").status_code == 422
    assert fake.post("close", legs=[{"right": "C", "strike": 100, "qty": 0, "entry": 5}], delivery_month="202610").status_code == 422
    assert fake.post("close", legs=[{"right": "C", "strike": 100, "entry": -1}], delivery_month="202610").status_code == 422
    assert fake.post("close", legs=legs_body()).status_code == 400
    too_many = [{"right": "C", "strike": 100 + i, "entry": 5} for i in range(9)]
    assert fake.post("close", legs=too_many, delivery_month="202610").status_code == 422
