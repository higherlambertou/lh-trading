"""口數試算（發現 51）：公式與手算一致、各上限的優先順序與標示、fail-closed、已佔用風險的分配、API。只顯示、不碰下單（有靜態檢查）。"""
from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

import api.routes_position as routes_position
from core import position_size as ps


def comp(equity=1_000_000, stop=50, contract="TMF", **kw):
    base = dict(risk_pct=0.5, slip=0.0, total_pct=100.0, open_twd=0.0, cap=None, qty=None, equity_age=None)
    base.update(kw)
    return ps.compute(equity, stop, contract, **base)


# ── 公式（需求文件的驗收範例）─────────────────────────────────────

def test_spec_examples_by_hand():
    r = comp(1_000_000, 50, "TMF")                                     # 5,000 ÷ (50×10) = 10 口
    assert r["lots"] == 10 and r["binding"] == "single" and r["reason"] is None and r["risk_per_lot"] == 500 and r["caps"][0]["budget_twd"] == 5000
    r = comp(1_000_000, 400, "TXF")                                    # 5,000 ÷ (400×200) = 0.0625 → 0 口，不進場
    assert r["lots"] == 0 and r["reason"] == "below_one"
    assert comp(1_000_000, 50, "MXF")["lots"] == 2 and comp(1_000_000, 25, "TXF")["lots"] == 1
    one = comp(52_000, 30, "TMF", risk_pct=1.0)                                      # 剛好 1 口：不是『不足 1 口』
    assert one["lots"] == 1 and one["reason"] is None and "need_equity_single" not in one and "max_stop_for_one" not in one


def test_slip_buffer_widens_the_stop():
    assert comp(52_000, 30, "TMF", risk_pct=1.0, slip=0)["lots"] == 1             # 520 ÷ 300
    r = comp(52_000, 30, "TMF", risk_pct=1.0, slip=2)                              # 520 ÷ 320
    assert r["lots"] == 1 and r["eff_stop"] == 32 and r["risk_per_lot"] == 320
    assert comp(52_000, 50, "TMF", risk_pct=1.0, slip=3)["lots"] == 0 and comp(52_000, 50, "TMF", risk_pct=1.0, slip=0)["lots"] == 1     # 520 ÷ 530 → 0；520 ÷ 500 → 1


def test_exactly_on_the_limit_counts_and_one_over_does_not():
    assert comp(100_000, 100, "TMF", risk_pct=1.0)["lots"] == 1                     # 1,000 ÷ 1,000 恰好 1 口
    assert comp(100_000, 101, "TMF", risk_pct=1.0)["lots"] == 0
    assert comp(300_000, 100, "TMF", risk_pct=1.0)["lots"] == 3


# ── 多層上限 ─────────────────────────────────────────────────────

def test_total_risk_cap_subtracts_open_risk_and_is_reported_as_binding():
    r = comp(1_000_000, 50, "TMF", total_pct=2.0, open_twd=19_000)                  # 總風險餘額 20,000−19,000＝1,000 → 2 口（單筆上限 10 口）
    assert r["lots"] == 2 and r["binding"] == "total" and r["caps"][1]["lots"] == 2 and r["caps"][1]["budget_twd"] == 1000
    r = comp(1_000_000, 50, "TMF", total_pct=2.0, open_twd=25_000)                  # 已經超過上限：餘額 0（不是負的）
    assert r["lots"] == 0 and r["caps"][1]["budget_twd"] == 0 and r["binding"] == "total" and r["reason"] == "below_one"


def test_lot_cap_and_priority_on_ties():
    r = comp(1_000_000, 50, "TMF", cap=3)
    assert r["lots"] == 3 and r["binding"] == "cap" and [c["name"] for c in r["caps"]] == ["single", "total", "cap"]
    r = comp(1_000_000, 50, "TMF", cap=10)                                          # 同值：單筆風險排前面
    assert r["lots"] == 10 and r["binding"] == "single"
    assert comp(1_000_000, 50, "TMF", total_pct=5.0)["binding"] == "single" and comp(1_000_000, 50, "TMF", total_pct=5.0)["lots"] == 10


def test_below_one_says_what_would_be_enough():
    r = comp(52_000, 100, "MXF", risk_pct=1.0)                                       # 每口 5,000 > 預算 520
    assert r["lots"] == 0 and r["need_equity_single"] == 500_000 and r["max_stop_for_one"] == 10     # 520 ÷ 50 = 10 點
    r2 = comp(52_000, 40, "TMF", risk_pct=1.0, slip=2, total_pct=1.0, open_twd=400)  # 總風險餘額 120 → 1 口要 ≤ 12−2 = 10 點
    assert r2["lots"] == 0 and r2["max_stop_for_one"] == 10


