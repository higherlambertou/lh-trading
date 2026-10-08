"""core/shioaji_worker.py：委託回報的解析。

背景（2026-10-08，update.md 發現 21）：shioaji 1.5.x 的委託回報是 Rust 實作的 OrderEventDict，**不是 dict 的子類**；
舊的解析用 `msg if isinstance(msg, dict) else {}`，整個回報被當成空的——trade_id／價格／口數／失敗代碼全拿不到，
scalp 因此認不出自己的成交與取消：掛單逾時就再進場一次，一分鐘內送了 15 次單。"""
from __future__ import annotations

import logging
import queue
from types import SimpleNamespace

import pytest

from api.routes_order import _format_trades
from core.shioaji_worker import _extract_trade, extract_order_event, make_order_callback, to_mapping


class DictLike:
    """照 shioaji 1.5.3 的 OrderEventDict：不是 dict 的子類，只有這些方法。"""

    def __init__(self, d):
        self._d = dict(d)

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def keys(self):
        return list(self._d)

    def values(self):
        return list(self._d.values())

    def items(self):
        return list(self._d.items())

    def __contains__(self, k):
        return k in self._d

    def __len__(self):
        return len(self._d)

    def __iter__(self):
        return iter(self._d)


class State:
    def __init__(self, name):
        self.name = name


def deal(**over):
    d = {"trade_id": "5d1bf173", "seqno": "000008", "ordno": "kY00J", "exchange_seq": "", "broker_id": "F002000",
         "account_id": "0000000", "action": "Buy", "code": "TMFJ6", "price": 48981.0, "quantity": 1,
         "market_type": "Night", "combo": False, "ts": 1.0}
    d.update(over)
    return d


def order(op_type="New", op_code="00", op_msg="", tid="5d1bf173"):
    return {"operation": {"op_type": op_type, "op_code": op_code, "op_msg": op_msg},
            "order": {"id": tid, "seqno": "000008", "ordno": "kY00J", "action": "Buy", "price": 48981.0, "quantity": 1},
            "status": {"id": tid, "exchange_ts": 1.0, "modified_price": 0.0, "cancel_quantity": 0,
                       "order_quantity": 1, "web_id": "137"},
            "contract": {"security_type": "FUT", "code": "TMFJ6"}}


def deep_wrap(x):
    return DictLike({k: deep_wrap(v) for k, v in x.items()}) if isinstance(x, dict) else x


def attr_nested(x):
    return DictLike({k: (SimpleNamespace(**v) if isinstance(v, dict) else v) for k, v in x.items()})


SHAPES = {
    "dict": lambda d: d,                    # 舊版 SDK
    "dictlike_top": lambda d: DictLike(d),  # 1.5.x：頂層是 OrderEventDict，巢狀是一般 dict
    "dictlike_deep": deep_wrap,             # 巢狀也是 dict-like
    "attr_nested": attr_nested,             # 巢狀是純屬性物件
}
shape = pytest.mark.parametrize("wrap", SHAPES.values(), ids=SHAPES.keys())


# ── 回歸：1.5.x 的事件不是 dict ───────────────────────────────────

def test_the_sdk_event_type_is_not_a_dict_so_the_old_parse_saw_nothing():
    msg = DictLike(deal())
    assert not isinstance(msg, dict)
    old_parse = msg if isinstance(msg, dict) else {}
    assert old_parse == {}                                     # 舊程式看到的：全空
    assert extract_order_event(State("FuturesDeal"), msg)["trade_id"] == "5d1bf173"


# ── 成交（FuturesDeal）────────────────────────────────────────────

@shape
def test_deal_event_carries_trade_id_price_and_quantity(wrap):
    ev = extract_order_event(State("FuturesDeal"), wrap(deal(price=48981.0, quantity=2)))
    assert ev["state"] == "FuturesDeal" and ev["trade_id"] == "5d1bf173"
    assert ev["price"] == 48981.0 and ev["quantity"] == 2


def test_deal_trade_id_falls_back_to_seqno_then_nested_ids():
    assert extract_order_event(State("FuturesDeal"), DictLike(deal(trade_id="")))["trade_id"] == "000008"
    only_nested = {"price": 1.0, "quantity": 1, "status": {"id": "from-status"}}
    assert extract_order_event(State("FuturesDeal"), DictLike(only_nested))["trade_id"] == "from-status"


# ── 委託（FuturesOrder）：id 在 status.id，結果在 operation ──────────

@shape
def test_order_event_reads_the_id_from_status_and_the_result_from_operation(wrap):
    ev = extract_order_event(State("FuturesOrder"), wrap(order()))
    assert ev["state"] == "FuturesOrder" and ev["trade_id"] == "5d1bf173"
    assert (ev["op_type"], ev["op_code"], ev["op_msg"]) == ("New", "00", "")


