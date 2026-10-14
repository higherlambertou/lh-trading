"""持倉保護狀態（core/protection.py）：誰沒有停損保護、持倉狀態檔、重啟／凍結的警示訊息。"""
from __future__ import annotations

import json

from core.protection import (
    build_watchdog_message, describe, load_position_state, startup_message, unprotected, write_position_state,
)


def pos(code="TMFJ6", direction="Buy", qty=1, price=49000.0):
    return {"code": code, "direction": direction, "quantity": qty, "price": price, "last_price": price}


def watch(**kw):
    w = {"id": "w1", "match_code": "TMF", "direction": "Buy", "quantity": 1, "remaining": 1, "stop_loss_pts": 50, "take_profit_pts": 0,
         "is_option": False, "seen": True, "close_gave_up": False}
    w.update(kw)
    return w


# ── unprotected ──────────────────────────────────────────────────

def test_a_position_with_nothing_watching_it_is_unprotected():
    assert unprotected([pos(qty=2)], []) == [{"code": "TMFJ6", "direction": "Buy", "quantity": 2, "total": 2}]
    assert unprotected([], [watch()]) == [] and unprotected([pos(qty=0)], []) == []


def test_a_manual_watch_with_a_stop_covers_the_position():
    assert unprotected([pos()], [watch()]) == []
    assert unprotected([pos(code="MXFJ6")], [watch(match_code="MXF")]) == []                 # 期貨用代碼前綴比對


def test_what_does_not_count_as_protection():
    p = [pos()]
    assert len(unprotected(p, [watch(stop_loss_pts=0, take_profit_pts=30)])) == 1             # 只有停利、沒有停損
    assert len(unprotected(p, [watch(close_gave_up=True)])) == 1                              # 已經放棄平倉的
    assert len(unprotected(p, [watch(seen=False)])) == 1                                      # 還在等開倉單成交的監看，不是這個部位的保護
    assert len(unprotected(p, [watch(direction="Sell")])) == 1                                # 方向不同
    assert len(unprotected(p, [watch(match_code="MXF")])) == 1                                # 合約不同


def test_partial_cover_reports_the_lots_left():
    assert unprotected([pos(qty=3)], [watch(remaining=1), watch(id="w2", remaining=1)]) == [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "total": 3}]
    assert unprotected([pos(qty=2)], [watch(remaining=5)]) == []                              # 監看口數比部位多：算全覆蓋
    assert unprotected([pos(qty=2)], [watch(remaining=0, quantity=1)]) == [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "total": 2}]   # remaining 0 → 用 quantity


def test_option_watches_match_the_exact_code():
    opt = pos(code="TXO49800J6", direction="Buy", qty=1)
    w = watch(match_code="TXO49800J6", is_option=True)
    assert unprotected([opt], [w]) == []
    assert len(unprotected([pos(code="TXO49800K6")], [w])) == 1                               # 選擇權不能用前綴比對


def test_a_running_strategy_covers_only_micro_futures():
    assert unprotected([pos(code="TMFJ6")], [], strategy_running=True) == []
    assert len(unprotected([pos(code="MXFJ6")], [], strategy_running=True)) == 1              # 策略只交易微台
    assert len(unprotected([pos(code="TXO49800J6")], [], strategy_running=True)) == 1
    assert len(unprotected([pos()], [], strategy_running=False)) == 1


def test_each_position_row_is_judged_alone():
    got = unprotected([pos(), pos(code="MXFJ6", direction="Sell", qty=2)], [watch()])
    assert got == [{"code": "MXFJ6", "direction": "Sell", "quantity": 2, "total": 2}]


def test_describe():
    assert describe([pos(), pos(code="MXFJ6", direction="Sell", qty=2)]) == "TMFJ6 多 1 口、MXFJ6 空 2 口"
    assert describe([{"code": "X", "direction": "?", "quantity": 1}]) == "X ? 1 口"


# ── 持倉狀態檔 ───────────────────────────────────────────────────

def test_state_file_roundtrip_is_atomic_and_tolerates_bad_files(tmp_path):
    path = tmp_path / "sub" / "state.json"
    assert write_position_state([pos(qty=2)], [{"code": "TMFJ6"}], "live", path=path, now=123.0)
    assert not (tmp_path / "sub" / "state.json.tmp").exists()                                 # 暫存檔已 rename 掉
    d = load_position_state(path)
    assert d == {"version": 1, "ts": 123.0, "mode": "live", "positions": [{"code": "TMFJ6", "direction": "Buy", "quantity": 2, "price": 49000.0}],
                 "unprotected": [{"code": "TMFJ6"}]}
    assert load_position_state(tmp_path / "missing.json") is None
    path.write_text("{壞掉", encoding="utf-8")
    assert load_position_state(path) is None
    path.write_text(json.dumps({"positions": "x"}), encoding="utf-8")
    assert load_position_state(path) is None


def test_state_write_failure_returns_false_instead_of_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert write_position_state([pos()], [], "live", path=blocker / "state.json") is False    # 上層是檔案，建不了資料夾


# ── watchdog 警示 ────────────────────────────────────────────────

def state(positions, unprot=(), ts=1000.0):
    return {"ts": ts, "positions": positions, "unprotected": list(unprot)}