def test_qty_flags():
    r = comp(52_000, 30, "TMF", risk_pct=1.0, slip=2, qty=2)
    assert r["lots"] == 1 and r["qty_over"] is True and r["qty_over_by"] == ["single"] and r["qty_risk_twd"] == 640 and r["qty_risk_pct"] == pytest.approx(1.231, abs=0.001)
    ok = comp(52_000, 30, "TMF", risk_pct=1.0, slip=2, qty=1)
    assert ok["qty_over"] is False and ok["qty_over_by"] == []
    assert "qty_over" not in comp(52_000, 30, "TMF", qty=None)


# ── fail-closed ──────────────────────────────────────────────────

@pytest.mark.parametrize("equity", [None, 0, -5, float("nan"), float("inf"), True, "52000"])
def test_bad_equity_gives_no_lots(equity):
    r = comp(equity, 30, "TMF")
    assert r["lots"] is None and r["reason"] == "equity_missing"


@pytest.mark.parametrize("stop", [None, 0, -1, float("nan"), float("inf"), "30", False])
def test_bad_stop_gives_no_lots(stop):
    r = comp(52_000, stop, "TMF")
    assert r["lots"] is None and r["reason"] == "stop_missing" and r["equity"] == 52_000


def test_stale_equity_and_unknown_contract_give_no_lots():
    assert comp(52_000, 30, "TMF", equity_age=301)["reason"] == "equity_stale" and comp(52_000, 30, "TMF", equity_age=301)["lots"] is None
    assert comp(52_000, 30, "TMF", equity_age=300)["lots"] is not None
    r = comp(52_000, 30, "TXO")
    assert r["lots"] is None and r["reason"] == "contract_unknown"
    assert comp(52_000, 30, "tmf")["contract"] == "TMF"


def test_result_is_json_safe():
    import json
    for r in (comp(52_000, 30), comp(None, 30), comp(52_000, None), comp(52_000, 9999, "TXF"), comp(52_000, 30, qty=3)):
        json.dumps(r, allow_nan=False)


# ── 已佔用風險 ───────────────────────────────────────────────────

def pos(code, direction="Buy", q=1):
    return {"code": code, "direction": direction, "quantity": q}


def watch(contract="TMF", direction="Buy", sl=30, remaining=1, seen=True, **kw):
    return {"contract": contract, "direction": direction, "stop_loss_pts": sl, "remaining": remaining, "quantity": remaining, "seen": seen, "is_option": False, **kw}


def test_open_risk_counts_stop_distance_times_lots_with_slip():
    r = ps.open_risk([pos("TMFJ6", "Buy", 2)], [watch(sl=30, remaining=2)], slip=2)
    assert r["twd"] == (30 + 2) * 10 * 2 and r["covered_lots"] == 2 and r["uncovered"] == [] and r["options"] == []
    r = ps.open_risk([pos("MXFJ6", "Sell", 1), pos("TXFJ6", "Buy", 1)], [watch("MXF", "Sell", 20, 1), watch("TXF", "Buy", 10, 1)], slip=0)
    assert r["twd"] == 20 * 50 + 10 * 200


def test_open_risk_lists_what_has_no_stop_instead_of_guessing():
    r = ps.open_risk([pos("TMFJ6", "Buy", 3)], [watch(sl=30, remaining=1)], slip=0)
    assert r["twd"] == 300 and r["covered_lots"] == 1 and r["uncovered"] == [{"code": "TMFJ6", "direction": "Buy", "quantity": 2}]          # 3 口只有 1 口登記了停損
    assert ps.open_risk([pos("TMFJ6")], [], 0)["uncovered"][0]["quantity"] == 1                                                              # 沒有監看（策略單）
    for w in (watch(sl=0), watch(sl=None), watch(seen=False), watch(direction="Sell"), watch(contract="MXF")):                               # 停損 0／沒有／還沒成交／方向不同／合約不同：都不算保護
        r = ps.open_risk([pos("TMFJ6")], [w], 0)
        assert r["twd"] == 0 and r["uncovered"][0]["quantity"] == 1, w


