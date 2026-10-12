"""手動下單的「當沖」（倉別 DayTrade）：期交所保證金減半只給小台 MXF、大台 TXF 的日盤，微台 TMF 與夜盤不適用。

後端要擋的是「一定沒有好處、或券商行為不明」的下單（TMF 標當沖、日盤以外標當沖、亂寫的倉別）；
標了當沖的單，停損停利監控的平倉單也要沿用當沖（日盤內），日盤以外退回 Auto 免得平倉單被拒絕。策略一律 Auto，不支援。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes_order as ro
import core.daytrade as dt
import core.trade_log as tl
from core.broker import BrokerClient, broker
from core.daytrade import TAIPEI, check_octype, close_octype, in_day_session
from core.manual_monitor import ManualOrderMonitor, ManualWatch
from core.quote_hub import quote_hub

ROOT = Path(__file__).resolve().parent.parent
MON_DAY = datetime(2026, 10, 12, 10, 0, tzinfo=TAIPEI)             # 週一 10:00（日盤）
MON_NIGHT = datetime(2026, 10, 12, 21, 0, tzinfo=TAIPEI)           # 週一 21:00（夜盤）


# ── 日盤判斷 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 10, 12, 8, 44, 59, tzinfo=TAIPEI), False),
    (datetime(2026, 10, 12, 8, 45, 0, tzinfo=TAIPEI), True),
    (datetime(2026, 10, 12, 13, 44, 59, tzinfo=TAIPEI), True),
    (datetime(2026, 10, 12, 13, 45, 0, tzinfo=TAIPEI), False),
    (datetime(2026, 10, 12, 21, 0, tzinfo=TAIPEI), False),                   # 夜盤
    (datetime(2026, 10, 10, 10, 0, tzinfo=TAIPEI), False),                   # 週六
    (datetime(2026, 10, 11, 10, 0, tzinfo=TAIPEI), False),                   # 週日
    (datetime(2026, 10, 16, 10, 0, tzinfo=TAIPEI), True),                    # 週五
])
def test_day_session_boundaries(when, expected):
    assert in_day_session(when) is expected


def test_day_session_uses_taipei_time_whatever_the_clock_timezone():
    utc = datetime(2026, 10, 12, 1, 0, tzinfo=dt.timezone.utc)               # = 台北 09:00
    assert in_day_session(utc) is True
    assert in_day_session(datetime(2026, 10, 12, 6, 0, tzinfo=dt.timezone.utc)) is False        # = 台北 14:00


# ── 倉別檢查 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("octype,contract,when,expect_error", [
    ("Auto", "TMF", MON_DAY, None), ("Auto", "TMF", MON_NIGHT, None), ("Auto", "MXF", MON_NIGHT, None),
    ("New", "TMF", MON_DAY, None), ("Cover", "MXF", MON_NIGHT, None),
    ("DayTrade", "MXF", MON_DAY, None), ("DayTrade", "TXF", MON_DAY, None), ("DayTrade", "mxf", MON_DAY, None),
    ("DayTrade", "TMF", MON_DAY, "微台"),                    # 期交所不給 TMF 減半
    ("DayTrade", "MXF", MON_NIGHT, "日盤"),                  # 夜盤不適用
    ("DayTrade", "TMF", MON_NIGHT, "微台"),
    ("auto", "TMF", MON_DAY, "octype"), ("Foo", "MXF", MON_DAY, "octype"), ("", "MXF", MON_DAY, "octype"),
])
def test_check_octype(octype, contract, when, expect_error):
    msg = check_octype(octype, contract, when)
    assert (msg is None) if expect_error is None else (msg is not None and expect_error in msg), msg


@pytest.mark.parametrize("octype,when,expected", [
    ("DayTrade", MON_DAY, "DayTrade"), ("DayTrade", MON_NIGHT, "Auto"),     # 日盤以外退回 Auto，平倉單才不會被拒絕
    ("Auto", MON_DAY, "Auto"), ("Auto", MON_NIGHT, "Auto"), ("Cover", MON_DAY, "Auto"),
])
def test_close_octype(octype, when, expected):
    assert close_octype(octype, when) == expected


# ── 下單 API ───────────────────────────────────────────────────────

class Rig:
    def __init__(self, monkeypatch, now=MON_DAY):
        self.placed: list[dict] = []
        self.watches: list[dict] = []
        rig = self

        async def place_order(**kw):
            rig.placed.append(kw)
            return {"trade_id": f"T{len(rig.placed)}", "status": "PendingSubmit"}

        monkeypatch.setattr(ro.broker, "place_order", place_order)
        monkeypatch.setattr(ro.quote_hub, "last_price_by_prefix", lambda c: 48980.0)
        monkeypatch.setattr(ro.manual_monitor, "add", lambda **k: rig.watches.append(k) or "w1")
        monkeypatch.setattr(dt, "taipei_now", lambda: now)
        app = FastAPI()
        app.include_router(ro.router, prefix="/api/order")
        self.client = TestClient(app)

    def post(self, **over):
        body = {"contract": "MXF", "action": "Buy", "quantity": 1, "price_type": "MKT", "order_type": "IOC", "octype": "DayTrade",
                "stop_loss_pts": 0, "take_profit_pts": 0}
        body.update(over)
        return self.client.post("/api/order/place", json=body)


def test_a_day_trade_order_on_a_small_taiex_future_goes_out_flagged(monkeypatch):
    rig = Rig(monkeypatch)
    r = rig.post(contract="MXF")
    assert r.status_code == 200, r.text
    assert [(k["contract_code"], k["octype"]) for k in rig.placed] == [("MXF", "DayTrade")]
    assert rig.post(contract="TXF").status_code == 200 and rig.placed[-1]["octype"] == "DayTrade"


def test_day_trade_is_refused_for_the_micro_contract_and_nothing_is_sent(monkeypatch):
    rig = Rig(monkeypatch)
    r = rig.post(contract="TMF")
    assert r.status_code == 400 and "微台" in r.json()["detail"]
    assert rig.placed == [] and rig.watches == []


def test_day_trade_is_refused_outside_the_day_session_and_nothing_is_sent(monkeypatch):
    rig = Rig(monkeypatch, now=MON_NIGHT)
    r = rig.post(contract="MXF")
    assert r.status_code == 400 and "日盤" in r.json()["detail"]
    assert rig.placed == [] and rig.watches == []


def test_an_unknown_octype_is_rejected_before_it_reaches_the_broker(monkeypatch):
    rig = Rig(monkeypatch)
    r = rig.post(octype="Foo")
    assert r.status_code == 422 and rig.placed == []


def test_ordinary_orders_are_unchanged_day_or_night(monkeypatch):
    """沒勾當沖的單（含微台、夜盤）跟以前完全一樣：倉別 Auto，不受任何新檢查影響。"""
    for now in (MON_DAY, MON_NIGHT):
        rig = Rig(monkeypatch, now=now)
        assert rig.post(contract="TMF", octype="Auto").status_code == 200
        assert rig.post(contract="MXF", octype="Auto").status_code == 200
        assert [k["octype"] for k in rig.placed] == ["Auto", "Auto"]
    rig = Rig(monkeypatch)
    body = {"contract": "TMF", "action": "Buy", "quantity": 1, "price_type": "MKT", "order_type": "IOC"}
    assert rig.client.post("/api/order/place", json=body).status_code == 200 and rig.placed[-1]["octype"] == "Auto"     # 沒帶 octype 的舊客戶端


def test_the_stop_loss_watch_inherits_the_day_trade_flag(monkeypatch):
    rig = Rig(monkeypatch)
    assert rig.post(contract="MXF", stop_loss_pts=30).status_code == 200
    assert rig.post(contract="MXF", octype="Auto", stop_loss_pts=30).status_code == 200
    assert rig.post(contract="TMF", octype="Auto", stop_loss_pts=30).status_code == 200
    assert [w["octype"] for w in rig.watches] == ["DayTrade", "Auto", "Auto"]


def test_the_octype_reaches_the_trade_log(monkeypatch):
    """成交紀錄（orders.octype）要看得出這張單是不是當沖——日後對帳用。"""
    sent: list = []

    async def fake_acall(self, method, **kw):
        sent.append((method, kw))
        return {"trade_id": "T1", "status": "PendingSubmit"}

    monkeypatch.setattr(BrokerClient, "_acall", fake_acall)
    monkeypatch.setattr(ro.quote_hub, "last_price_by_prefix", lambda c: 48980.0)
    monkeypatch.setattr(ro.manual_monitor, "add", lambda **k: "w1")
    monkeypatch.setattr(dt, "taipei_now", lambda: MON_DAY)
    monkeypatch.setattr(tl.trade_log, "enabled", True)
    while not tl.trade_log._q.empty():
        tl.trade_log._q.get_nowait()
    app = FastAPI()
    app.include_router(ro.router, prefix="/api/order")
    r = TestClient(app).post("/api/order/place", json={"contract": "MXF", "action": "Buy", "quantity": 1, "price_type": "MKT",
                                                       "order_type": "IOC", "octype": "DayTrade"})
    assert r.status_code == 200 and sent[0][1]["octype"] == "DayTrade"
    kind, row = tl.trade_log._q.get_nowait()
    assert kind == "order" and row[tl._ORDER_COLS.index("octype")] == "DayTrade" and row[tl._ORDER_COLS.index("contract")] == "MXF"


# ── 停損停利監控的平倉單 ────────────────────────────────────────────

class MonRig:
    def __init__(self, monkeypatch, now):
        self.sent: list[dict] = []
        rig = self

        async def place_order(**kw):
            rig.sent.append(kw)
            return {"trade_id": f"F{len(rig.sent)}", "status": "PendingSubmit"}

        monkeypatch.setattr(broker, "place_order", place_order)
        monkeypatch.setattr(dt, "taipei_now", lambda: now)
        monkeypatch.setitem(quote_hub._last_price, "MXFJ6", 85.0)
        self.mm = ManualOrderMonitor()
        self.mm._clock = lambda: 1000.0

    def stop_out(self, octype="Auto", contract="MXF"):
        w = ManualWatch(id="f", contract=contract, direction=1, quantity=1, entry_price=100.0, stop_loss_pts=10, take_profit_pts=0,
                        match_code=contract, octype=octype)
        self.mm._watches["f"] = w
        p = {"code": f"{contract}J6", "direction": "Buy", "quantity": 1, "price": 100.0, "last_price": 85.0}
        asyncio.run(self.mm._check(w, [p], {}, {}))
        return self.sent[-1]


def test_the_stop_loss_close_order_of_a_day_trade_is_also_a_day_trade(monkeypatch):
    kw = MonRig(monkeypatch, MON_DAY).stop_out("DayTrade")
    assert (kw["action"], kw["price_type"], kw["order_type"], kw["octype"]) == ("Sell", "MKT", "IOC", "DayTrade")


def test_the_close_order_falls_back_to_auto_outside_the_day_session(monkeypatch):
    """萬一日盤以外還有當沖倉位的停損觸發：用 Auto 平倉，不要讓平倉單被拒絕而失去停損保護。"""
    assert MonRig(monkeypatch, MON_NIGHT).stop_out("DayTrade")["octype"] == "Auto"


def test_an_ordinary_watch_closes_with_auto_as_before(monkeypatch):
    assert MonRig(monkeypatch, MON_DAY).stop_out("Auto", contract="TMF")["octype"] == "Auto"
    assert MonRig(monkeypatch, MON_NIGHT).stop_out("Auto", contract="TMF")["octype"] == "Auto"


def test_a_watch_defaults_to_auto_and_exposes_the_flag():
    mm = ManualOrderMonitor()
    mm.add(contract="MXF", direction=1, quantity=1, entry_price=100.0, stop_loss_pts=10, take_profit_pts=0)
    mm.add(contract="MXF", direction=1, quantity=1, entry_price=100.0, stop_loss_pts=10, take_profit_pts=0, octype="DayTrade")
    assert [w["octype"] for w in mm.list_watches()] == ["Auto", "DayTrade"]


# ── 範圍 ──────────────────────────────────────────────────────────

def test_strategies_never_send_day_trade_orders():
    """策略一律 Auto：當沖要在收盤前沖掉、沒沖掉會被強制沖銷，不能讓策略在沒人知道的情況下帶著這個旗標。"""
    offenders = [f.name for f in sorted((ROOT / "strategies").glob("*.py")) if "DayTrade" in f.read_text(encoding="utf-8")]
    assert offenders == []
    assert "DayTrade" not in (ROOT / "api" / "routes_strategy.py").read_text(encoding="utf-8")
