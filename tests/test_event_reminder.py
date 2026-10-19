"""美國重要事件提醒（發現 52）：CPI、非農、FOMC 公布前推播一次（有沒有持倉都提醒）；窗口、事件篩選、訊息內容、狀態機、讀不到不提醒；只通知、不碰下單。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import core.event_reminder as er
import core.intel_reader as ir
from core import daytrade
from intel_fixture import event, make_db

TPE = er.TAIPEI
ROOT = Path(er.__file__).resolve().parent.parent


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TPE).timestamp()


def ev(kind="CPI", uid=None, when=None, local="2026-11-10"):
    src, title = {"CPI": ("bls", "Consumer Price Index"), "NFP": ("bls", "Employment Situation"), "FOMC": ("fed", "FOMC 利率決策")}[kind]
    return {"uid": uid or f"{kind}-1", "source": src, "title_en": title, "scheduled_time": when, "local_date": local, "importance": 3}


CPI_T = at(2026, 11, 10, 21, 30)                                                   # 冬令：美東 08:30 ＝ 台北 21:30
CPI = ev("CPI", "bls-cpi-nov", CPI_T)
NFP = ev("NFP", "bls-nfp-nov", at(2026, 11, 6, 21, 30), "2026-11-06")
FOMC = ev("FOMC", "fomc-2026-10-28", at(2026, 10, 29, 2, 0), "2026-10-28")           # 夏令：14:00 EDT ＝ 台北隔天 02:00


def pos(code="TMFK6", direction="Buy", q=1):
    return {"code": code, "direction": direction, "quantity": q}


# ── 事件篩選與窗口 ───────────────────────────────────────────────

def test_kind_of_picks_the_three_events_only():
    assert er.kind_of(CPI) == "CPI" and er.kind_of(NFP) == "NFP" and er.kind_of(FOMC) == "FOMC"
    assert er.kind_of({**FOMC, "title_en": "FOMC 利率決策（含經濟預測 SEP）"}) == "FOMC"
    for bad in ({**FOMC, "title_en": "FOMC 會議紀錄"}, {**CPI, "title_en": "Producer Price Index"}, {**CPI, "title_en": "Consumer Credit"}, {**CPI, "title_en": "Consumer Price Index (revised)"}, {**CPI, "source": "fred"}, {**NFP, "source": "fed"}, {**FOMC, "source": "bls"},
                {**CPI, "scheduled_time": None}, {**CPI, "title_en": None}, {}):
        assert er.kind_of(bad) is None, bad


def test_window_starts_at_1230_taipei_on_the_us_local_date():
    assert er.window_start(CPI) == at(2026, 11, 10, 12, 30)
    assert er.window_start(FOMC) == at(2026, 10, 28, 12, 30)                        # 台北隔天凌晨才公布 → 前一天 12:30 就開始
    assert er.window_start({**CPI, "local_date": None, "scheduled_date": "2026-11-10"}) == at(2026, 11, 10, 12, 30)
    assert er.window_start({**CPI, "local_date": "壞掉"}) is None and er.window_start({**CPI, "local_date": None}) is None


def test_due_events_window_boundaries():
    assert er.due_events(at(2026, 11, 10, 12, 30), [CPI]) == [CPI]
    assert er.due_events(at(2026, 11, 10, 12, 29, 59), [CPI]) == []
    assert er.due_events(at(2026, 11, 10, 21, 29, 59), [CPI]) == [CPI]
    assert er.due_events(at(2026, 11, 10, 21, 30), [CPI]) == []                    # 公布了就不提醒
    assert er.due_events(at(2026, 11, 9, 20, 0), [CPI]) == [] and er.due_events(at(2026, 11, 11, 13, 0), [CPI]) == []
    assert er.due_events(at(2026, 10, 28, 12, 30), [FOMC]) == [FOMC] and er.due_events(at(2026, 10, 29, 1, 59), [FOMC]) == [FOMC]      # 跨過午夜仍在窗口
    assert er.due_events(at(2026, 10, 29, 2, 0), [FOMC]) == [] and er.due_events(at(2026, 10, 28, 12, 29), [FOMC]) == []
    assert er.due_events(at(2026, 11, 10, 15, 0), None) == [] and er.due_events(at(2026, 11, 10, 15, 0), []) == []
    others = [{**CPI, "title_en": "Real Earnings"}, {**CPI, "scheduled_time": None}, {**CPI, "local_date": "x"}]
    assert er.due_events(at(2026, 11, 10, 15, 0), others) == []
    assert er.due_events(at(2026, 11, 10, 15, 0), [NFP, CPI, FOMC]) == [CPI]


def test_when_text():
    now = at(2026, 11, 10, 15, 0)
    assert er.when_text(CPI_T, now) == "今晚 21:30"
    assert er.when_text(at(2026, 11, 11, 3, 0), now) == "明天凌晨 03:00" and er.when_text(at(2026, 11, 11, 9, 0), now) == "明天 09:00"
    assert er.when_text(at(2026, 11, 11, 3, 0), at(2026, 11, 11, 1, 0)) == "今天凌晨 03:00" and er.when_text(at(2026, 11, 10, 14, 0), now) == "今天 14:00"
    assert er.when_text(at(2026, 11, 13, 21, 30), now) == "11/13 21:30"
    assert er.when_text(at(2026, 7, 14, 20, 30), at(2026, 7, 14, 12, 30)) == "今晚 20:30"


# ── 哪些持倉算 ───────────────────────────────────────────────────

def test_carried_skips_zero_blank_and_daytrade_positions():
    daytrade._MARKS.clear()
    try:
        daytrade.note_open("TXF", 1)
        today = daytrade.taipei_now()
        items = er.carried([pos("TXFK6", "Buy", 1), pos("TMFK6", "Sell", 2), pos("TMFK6", "Buy", 0), pos("", "Buy", 1)], [], today)
        assert [(i["code"], i["direction"], i["quantity"]) for i in items] == [("TMFK6", "Sell", 2)]                  # 當沖的大台不算、0 口與空代碼不算
    finally:
        daytrade._MARKS.clear()
    now = datetime.fromtimestamp(at(2026, 11, 10, 13, 0), TPE)
    assert [i["code"] for i in er.carried([pos("TXFK6")], [], now)] == ["TXFK6"]                                   # 沒登記當沖就算
    assert er.carried([], [], now) == [] and er.carried(None, None, now) == []


# ── 訊息 ─────────────────────────────────────────────────────────

def test_messages_state_only_what_the_study_found():
    now = at(2026, 11, 10, 15, 0)
    t, b = er.message("CPI", CPI_T, now, [])
    assert t == "📅 今晚 21:30 美國 CPI" and "61 次" in b and "154 點" in b and "100 點" in b and "約 1.6 倍" in b and "振幅沒有明顯變大" in b and b.endswith("這是歷史統計、不是方向預測。") and "持有" not in b
    t, b = er.message("NFP", at(2026, 11, 6, 21, 30), at(2026, 11, 6, 15, 0), [])
    assert t == "📅 今晚 21:30 美國非農就業" and "66 次" in b and "沒有確認" in b and "約 1.1 倍" in b and "154" not in b
    t, b = er.message("FOMC", FOMC["scheduled_time"], at(2026, 10, 28, 15, 0), [])
    assert t == "📅 明天凌晨 02:00 FOMC 利率決策" and "45 次" in b and "約 1.04 倍" in b and "1.6～2.4 倍" in b and "1.05～1.18 倍" in b and "沒有確認" in b


def test_messages_with_positions_list_them_and_never_mention_equity():
    items = [{"code": "TMFK6", "direction": "Buy", "quantity": 2}, {"code": "MXFK6", "direction": "Sell", "quantity": 1}]
    now = at(2026, 11, 10, 15, 0)
    t, b = er.message("CPI", CPI_T, now, items)
    assert t == "📅 今晚 21:30 美國 CPI，你還有持倉" and b.startswith("持有 TMFK6 多 2 口、MXFK6 空 1 口。") and b.endswith("要不要留倉自行決定。")
    t2, b2 = er.message("CPI", CPI_T, now, items, follow_up=True)
    assert t2 == "📅 今晚 21:30 美國 CPI，你現在有持倉" and b2 == b
    for kind in er.EVENTS_ALL:
        for its in ([], items):
            tt, bb = er.message(kind, CPI_T, now, its)
            assert "權益" not in tt + bb and "保證金" not in tt + bb and "預測" in bb


def test_stat_matches_the_registered_study_result_when_it_is_on_disk():
    p = ROOT / "data" / "event_day_result.json"
    if not p.exists():
        pytest.skip("本機沒有 data/event_day_result.json")
    r = json.loads(p.read_text(encoding="utf-8"))
    T, D = r["tests"], r["descriptive"]
    for k in er.EVENTS_ALL:
        assert er.STAT[k]["n"] == D[k]["range"]["n"] and er.STAT[k]["range_amp"] == round(T[f"{k}/range"]["pooled"]["amp"], 2), k
        assert T[f"{k}/range"]["confirmed"] is False                              # 三種的日盤振幅都沒確認（訊息才能寫「沒有放大」）
    c = er.STAT["CPI"]
    assert T["CPI/gap"]["confirmed"] is True and (c["gap_amp"], c["gap_lo"], c["gap_hi"]) == tuple(round(T["CPI/gap"]["pooled"][k], 2) for k in ("amp", "amp_lo", "amp_hi"))
    assert (c["median_event"], c["median_ctrl"]) == (round(D["CPI"]["gap"]["points_median_event"]), round(D["CPI"]["gap"]["points_median_ctrl"]))
    for k in ("NFP", "FOMC"):
        assert T[f"{k}/gap"]["confirmed"] is False
        assert er.STAT[k]["gap_explore"] == round(T[f"{k}/gap"]["explore"]["amp"], 2) and er.STAT[k]["gap_holdout"] == round(T[f"{k}/gap"]["holdout"]["amp"], 2)
    y = {int(a): q["amp"] for a, q in D["FOMC"]["gap"]["yearly"].items()}
    assert (f"{min(y[a] for a in (2021, 2022, 2023, 2024)):.1f}", f"{max(y[a] for a in (2021, 2022, 2023, 2024)):.1f}") == tuple(er.STAT["FOMC"]["gap_early"].split("～"))
    assert (f"{min(y[2025], y[2026]):.2f}", f"{max(y[2025], y[2026]):.2f}") == tuple(er.STAT["FOMC"]["gap_late"].split("～"))


# ── 狀態機 ───────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(er, "notify", lambda title, msg, **kw: out.append((title, msg, kw)) or True)
    return out


NOW = at(2026, 11, 10, 15, 0)


def test_sends_once_per_event_even_when_flat(sent):
    r = er.EventReminder()
    assert r.check(NOW, [], [], [CPI]) == ["CPI"]
    assert len(sent) == 1 and sent[0][0] == "📅 今晚 21:30 美國 CPI" and sent[0][2] == {"key": "event:bls-cpi-nov", "cooldown": er.COOLDOWN, "priority": "default"}
    for k in range(1, 6):
        assert r.check(NOW + 60 * k, [], [], [CPI]) == []
    assert len(sent) == 1


def test_a_position_opened_later_gets_one_follow_up(sent):
    r = er.EventReminder()
    r.check(NOW, [], [], [CPI])
    assert r.check(NOW + 3600, [pos("TMFK6", "Buy", 1)], [], [CPI]) == ["CPI"]
    assert len(sent) == 2 and sent[1][0].endswith("你現在有持倉") and sent[1][2]["key"] == "event:bls-cpi-nov:held" and sent[1][2]["priority"] == "high" and "TMFK6 多 1 口" in sent[1][1]
    r.check(NOW + 7200, [pos("TMFK6", "Buy", 1)], [], [CPI])
    r.check(NOW + 7260, [], [], [CPI])
    r.check(NOW + 7320, [pos("TMFK6", "Buy", 3)], [], [CPI])
    assert len(sent) == 2                                                          # 只補一次


def test_holding_at_the_start_gets_one_message_with_the_positions_and_no_follow_up(sent):
    r = er.EventReminder()
    assert r.check(NOW, [pos("TMFK6", "Sell", 2)], [], [CPI]) == ["CPI"]
    assert sent[0][0] == "📅 今晚 21:30 美國 CPI，你還有持倉" and sent[0][2]["priority"] == "high" and sent[0][2]["key"] == "event:bls-cpi-nov"
    r.check(NOW + 60, [], [], [CPI])                                               # 平倉了：不再推
    r.check(NOW + 120, [pos("TMFK6", "Buy", 1)], [], [CPI])
    assert len(sent) == 1


def test_each_event_is_independent(sent):
    r = er.EventReminder()
    t = at(2026, 11, 6, 15, 0)
    r.check(t, [], [], [NFP, CPI, FOMC])                                           # 這時間只有 NFP 在窗口（CPI 是 11/10、FOMC 是 10/28 已過）
    assert [s[2]["key"] for s in sent] == ["event:bls-nfp-nov"]
    r2 = er.EventReminder()
    both = [CPI, {**NFP, "scheduled_time": at(2026, 11, 10, 21, 30), "local_date": "2026-11-10", "uid": "n2"}]
    assert sorted(r2.check(NOW, [], [], both)) == ["CPI", "NFP"] and {s[2]["key"] for s in sent[1:]} == {"event:bls-cpi-nov", "event:n2"}


def test_fomc_is_reminded_the_afternoon_before_and_states_the_time(sent):
    r = er.EventReminder()
    assert r.check(at(2026, 10, 28, 12, 30), [], [], [FOMC]) == ["FOMC"] and sent[0][0] == "📅 明天凌晨 02:00 FOMC 利率決策"
    assert r.check(at(2026, 10, 29, 1, 0), [pos()], [], [FOMC]) == ["FOMC"] and sent[1][0] == "📅 今天凌晨 02:00 FOMC 利率決策，你現在有持倉"      # 半夜才開倉：補一則，用當下的說法


def test_nothing_when_events_are_unavailable_or_not_due(sent):
    r = er.EventReminder()
    assert r.check(NOW, [pos()], [], None) == [] and r.check(NOW, [pos()], [], []) == []
    assert r.check(at(2026, 11, 9, 15, 0), [pos()], [], [CPI]) == [] and r.check(at(2026, 11, 10, 21, 31), [pos()], [], [CPI]) == []
    assert r.check(at(2026, 11, 10, 12, 29), [], [], [CPI]) == [] and sent == []


def test_a_message_that_was_not_delivered_is_not_followed_up(monkeypatch):
    """key 已經被用掉（重啟前推過、或通道暫時失敗被冷卻擋下）notify 回 False：不重複補。"""
    calls = []
    monkeypatch.setattr(er, "notify", lambda *a, **k: calls.append(k["key"]) and False)
    r = er.EventReminder()
    assert r.check(NOW, [], [], [CPI]) == []
    assert r.check(NOW + 60, [pos()], [], [CPI]) == [] and r.check(NOW + 120, [pos()], [], [CPI]) == []
    assert calls == ["event:bls-cpi-nov"]


def test_daytrade_only_positions_count_as_flat(sent):
    daytrade._MARKS.clear()
    try:
        daytrade.note_open("TXF", 1)
        now = daytrade.taipei_now()
        t0 = now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()
        today = now.date().isoformat()
        cpi = {**CPI, "scheduled_time": now.replace(hour=21, minute=30, second=0, microsecond=0).timestamp(), "local_date": today}
        r = er.EventReminder()
        assert r.check(t0, [pos("TXFK6", "Buy", 1)], [], [cpi]) == ["CPI"] and "持倉" not in sent[0][0] and sent[0][2]["priority"] == "default"
        assert r.check(t0 + 60, [pos("TXFK6", "Buy", 1), pos("TMFK6", "Buy", 1)], [], [cpi]) == ["CPI"] and "TMFK6" in sent[1][1] and "TXFK6" not in sent[1][1]
    finally:
        daytrade._MARKS.clear()


# ── 事件快取 ─────────────────────────────────────────────────────

def test_cached_events_reads_at_most_once_per_ttl_and_keeps_the_last_good_copy():
    reads = []

    def fetch(t):
        reads.append(t)
        return [CPI] if len(reads) != 3 else None
    c = er.CachedEvents(fetch, ttl=600)
    assert c.get(1000) == [CPI] and c.get(1599) == [CPI] and reads == [1000]
    assert c.get(1600) == [CPI] and reads == [1000, 1600]
    assert c.get(2200) == [CPI] and reads == [1000, 1600, 2200]                    # 這次讀不到（None）：沿用上一份
    assert c.get(2300) == [CPI] and len(reads) == 3
    assert c.get(2800) == [CPI] and len(reads) == 4
    first_fail = er.CachedEvents(lambda t: None)
    assert first_fail.get(1) is None
    back = er.CachedEvents(fetch, ttl=600)
    back.at = 5000.0
    reads.clear()
    back.get(100)
    assert len(reads) == 1                                                         # 時鐘倒退也重讀


# ── intel_reader.upcoming_events ─────────────────────────────────

def seed(tmp_path):
    c = make_db(tmp_path / "intel.db")
    event(c, "bls-cpi", "Consumer Price Index", when=CPI_T, date="2026-11-10", importance=3, source="bls", detail={"zh": "美國消費者物價指數 CPI"})
    event(c, "bls-old", "Consumer Price Index", when=NOW - 3600, date="2026-11-10", importance=3, source="bls")                  # 已經過去
    event(c, "bls-far", "Consumer Price Index", when=NOW + 5 * 86400, date="2026-11-15", importance=3, source="bls")              # 2 天以外
    event(c, "bls-low", "Real Earnings", when=CPI_T, date="2026-11-10", importance=1, source="bls")
    event(c, "fomc-x", "FOMC 利率決策", when=at(2026, 11, 11, 3, 0), date="2026-11-10", importance=3, source="fed")
    c.close()
    return tmp_path / "intel.db"


def test_upcoming_events_returns_the_future_rows_the_panel_would_show(tmp_path):
    out = ir.upcoming_events(NOW, seed(tmp_path))
    assert [e["uid"] for e in out] == ["bls-cpi", "fomc-x"] and out[0]["source"] == "bls" and out[0]["title_en"] == "Consumer Price Index" and out[0]["scheduled_time"] == CPI_T
    assert [e["uid"] for e in ir.upcoming_events(NOW, tmp_path / "intel.db", ahead_days=7)] == ["bls-cpi", "fomc-x", "bls-far"]
    assert [kind for kind in map(er.kind_of, out)] == ["CPI", "FOMC"] and out[1]["local_date"] == "2026-11-10"
    assert [e["uid"] for e in er.due_events(NOW, out)] == ["bls-cpi", "fomc-x"]


def test_the_loops_fetch_reads_three_days_ahead_from_the_configured_database(tmp_path, monkeypatch):
    ir._cache.update(key=None, at=0.0, data=None)
    monkeypatch.setenv("INTEL_DB", str(seed(tmp_path)))
    got = er._fetch(NOW)
    assert [e["uid"] for e in got] == ["bls-cpi", "fomc-x"]                         # 3 天內的 CPI 與 FOMC；5 天後的不要
    monkeypatch.setenv("INTEL_DB", str(tmp_path / "nope.db"))
    assert er._fetch(NOW) is None


def test_upcoming_events_never_raises_and_returns_none(tmp_path):
    assert ir.upcoming_events(NOW, tmp_path / "nope.db") is None
    new = tmp_path / "b"
    new.mkdir()
    make_db(new / "intel.db", version=ir.SUPPORTED_SCHEMA + 1).close()
    assert ir.upcoming_events(NOW, new / "intel.db") is None                       # 版本比認得的新
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 50)
    assert ir.upcoming_events(NOW, bad) is None
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert ir.upcoming_events(NOW, empty) is None                                  # 沒有 schema_meta


def test_upcoming_events_is_read_only(tmp_path):
    p = seed(tmp_path)
    before = p.read_bytes()
    ir.upcoming_events(NOW, p)
    assert p.read_bytes() == before


# ── 迴圈 ─────────────────────────────────────────────────────────

def run_loop(seconds, **kw):
    async def go():
        t = asyncio.create_task(er.event_reminder_loop(**kw))
        await asyncio.sleep(seconds)
        alive = not t.done()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return alive
    return asyncio.run(go())


def test_loop_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("EVENT_REMINDER", "false")
    assert er.enabled() is False
    asyncio.run(asyncio.wait_for(er.event_reminder_loop(lambda: [pos()], lambda: []), 1))                    # 直接結束
    monkeypatch.delenv("EVENT_REMINDER")
    assert er.enabled() is True


def test_loop_runs_even_when_flat_and_survives_errors(monkeypatch):
    monkeypatch.setattr(er, "POLL_SEC", 0.01)
    seen = []
    monkeypatch.setattr(er, "_fetch", lambda t: seen.append("fetch") or [])
    monkeypatch.setattr(er.EventReminder, "check", lambda self, *a, **k: seen.append(("check", a[1])) or [])
    assert run_loop(0.12, get_positions=lambda: [], get_watches=lambda: []) is True
    assert any(s == ("check", []) for s in seen) and seen.count("fetch") == 1            # 空手也跑；資料庫每 10 分鐘才讀一次，不是每輪

    n = {"c": 0}

    def boom(self, *a, **k):
        n["c"] += 1
        raise RuntimeError("壞了")
    monkeypatch.setattr(er.EventReminder, "check", boom)
    assert run_loop(0.12, get_positions=lambda: [pos()], get_watches=lambda: []) is True                     # 例外被吞掉，迴圈還活著
    assert 2 <= n["c"] <= 40                                                       # 有睡覺（POLL_SEC），不是空轉


def test_loop_really_sends_through_the_state_machine(monkeypatch):
    import types
    monkeypatch.setattr(er, "POLL_SEC", 0.01)
    monkeypatch.setattr(er, "time", types.SimpleNamespace(time=lambda: NOW))                  # 固定『現在』＝ 11/10 15:00
    monkeypatch.setattr(er, "_fetch", lambda ts: [CPI])
    out = []
    monkeypatch.setattr(er, "notify", lambda title, msg, **kw: out.append((title, kw["key"], kw["priority"])) or True)
    run_loop(0.1, get_positions=lambda: [], get_watches=lambda: [])
    assert out == [("📅 今晚 21:30 美國 CPI", "event:bls-cpi-nov", "default")]                # 空手也推、只推一次
    out.clear()
    run_loop(0.1, get_positions=lambda: [pos("TMFK6", "Buy", 1)], get_watches=lambda: [])      # 新的迴圈＝新的狀態；這次有持倉
    assert out == [("📅 今晚 21:30 美國 CPI，你還有持倉", "event:bls-cpi-nov", "high")]


# ── 只通知、不碰下單 ─────────────────────────────────────────────

def test_event_reminder_is_wired_in_main_and_not_used_by_trading_paths():
    assert "event_reminder_loop" in (ROOT / "main.py").read_text(encoding="utf-8")
    paths = list((ROOT / "strategies").glob("*.py")) + [ROOT / "core" / n for n in ("broker.py", "manual_monitor.py", "protection.py", "quote_hub.py", "shioaji_worker.py", "trade_log.py", "entry_log.py")] \
        + [ROOT / "api" / n for n in ("routes_order.py", "routes_strategy.py", "routes_position.py", "routes_quote.py")] + list((ROOT / "core").glob("*risk*.py"))
    for p in paths:
        if p.exists():
            assert "event_reminder" not in p.read_text(encoding="utf-8"), p
