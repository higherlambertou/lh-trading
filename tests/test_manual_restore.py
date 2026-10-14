"""手動停損停利監控的落地與重啟後恢復（core/manual_monitor.py，update.md 發現 18、32）。

修正前：ManualOrderMonitor 只存在記憶體，後端重啟（含 watchdog 在凍結後的自動重啟）就消失，持倉從此沒有任何保護。
修正後：登記／修改／移除都落地；重啟後和券商「現在」的部位對帳再恢復——絕不能把舊的停損掛到不相干的新部位上。"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.broker import broker
from core.manual_monitor import (
    ManualOrderMonitor, ManualWatch, _from_saved, _load_saved, _to_saved, default_state_path, reconcile,
)


def pos(code="TMFJ6", direction="Buy", qty=1, price=49000.0, last=49000.0):
    return {"code": code, "direction": direction, "quantity": qty, "price": price, "last_price": last}


def saved(id="a", match="TMF", direction=1, qty=1, entry=49000.0, sl=50, tp=0, created=1.0, **kw):
    d = {"id": id, "contract": match, "direction": direction, "quantity": qty, "entry_price": entry, "stop_loss_pts": sl, "take_profit_pts": tp,
         "order_id": "", "is_option": False, "match_code": match, "multiplier": 0.0, "exit_buffer_pts": 0.0, "delivery_month": "", "strike_price": 0,
         "option_right": "", "option_category": "TXO", "octype": "Auto", "created_at": created}
    d.update(kw)
    return d


# ── reconcile：重啟後只恢復「部位還在」的，而且口數不能超過部位 ────────────

def test_a_watch_is_restored_when_its_position_is_still_there():
    restored, dropped = reconcile([saved()], [pos()], set())
    assert dropped == [] and len(restored) == 1 and restored[0]["seen"] is True and restored[0]["quantity"] == 1 and restored[0]["id"] == "a"


def test_a_watch_is_dropped_when_the_position_is_gone():
    restored, dropped = reconcile([saved()], [], set())
    assert restored == [] and dropped[0]["reason"] == "部位已不存在" and dropped[0]["id"] == "a"


def test_the_restored_quantity_never_exceeds_the_position():
    (r,), _ = reconcile([saved(qty=3)], [pos(qty=2)], set())
    assert r["quantity"] == 2                                                              # 重啟期間手動平掉一口：只管剩下的


def test_several_watches_share_one_position_oldest_first():
    a, b = saved(id="a", qty=1, created=1.0), saved(id="b", qty=2, created=2.0)
    restored, dropped = reconcile([b, a], [pos(qty=2)], set())                              # 清單順序打亂，仍依建立時間分配
    assert [(r["id"], r["quantity"]) for r in restored] == [("a", 1), ("b", 1)] and dropped == []
    restored, dropped = reconcile([a, b], [pos(qty=1)], set())
    assert [(r["id"], r["quantity"]) for r in restored] == [("a", 1)] and dropped[0]["id"] == "b" and dropped[0]["reason"] == "部位口數已被其他監看用完"


def test_direction_and_contract_must_match():
    assert reconcile([saved(direction=1)], [pos(direction="Sell")], set())[0] == []        # 重啟期間反手了：舊的多單停損不能掛到空單上
    assert reconcile([saved(match="MXF")], [pos(code="TMFJ6")], set())[0] == []
    (r,), _ = reconcile([saved(match="TMF")], [pos(code="TMFK6")], set())                  # 期貨用前綴：換月也對得上
    assert r["seen"] is True


def test_option_watches_need_the_exact_code():
    w = saved(match="TXO49800J6", is_option=True)
    assert len(reconcile([w], [pos(code="TXO49800J6")], set())[0]) == 1
    assert reconcile([w], [pos(code="TXO49800K6")], set())[0] == []                        # 選擇權不能用前綴


def test_a_pending_entry_order_is_kept_only_if_it_is_still_live_at_the_broker():
    w = saved(order_id="T1", entry=0.0)
    (r,), _ = reconcile([w], [], {"T1"})
    assert r["seen"] is False and r["entry_price"] == 0.0                                  # 限價單還掛著：等它成交
    restored, dropped = reconcile([w], [], {"T2"})
    assert restored == [] and dropped[0]["reason"] == "部位已不存在"
    (r2,), _ = reconcile([w], [pos()], {"T1"})
    assert r2["seen"] is True                                                              # 重啟期間已經成交了：有部位就直接保護


def test_reconcile_does_not_touch_its_inputs_and_handles_empty():
    s, p = [saved()], [pos()]
    snap = (json.dumps(s), json.dumps(p))
    reconcile(s, p, set())
    assert (json.dumps(s), json.dumps(p)) == snap
    assert reconcile([], [pos()], set()) == ([], []) and reconcile([saved()], None, set())[0] == []


# ── 落地檔 ───────────────────────────────────────────────────────

@pytest.fixture
def mm(tmp_path):
    m = ManualOrderMonitor()
    m.state_path = tmp_path / "watches.json"
    return m


def read(mm):
    return json.loads(mm.state_path.read_text(encoding="utf-8"))


def add_tmf(mm, qty=1, entry=49000.0, sl=50, tp=0, **kw):
    return mm.add("TMF", 1, qty, entry, sl, tp, order_id=kw.pop("order_id", "T1"), **kw)


def test_add_update_and_remove_are_written_to_disk(mm):
    wid = add_tmf(mm, tp=80)
    d = read(mm)
    (w,) = d["watches"]
    assert d["version"] == 1 and w["id"] == wid and w["match_code"] == "TMF" and w["direction"] == 1 and w["quantity"] == 1
    assert w["entry_price"] == 49000.0 and w["stop_loss_pts"] == 50 and w["take_profit_pts"] == 80 and w["order_id"] == "T1" and w["created_at"] > 0
    assert mm.update(wid, stop_loss_pts=30, take_profit_pts=0)
    assert read(mm)["watches"][0]["stop_loss_pts"] == 30 and read(mm)["watches"][0]["take_profit_pts"] == 0
    mm.remove(wid)
    assert read(mm)["watches"] == []
    assert not (mm.state_path.parent / "watches.json.tmp").exists()


def test_removing_something_that_is_not_there_does_not_rewrite(mm):
    mm.remove("nope")
    assert not mm.state_path.exists()


def test_transient_close_tracking_is_not_saved_and_remaining_is(mm):
    wid = add_tmf(mm, qty=3)
    w = mm._watches[wid]
    w.close_order_id, w.close_attempts, w.close_gave_up, w.retry_after = "C1", 3, True, 99.0
    w.remaining = 2                                                                         # 已經平掉 1 口
    mm._save()
    (d,) = read(mm)["watches"]
    assert d["quantity"] == 2 and not any(k.startswith("close_") or k in ("retry_after", "seen", "waited", "remaining") for k in d)
    assert _from_saved(d).remaining == 2 and _from_saved(d).close_attempts == 0 and _from_saved(d).close_gave_up is False


def test_the_entry_price_learned_from_the_position_is_saved(mm):
    wid = add_tmf(mm, entry=0.0)                                                            # 市價單：成交價還不知道
    w = mm._watches[wid]
    asyncio.run(mm._check(w, [pos(price=49012.0)], {}))
    assert w.entry_price == 49012.0 and read(mm)["watches"][0]["entry_price"] == 49012.0


def test_a_save_failure_never_raises(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    m = ManualOrderMonitor()
    m.state_path = blocker / "w.json"                                                       # 上層是檔案：寫不進去
    wid = add_tmf(m)
    assert wid in m._watches and "存檔失敗" in caplog.text                                   # 監看照常登記、只留 ERROR


def test_without_a_state_path_nothing_is_written(tmp_path):
    m = ManualOrderMonitor()
    add_tmf(m)
    assert list(tmp_path.iterdir()) == [] and m.state_path is None


def test_default_state_path_follows_mode_env_and_the_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUAL_WATCH_PERSIST", "true")
    monkeypatch.setenv("SIMULATION", "false")
    assert default_state_path().name == "manual_watches_live.json"
    monkeypatch.setenv("SIMULATION", "true")
    assert default_state_path().name == "manual_watches_sim.json"                          # 正式盤與模擬盤各一個檔
    monkeypatch.setenv("MANUAL_WATCH_STATE", str(tmp_path / "x.json"))
    assert default_state_path() == tmp_path / "x.json"
    monkeypatch.setenv("MANUAL_WATCH_PERSIST", "false")
    assert default_state_path() is None


def test_setup_picks_the_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MANUAL_WATCH_PERSIST", "true")
    monkeypatch.setenv("MANUAL_WATCH_STATE", str(tmp_path / "s.json"))

    async def go():
        m = ManualOrderMonitor()
        m.setup(asyncio.get_running_loop())
        path = m.state_path
        await m.shutdown()
        return path

    assert asyncio.run(go()) == tmp_path / "s.json"


def test_a_corrupt_file_is_kept_aside_and_not_used(tmp_path, caplog):
    path = tmp_path / "w.json"
    path.write_text("{壞掉的 json", encoding="utf-8")
    assert _load_saved(path) == []
    assert not path.exists() and len(list(tmp_path.glob("w.json.bad-*"))) == 1 and "讀不懂" in caplog.text


def test_only_the_bad_entry_is_skipped(tmp_path):
    path = tmp_path / "w.json"
    good = saved(id="ok")
    bad = [saved(id="b1", direction=0), saved(id="b2", qty=0), saved(id="b3", match=""), {"id": "b4"}, "x", saved(id="b5", sl=-1), saved(id="b6", qty=True)]
    path.write_text(json.dumps({"version": 1, "watches": [good, *bad]}), encoding="utf-8")
    assert [w["id"] for w in _load_saved(path)] == ["ok"]
    assert _load_saved(tmp_path / "missing.json") == []
    path.write_text(json.dumps([1, 2]), encoding="utf-8")                                   # 結構不對（不是 {watches: []}）
    assert _load_saved(path) == []


# ── restore：重啟後對帳恢復 ───────────────────────────────────────

def restart(mm):
    """模擬後端重啟：新的 monitor、同一個落地檔，記憶體是空的。"""
    m = ManualOrderMonitor()
    m.state_path = mm.state_path
    return m


def test_restart_restores_the_stop_for_a_position_that_is_still_open(mm):
    wid = add_tmf(mm, qty=2, entry=49000.0, sl=50, tp=80)
    new = restart(mm)
    assert new._watches == {}                                                                # 重啟後記憶體是空的
    rep = asyncio.run(new.restore([pos(qty=2)]))
    assert rep["status"] == "ok" and [w["id"] for w in rep["restored"]] == [wid] and rep["dropped"] == []
    w = new._watches[wid]
    assert (w.contract, w.direction, w.quantity, w.remaining, w.entry_price, w.stop_loss_pts, w.take_profit_pts, w.seen) == ("TMF", 1, 2, 2, 49000.0, 50, 80, True)
    assert new.last_restore is rep and read(new)["watches"][0]["id"] == wid


def test_restart_drops_watches_whose_position_closed_and_cleans_the_file(mm):
    add_tmf(mm)
    new = restart(mm)
    rep = asyncio.run(new.restore([]))                                                        # 重啟期間已經平倉了
    assert rep["status"] == "ok" and rep["restored"] == [] and [d["reason"] for d in rep["dropped"]] == ["部位已不存在"]
    assert new._watches == {} and read(new)["watches"] == []


def test_a_stale_stop_never_attaches_to_a_new_position(mm):
    """重啟前的多單已經平了；重啟後使用者手動開了新的多單。舊的停損（進場 49000）不能掛到新部位（進場 49500）上。"""
    add_tmf(mm, entry=49000.0)
    new = restart(mm)
    asyncio.run(new.restore([]))                                                              # 啟動當下沒有部位 → 舊監看被丟掉
    assert new._watches == {}
    new2 = restart(new)
    assert asyncio.run(new2.restore([pos(price=49500.0)]))["restored"] == []                  # 再重啟一次也不會復活


def test_a_restored_stop_really_closes_the_position_when_hit(mm, monkeypatch):
    add_tmf(mm, entry=49000.0, sl=50)
    new = restart(mm)
    asyncio.run(new.restore([pos(price=49000.0)]))
    sent = []

    async def place_order(**kw):
        sent.append(kw)
        return {"trade_id": "C1", "status": "PendingSubmit"}

    monkeypatch.setattr(broker, "place_order", place_order)
    (w,) = new._watches.values()
    asyncio.run(new._check(w, [pos(last=49000.0)], {}))                                       # 還沒到停損
    assert sent == []
    asyncio.run(new._check(w, [pos(last=48950.0)], {}))                                       # −50 點：停損
    assert len(sent) == 1 and sent[0]["action"] == "Sell" and sent[0]["quantity"] == 1 and sent[0]["price_type"] == "MKT" and sent[0]["order_type"] == "IOC"


def test_restore_skips_ids_that_are_already_registered(mm):
    wid = add_tmf(mm, sl=50)
    new = restart(mm)
    new._watches[wid] = ManualWatch(id=wid, contract="TMF", direction=1, quantity=1, entry_price=1.0, stop_loss_pts=99, take_profit_pts=0, match_code="TMF")
    rep = asyncio.run(new.restore([pos()]))
    assert rep["restored"] == [] and new._watches[wid].stop_loss_pts == 99                    # 以記憶體裡的為準


def test_restore_keeps_a_pending_limit_entry_only_while_the_order_is_live(mm, monkeypatch):
    wid = add_tmf(mm, entry=0.0, order_id="T9")
    states = {"T9": "Submitted"}

    async def trades():
        return [{"id": k, "status": v, "deal_quantity": 0} for k, v in states.items()]

    monkeypatch.setattr(broker, "list_trades_with_status", trades)
    new = restart(mm)
    rep = asyncio.run(new.restore([]))
    assert [w["id"] for w in rep["restored"]] == [wid] and new._watches[wid].seen is False
    states["T9"] = "Cancelled"                                                                 # 下次啟動時單已經被取消
    new2 = restart(new)
    assert asyncio.run(new2.restore([]))["restored"] == [] and new2._watches == {}

    async def boom():
        raise asyncio.TimeoutError()

    monkeypatch.setattr(broker, "list_trades_with_status", boom)                               # 查不到委託狀態：保守起見丟掉
    new3 = restart(new)
    assert asyncio.run(new3.restore([]))["restored"] == []


def test_restore_waits_for_the_broker_then_reconciles(mm, monkeypatch):
    add_tmf(mm)
    new = restart(mm)
    calls = {"connected": 0, "positions": 0}

    class Conn:
        def __get__(self, obj, typ=None):
            calls["connected"] += 1
            return calls["connected"] >= 3                                                    # 前兩次輪詢還沒連上

    async def list_positions():
        calls["positions"] += 1
        if calls["positions"] == 1:
            raise asyncio.TimeoutError()                                                        # 連上了但第一次查詢失敗
        return [pos()]

    monkeypatch.setattr(type(broker), "is_connected", Conn())
    monkeypatch.setattr(broker, "list_positions", list_positions)
    rep = asyncio.run(new.restore(None, wait_sec=5, poll_sec=0.001))
    assert rep["status"] == "ok" and len(rep["restored"]) == 1 and rep["positions"] == [pos()] and calls["positions"] == 2


def test_restore_gives_up_after_the_deadline_and_leaves_the_file_alone(mm, monkeypatch):
    add_tmf(mm)
    before = mm.state_path.read_bytes()
    new = restart(mm)
    monkeypatch.setattr(type(broker), "is_connected", property(lambda self: False))
    rep = asyncio.run(new.restore(None, wait_sec=0.05, poll_sec=0.01))
    assert rep["status"] == "broker_unavailable" and rep["restored"] == [] and new._watches == {}
    assert mm.state_path.read_bytes() == before                                              # 連不上就不動檔案：下次啟動再試


def test_restore_without_saved_state_reports_the_positions_it_found(mm, monkeypatch):
    new = restart(mm)

    async def list_positions():
        return [pos(qty=2)]

    monkeypatch.setattr(type(broker), "is_connected", property(lambda self: True))
    monkeypatch.setattr(broker, "list_positions", list_positions)
    rep = asyncio.run(new.restore(None, wait_sec=1, poll_sec=0.001))
    assert rep == {"status": "none", "restored": [], "dropped": [], "positions": [pos(qty=2)], "saved": 0} and not mm.state_path.exists()
    new2 = restart(mm)
    monkeypatch.setattr(type(broker), "is_connected", property(lambda self: False))
    assert asyncio.run(new2.restore(None, wait_sec=0.03, poll_sec=0.01))["status"] == "none"   # 沒有落地資料又連不上：沒有東西要恢復


def test_restart_without_persistence_restores_nothing(tmp_path):
    m = ManualOrderMonitor()                                                                  # state_path = None（MANUAL_WATCH_PERSIST=false）
    add_tmf(m)
    assert asyncio.run(restart_none().restore([pos()]))["status"] == "none"


def restart_none():
    return ManualOrderMonitor()


def test_list_watches_exposes_seen_and_remaining(mm):
    wid = add_tmf(mm, qty=2)
    d = mm.list_watches()[0]
    assert d["id"] == wid and d["seen"] is False and d["remaining"] == 2
    mm._watches[wid].seen, mm._watches[wid].remaining = True, 1
    d = mm.list_watches()[0]
    assert d["seen"] is True and d["remaining"] == 1


def test_to_saved_roundtrips_every_saved_field(mm):
    wid = add_tmf(mm, qty=2, tp=40, octype="DayTrade")
    d = _to_saved(mm._watches[wid])
    w = _from_saved(d)
    assert (w.id, w.contract, w.direction, w.quantity, w.entry_price, w.stop_loss_pts, w.take_profit_pts, w.octype, w.created_at) == \
           (wid, "TMF", 1, 2, 49000.0, 50, 40, "DayTrade", mm._watches[wid].created_at)


def test_option_codes_in_a_saved_watch_are_compared_exactly():
    w = saved(match="TXO4980", is_option=True)
    assert reconcile([w], [pos(code="TXO49800J6")], set())[0] == []                          # 前綴不算同一檔選擇權
    assert len(reconcile([w], [pos(code="TXO4980")], set())[0]) == 1


def test_the_state_file_is_replaced_atomically(mm, monkeypatch):
    wid = add_tmf(mm, sl=50)
    before = read(mm)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("core.manual_monitor.os.replace", boom)
    mm.update(wid, stop_loss_pts=10)                                                         # 存檔失敗（只記 ERROR）
    assert read(mm) == before                                                                # 舊檔還是完整的