@shape
def test_a_rejected_order_exposes_the_broker_reason(wrap):
    ev = extract_order_event(State("FuturesOrder"), wrap(order(op_code="88", op_msg="保證金不足")))
    assert ev["trade_id"] == "5d1bf173" and ev["op_code"] == "88" and ev["op_msg"] == "保證金不足"


@shape
def test_cancel_confirmation_is_recognisable(wrap):
    ev = extract_order_event(State("FuturesOrder"), wrap(order(op_type="Cancel")))
    assert ev["op_type"] == "Cancel" and ev["op_code"] == "00"


def test_order_id_falls_back_to_order_id_when_status_has_none():
    msg = order()
    msg["status"] = {"exchange_ts": 1.0}
    assert extract_order_event(State("FuturesOrder"), DictLike(msg))["trade_id"] == "5d1bf173"


# ── 看不懂的東西不能丟例外 ─────────────────────────────────────────

@pytest.mark.parametrize("junk", [None, object(), 42, "text", [], DictLike({})])
def test_unrecognised_shapes_give_empty_fields_without_raising(junk):
    ev = extract_order_event(State("FuturesOrder"), junk)
    assert ev["type"] == "order_event" and ev["trade_id"] == "" and ev["price"] == 0.0 and ev["quantity"] == 0


def test_state_name_accepts_enum_like_or_plain_string():
    assert extract_order_event(State("FuturesDeal"), None)["state"] == "FuturesDeal"
    assert extract_order_event("FuturesOrder", None)["state"] == "FuturesOrder"


def test_to_mapping_handles_each_shape():
    d = {"a": 1}
    assert to_mapping(d) is d
    assert to_mapping(DictLike(d)) == d
    assert to_mapping(SimpleNamespace(dict=lambda: d)) == d     # MappingMixin.dict()
    assert to_mapping(None) == {} and to_mapping(object()) == {}


# ── callback（解析壞掉不能再靜默）──────────────────────────────────

def test_callback_queues_the_parsed_event(caplog):
    q = queue.Queue()
    cb = make_order_callback(q)
    with caplog.at_level(logging.INFO, logger="core.shioaji_worker"):
        cb(State("FuturesDeal"), DictLike(deal()))
    ev = q.get_nowait()
    assert ev["trade_id"] == "5d1bf173" and ev["price"] == 48981.0
    assert "前 3 筆" in caplog.text                              # 重啟後第一批回報會留下一行，方便核對解析正確


def test_callback_warns_loudly_when_the_id_cannot_be_extracted(caplog):
    q = queue.Queue()
    cb = make_order_callback(q)
    with caplog.at_level(logging.WARNING, logger="core.shioaji_worker"):
        for _ in range(8):
            cb(State("FuturesOrder"), object())
    assert q.qsize() == 8                                      # 事件照送，只是警告
    warns = [r for r in caplog.records if "萃取不到 trade_id" in r.getMessage()]
    assert len(warns) == 5                                     # 只警告前 5 次，不洗版
    assert "object" in warns[0].getMessage()                   # 記下 msg 的型別，下次一眼看出是哪種格式


def test_callback_exceptions_are_logged_not_swallowed_and_never_raised(caplog):
    class Boom:
        def put_nowait(self, _):
            raise RuntimeError("queue broken")

    cb = make_order_callback(Boom())
    with caplog.at_level(logging.ERROR, logger="core.shioaji_worker"):
        for _ in range(8):
            cb(State("FuturesDeal"), DictLike(deal()))       # 不能把例外丟回 SDK 的 callback 執行緒
    assert len([r for r in caplog.records if "委託回報處理失敗" in r.getMessage()]) == 5


# ── 失敗原因要看得到 ───────────────────────────────────────────────

def fake_trade(status="Failed", msg="保證金不足", code="TMFJ6"):
    return SimpleNamespace(
        status=SimpleNamespace(id="abc12345", status=SimpleNamespace(value=status), deals=[], deal_quantity=0,
                               order_datetime=None, msg=msg),
        order=SimpleNamespace(action=SimpleNamespace(value="Buy"), price=48982.0, quantity=1),
        contract=SimpleNamespace(code=code))


def test_trade_list_carries_the_broker_failure_message():
    row = _format_trades([_extract_trade(fake_trade())])[0]
    assert row["status"] == "Failed" and row["msg"] == "保證金不足"
    assert _format_trades([_extract_trade(fake_trade("Filled", None))])[0]["msg"] == ""


def test_trade_dict_carries_the_contract_code_for_the_entry_gate():
    """scalp 的進場把關只看 TMF 的未成交委託，要分得出是哪個合約。"""
    assert _extract_trade(fake_trade(code="TMFJ6"))["code"] == "TMFJ6"
    assert _extract_trade(fake_trade(code="MXFJ6"))["code"] == "MXFJ6"
    no_contract = fake_trade()
    del no_contract.contract
    assert _extract_trade(no_contract)["code"] == ""                              # 取不到就留空（把關會保守地當成 TMF）