def test_watchdog_message_needs_a_known_position():
    assert build_watchdog_message("freeze", "down", None) is None
    assert build_watchdog_message("freeze", "down", state([])) is None


def test_watchdog_message_content():
    title, msg = build_watchdog_message("freeze", "down", state([pos(qty=2)]), now=1010.0)
    assert title == "⚠️ 後端凍結或斷線，持倉暫時沒有停損保護"
    assert "持倉：TMFJ6 多 2 口（不到 1 分鐘前的資料）。" in msg and "原因：連 /health 都沒有回應。" in msg and "重新登入通常要 1～3 分鐘" in msg
    assert "凍結前就沒有" not in msg
    t2, m2 = build_watchdog_message("exit", "gone", state([pos()], [{"code": "TMFJ6", "direction": "Buy", "quantity": 1}]), now=1010.0)
    assert t2 == "⚠️ 後端進程結束，持倉暫時沒有停損保護" and "原因：進程已不存在。" in m2 and "（凍結前就沒有停損保護的：TMFJ6 多 1 口）" in m2
    assert "原因：沒有連上券商。" in build_watchdog_message("freeze", "nobroker", state([pos()]), now=1010.0)[1]
    assert "原因：怪事。" in build_watchdog_message("freeze", "怪事", state([pos()]), now=1010.0)[1]


def test_watchdog_message_says_how_old_the_information_is():
    s = state([pos()], ts=0.0)
    assert "（5 分鐘前的資料）" in build_watchdog_message("freeze", "down", s, now=300.0)[1]
    assert "（2.0 小時前的資料）" in build_watchdog_message("freeze", "down", s, now=7200.0)[1]      # 很舊但最後已知有持倉：照樣警示
    assert "（不到 1 分鐘前的資料）" in build_watchdog_message("freeze", "down", s, now=-50.0)[1]    # 時鐘倒退不會出現負數


# ── 啟動報告 ─────────────────────────────────────────────────────

def restored(**kw):
    w = {"match_code": "TMF", "direction": 1, "quantity": 1, "entry_price": 49000.0, "stop_loss_pts": 50, "take_profit_pts": 0}
    w.update(kw)
    return w


def test_startup_message_when_everything_was_restored():
    title, msg = startup_message([pos()], [restored(take_profit_pts=80)], [])
    assert title == "後端已重啟：持倉的停損監控已恢復"
    assert "持倉：TMFJ6 多 1 口。" in msg and "已恢復手動停損監控：TMF 多 1 口，停損 50 點（約 48,950）、停利 80 點。" in msg


def test_startup_message_for_a_short_position_puts_the_stop_above_the_entry():
    _, msg = startup_message([pos(direction="Sell")], [restored(direction=-1, entry_price=49000.0, stop_loss_pts=40)], [])
    assert "TMF 空 1 口，停損 40 點（約 49,040）" in msg


def test_startup_message_warns_about_unprotected_positions():
    title, msg = startup_message([pos(), pos(code="MXFJ6")], [restored()], [{"code": "MXFJ6", "direction": "Buy", "quantity": 1}])
    assert title == "⚠️ 後端已重啟：持倉沒有停損保護"
    assert "沒有停損保護：MXFJ6 多 1 口（這些口數沒有策略在跑、也沒有停損監控覆蓋）" in msg and "已恢復手動停損監控" in msg
    _, m2 = startup_message([pos()], [], [{"code": "TMFJ6", "direction": "Buy", "quantity": 1}], status="broker_unavailable")
    assert "券商一直連不上，停損監控還沒恢復。" in m2


def test_startup_message_is_silent_without_positions():
    assert startup_message([], [restored()], []) is None
    assert startup_message([], [], []) is None


# ── 補：變異測試抓到的缺口 ───────────────────────────────────────

def test_option_watch_codes_are_compared_exactly_not_by_prefix():
    w = watch(match_code="TXO4980", is_option=True)
    assert len(unprotected([pos(code="TXO49800J6")], [w])) == 1                              # 前綴不算同一檔選擇權
    assert unprotected([pos(code="TXO4980")], [w]) == []


def test_a_watch_without_a_code_covers_nothing():
    assert len(unprotected([pos()], [watch(match_code="")])) == 1
    assert len(unprotected([pos()], [watch(match_code=None)])) == 1


def test_age_text_boundaries():
    s = state([pos()], ts=0.0)

    def msg(sec):
        return build_watchdog_message("freeze", "down", s, now=float(sec))[1]

    assert "（不到 1 分鐘前的資料）" in msg(89) and "（1 分鐘前的資料）" in msg(90)
    assert "（59 分鐘前的資料）" in msg(3599) and "（1.0 小時前的資料）" in msg(3600)


def test_a_failed_replace_leaves_the_old_state_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    assert write_position_state([pos()], [], "live", path=path, now=1.0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("core.protection.os.replace", boom)
    assert write_position_state([pos(qty=9)], [], "live", path=path, now=2.0) is False
    d = load_position_state(path)
    assert d["ts"] == 1.0 and d["positions"][0]["quantity"] == 1                              # 寫到一半失敗，舊檔還是完整的