def test_open_risk_two_watches_share_one_position_and_options_are_listed_not_counted():
    r = ps.open_risk([pos("TMFJ6", "Buy", 3)], [watch(sl=30, remaining=1), watch(sl=50, remaining=2)], slip=0)
    assert r["twd"] == 30 * 10 + 50 * 10 * 2 and r["covered_lots"] == 3 and r["uncovered"] == []
    r = ps.open_risk([pos("TXO22000J6", "Buy", 2), pos("TMFJ6", "Buy", 0), pos("", "Buy", 1)], [watch(is_option=True, contract="TXO")], 0)
    assert r["twd"] == 0 and r["options"] == [{"code": "TXO22000J6", "direction": "Buy", "quantity": 2}] and r["uncovered"] == []


def test_open_risk_uses_remaining_lots_and_never_counts_more_than_the_position():
    r = ps.open_risk([pos("TMFJ6", "Buy", 3)], [watch(sl=30, remaining=1, quantity=3)], slip=0)         # 3 口單平掉 2 口後 remaining＝1
    assert r["twd"] == 300 and r["uncovered"][0]["quantity"] == 2
    r = ps.open_risk([pos("TMFJ6", "Buy", 1)], [watch(sl=30, remaining=3)], slip=0)                    # 監看的口數比部位多：只算部位有的
    assert r["twd"] == 300 and r["covered_lots"] == 1 and r["uncovered"] == []
    r = ps.open_risk([pos("TMFJ6", "Buy", 1), pos("TMFK6", "Buy", 1)], [watch(sl=30, remaining=2)], slip=0)   # 兩列部位共用一個監看
    assert r["twd"] == 600 and r["covered_lots"] == 2 and r["uncovered"] == []
    r = ps.open_risk([pos("TMFJ6", "Buy", 1), pos("TMFK6", "Buy", 1)], [watch(sl=30, remaining=1)], slip=0)
    assert r["twd"] == 300 and r["uncovered"] == [{"code": "TMFK6", "direction": "Buy", "quantity": 1}]
    r = ps.open_risk([pos("TMFJ6", "Buy", 1), pos("TMFK6", "Buy", 2)], [watch(sl=30, remaining=2)], slip=0)     # 監看被第一列用掉 1 口，第二列只剩 1 口可用
    assert r["twd"] == 600 and r["covered_lots"] == 2 and r["uncovered"] == [{"code": "TMFK6", "direction": "Buy", "quantity": 1}]
    assert ps.open_risk([pos("TXO22000J6", "Buy", 0)], [], 0)["options"] == []                          # 0 口的選擇權不列
    r = ps.open_risk([pos("TMFJ6", "Buy", 1)], [watch(is_option=True, contract="TMF", sl=30)], 0)       # 標成選擇權的監看不算期貨保護
    assert r["twd"] == 0 and r["uncovered"][0]["quantity"] == 1


def test_open_risk_does_not_mutate_inputs():
    ws = [watch(sl=30, remaining=2)]
    before = [dict(w) for w in ws]
    ps.open_risk([pos("TMFJ6", "Buy", 1)], ws, 0)
    assert ws == before


# ── 設定 ─────────────────────────────────────────────────────────

def test_config_defaults_and_bad_values(monkeypatch):
    for k in ("SIZE_SLIP_PTS", "SIZE_TOTAL_RISK_PCT", "SIZE_LOT_CAP", "SUGGEST_RISK_PCT"):
        monkeypatch.delenv(k, raising=False)
    assert ps.config() == {"risk_pct": 1.0, "slip_pts": 2.0, "total_risk_pct": 3.0, "lot_cap": None}
    for k, v, key, want in (("SIZE_SLIP_PTS", "-1", "slip_pts", 2.0), ("SIZE_SLIP_PTS", "abc", "slip_pts", 2.0), ("SIZE_SLIP_PTS", "inf", "slip_pts", 2.0),
                            ("SIZE_TOTAL_RISK_PCT", "0", "total_risk_pct", 3.0), ("SIZE_TOTAL_RISK_PCT", "nan", "total_risk_pct", 3.0), ("SIZE_TOTAL_RISK_PCT", "101", "total_risk_pct", 3.0),
                            ("SIZE_LOT_CAP", "0", "lot_cap", None), ("SIZE_LOT_CAP", "x", "lot_cap", None), ("SIZE_LOT_CAP", "-2", "lot_cap", None), ("SIZE_LOT_CAP", " 2 ", "lot_cap", 2)):
        monkeypatch.setenv(k, v)
        assert ps.config()[key] == want, (k, v)
        monkeypatch.delenv(k)
    monkeypatch.setenv("SIZE_SLIP_PTS", "0")
    monkeypatch.setenv("SIZE_TOTAL_RISK_PCT", "5.5")
    monkeypatch.setenv("SIZE_LOT_CAP", "4")
    monkeypatch.setenv("SUGGEST_RISK_PCT", "0.5")
    assert ps.config() == {"risk_pct": 0.5, "slip_pts": 0.0, "total_risk_pct": 5.5, "lot_cap": 4}


