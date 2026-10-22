"""臺指選擇權的到期序列（發現 70）：月選＋週三週選＋週五週選的分類、標籤、排序、已到期的丟掉、worker 從合約清單整理、API。"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from core import option_series as osr

# 2026-10-22 永豐合約清單裡真的有的序列（~/.shioaji/contracts-1.5.8-OPT-TW.parquet）
REAL = [("TX1", "202611", "2026/11/04", 270), ("TX4", "202610", "2026/10/28", 344), ("TXO", "202611", "2026/11/18", 388),
        ("TXO", "202612", "2026/12/16", 510), ("TXO", "202701", "2027/01/20", 286), ("TXO", "202703", "2027/03/17", 282),
        ("TXO", "202706", "2027/06/16", 234), ("TXY", "202610", "2026/10/23", 374), ("TXZ", "202610", "2026/10/30", 280)]
TODAY = date(2026, 10, 22)


def rows(data=REAL):
    return [{"category": c, "delivery_month": m, "delivery_date": d, "n": n} for c, m, d, n in data]


def test_kind_of():
    assert osr.kind_of("TXO") == "monthly"
    assert [osr.kind_of(c) for c in ("TX1", "TX2", "TX4", "TX5")] == ["weekly_wed"] * 4
    assert [osr.kind_of(c) for c in ("TXU", "TXV", "TXW", "TXX", "TXY", "TXZ")] == ["weekly_fri"] * 6
    assert osr.kind_of("TEO") == "other" and osr.kind_of("") == "other"


def test_parse_date_and_label():
    assert osr.parse_date("2026/10/23") == osr.parse_date("2026-10-23") == osr.parse_date("20261023") == date(2026, 10, 23)
    assert osr.parse_date("") is None and osr.parse_date("2026/13/40") is None
    assert osr.label("TXY", "202610", date(2026, 10, 23), TODAY) == "10/23（五）週選・明天到期"
    assert osr.label("TXO", "202611", date(2026, 11, 18), TODAY) == "11/18（三）月選・剩 27 天"
    assert osr.label("TX4", "202610", date(2026, 10, 22), TODAY) == "10/22（四）週選・今天到期"


def test_build_sorts_by_expiry_and_drops_expired():
    s = osr.build(rows() + [{"category": "TX2", "delivery_month": "202610", "delivery_date": "2026/10/14", "n": 1},     # 已到期
                            {"category": "TXO", "delivery_month": "", "delivery_date": "2026/12/16"}], TODAY)     # 不完整
    assert [(x["category"], x["delivery_date"]) for x in s] == [
        ("TXY", "2026-10-23"), ("TX4", "2026-10-28"), ("TXZ", "2026-10-30"), ("TX1", "2026-11-04"), ("TXO", "2026-11-18"),
        ("TXO", "2026-12-16"), ("TXO", "2027-01-20"), ("TXO", "2027-03-17"), ("TXO", "2027-06-16")]
    first = s[0]
    assert first == {"category": "TXY", "delivery_month": "202610", "delivery_date": "2026-10-23", "kind": "weekly_fri", "kind_text": "週五週選",
                     "days": 1, "label": "10/23（五）週選・明天到期", "n": 374}
    same_day = osr.build(rows([("TX5", "202612", "2026/12/16", 1), ("TXO", "202612", "2026/12/16", 2)]), TODAY)
    assert [x["category"] for x in same_day] == ["TXO", "TX5"]                         # 同一天：月選排前面


def test_worker_collects_rows_from_the_contract_list():
    from core.shioaji_worker import option_series_rows
    c = lambda m, d: SimpleNamespace(delivery_month=m, delivery_date=d)                 # noqa: E731

    class Boom:
        def __iter__(self):
            raise RuntimeError("壞掉的類別")
    opts = SimpleNamespace(TXO=[c("202611", "2026/11/18")] * 3 + [c("202612", "2026/12/16")], TXY=[c("202610", "2026/10/23")] * 2, TX2=Boom())
    got = option_series_rows(SimpleNamespace(Contracts=SimpleNamespace(Options=opts)))
    assert got == [{"category": "TXO", "delivery_month": "202611", "delivery_date": "2026/11/18", "n": 3},
                   {"category": "TXO", "delivery_month": "202612", "delivery_date": "2026/12/16", "n": 1},
                   {"category": "TXY", "delivery_month": "202610", "delivery_date": "2026/10/23", "n": 2}]
    assert option_series_rows(SimpleNamespace()) == [] and option_series_rows(SimpleNamespace(Contracts=SimpleNamespace(Options=None))) == []


def test_api_route(monkeypatch):
    from fastapi import HTTPException
    from api import routes_order as ro

    async def fake():
        return rows()
    monkeypatch.setattr(ro.broker, "option_series", fake)
    out = asyncio.run(ro.option_series())
    assert out[0]["category"] in ("TXY", "TX4", "TXZ", "TX1", "TXO") and all(x["days"] >= 0 for x in out)

    async def slow():
        raise asyncio.TimeoutError
    monkeypatch.setattr(ro.broker, "option_series", slow)
    with pytest.raises(HTTPException) as e:
        asyncio.run(ro.option_series())
    assert e.value.status_code == 503