# ── API ──────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("SIMULATION", "true")
    for k in ("SIZE_SLIP_PTS", "SIZE_TOTAL_RISK_PCT", "SIZE_LOT_CAP", "SUGGEST_RISK_PCT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(routes_position._cache, "margin", {"equity": 52_000.0})
    monkeypatch.setitem(routes_position._cache, "updated_at", time.time())
    monkeypatch.setitem(routes_position._cache, "positions", [])
    from api import routes_risk
    monkeypatch.setattr(routes_risk.manual_monitor, "list_watches", lambda: [])
    return TestClient(main.app), routes_risk


def test_api_returns_lots_from_the_cached_equity(client):
    c, _ = client
    r = c.get("/api/risk/size?contract=TMF&stop_pts=30&qty=2").json()
    assert r["lots"] == 1 and r["equity"] == 52_000 and r["risk_per_lot"] == 320 and r["qty_over"] is True and r["open_risk"]["twd"] == 0 and r["open_risk"]["pct"] == 0
    assert c.get("/api/risk/size?contract=tmf&stop_pts=10").json()["lots"] == 4                    # (10+2)×10＝120；520 ÷ 120 ＝ 4.3 → 4；合約不分大小寫
    assert c.get("/api/risk/size?contract=TXO&stop_pts=10").status_code == 422
    assert c.get("/api/risk/size?contract=TMF&stop_pts=10&qty=0").status_code == 422


def test_api_fails_closed_without_equity_or_stop_or_fresh_data(client, monkeypatch):
    c, _ = client
    assert c.get("/api/risk/size?contract=TMF").json()["reason"] == "stop_missing"
    monkeypatch.setitem(routes_position._cache, "updated_at", time.time() - 1000)
    r = c.get("/api/risk/size?contract=TMF&stop_pts=30").json()
    assert r["lots"] is None and r["reason"] == "equity_stale"
    monkeypatch.setitem(routes_position._cache, "margin", None)
    r = c.get("/api/risk/size?contract=TMF&stop_pts=30").json()
    assert r["lots"] is None and r["reason"] == "equity_missing"
    assert c.get("/api/risk/size?contract=TMF&stop_pts=nan").json()["reason"] in ("stop_missing", "equity_missing")


def test_api_subtracts_open_risk_from_watches_and_positions(client, monkeypatch):
    c, rr = client
    monkeypatch.setitem(routes_position._cache, "positions", [pos("TMFJ6", "Buy", 4), pos("TXO22000J6", "Buy", 1)])
    monkeypatch.setattr(rr.manual_monitor, "list_watches", lambda: [watch(sl=40, remaining=3)])
    r = c.get("/api/risk/size?contract=TMF&stop_pts=10").json()
    assert r["open_risk"]["twd"] == (40 + 2) * 10 * 3 and r["open_risk"]["covered_lots"] == 3 and r["open_risk"]["pct"] == pytest.approx(2.423, abs=0.001)
    assert r["open_risk"]["uncovered"] == [{"code": "TMFJ6", "direction": "Buy", "quantity": 1}] and len(r["open_risk"]["options"]) == 1
    assert r["caps"][1]["budget_twd"] == 300                                                         # 1,560 − 1,260 = 300 → 300 ÷ 120 = 2 口
    assert r["caps"][1]["lots"] == 2 and r["lots"] == 2 and r["binding"] == "total"


# ── 只顯示、不碰下單 ─────────────────────────────────────────────

def test_position_size_is_not_used_by_order_or_strategy_paths():
    root = Path(ps.__file__).resolve().parent.parent
    paths = list((root / "strategies").glob("*.py")) + [root / "core" / n for n in ("broker.py", "manual_monitor.py", "protection.py", "quote_hub.py", "shioaji_worker.py")] \
        + [root / "api" / n for n in ("routes_order.py", "routes_strategy.py")]
    for p in paths:
        assert "position_size" not in p.read_text(encoding="utf-8"), p
