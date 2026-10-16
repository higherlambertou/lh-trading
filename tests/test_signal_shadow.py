"""順勢爆量訊號的影子模式（update.md 發現 43）：偵測與記錄、同一個訊號不重複、通知（只發登記的變體、舊訊號不發）、後續 K 棒、結果的計算、API。
用假的 flow_1m 與暫存資料庫；通知用假的 notify，不碰網路與真實資料庫。"""
from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

import api.routes_signals as rs
import core.signal_rules as sr
import core.signal_shadow as sh
from core.daytrade import TAIPEI

ROOT = Path(__file__).resolve().parent.parent
BASE = 47000.0


def tpe(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TAIPEI)


def row(ts, o, h, l, c, vol):
    return {"ts": int(ts), "open": o, "high": h, "low": l, "close": c, "buy_vol": vol // 2, "sell_vol": vol - vol // 2, "unk_vol": 0, "buy_n": 3, "sell_n": 2, "unk_n": 0}


PRIOR_DAYS = [(2026, 9, 28), (2026, 9, 29), (2026, 9, 30), (2026, 10, 1), (2026, 10, 2), (2026, 10, 5), (2026, 10, 6), (2026, 10, 7),
              (2026, 10, 8), (2026, 10, 9), (2026, 10, 12), (2026, 10, 13)]
TODAY = "2026-10-14"
START = sh.session_start_ts("day", TODAY)
M0 = 40                                                       # 爆量那一分鐘（09:25）


def prior_rows(days=PRIOR_DAYS):
    out = []
    for y, mo, d in days:
        s = int(tpe(y, mo, d, 8, 45).timestamp())
        out += [row(s + i * 60, BASE, BASE + 2, BASE - 2, BASE, 200) for i in range(300)]
    return out


def today_rows(upto: int, drift: float = 1.0, jump: float = 60.0):
    """今天第 0～upto 分鐘：前面平的、第 M0 分鐘一口氣跳 jump 點且量 ×7.5、之後每分鐘多走 drift 點。"""
    out = []
    prev = BASE
    for i in range(upto + 1):
        if i < M0:
            c, v = BASE, 200
        elif i == M0:
            c, v = BASE + jump, 1500
        else:
            c, v = BASE + jump + drift * (i - M0), 200
        out.append(row(START + i * 60, prev, max(prev, c) + 1, min(prev, c) - 1, c, v))
        prev = c
    return out


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def flow_1m(self, code, since_ts=None, until_ts=None):
        return [r for r in self.rows if since_ts is None or r["ts"] >= since_ts]


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    rec = Rec()
    monkeypatch.setattr(sh, "notify", rec)
    monkeypatch.delenv("SHADOW_NOTIFY", raising=False)
    monkeypatch.delenv("SIGNAL_SHADOW", raising=False)
    monkeypatch.setenv("SHADOW_COST_PTS", "4")
    store = sh.SignalStore(tmp_path / "signals.db")
    return store, rec


def check(engine, now, rows):
    return asyncio.run(engine.check(now, FakeStore(rows)))


NOW = START + 43 * 60 + 5                                     # 第 43 分鐘進行中；最後收完的是第 42 分鐘


# ── 偵測與記錄 ───────────────────────────────────────────────

def test_a_burst_is_recorded_for_every_threshold_and_notified_once(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    rows = prior_rows() + today_rows(42)
    new = check(eng, NOW, rows)
    assert sorted(s["rule"] for s in new) == ["burst30", "burst40", "burst50"]
    ev = new[0]
    assert ev["ts"] == START + M0 * 60 and ev["session"] == "day" and ev["direction"] == 1 and ev["close"] == BASE + 60
    assert ev["move"] == pytest.approx(60 / BASE) and ev["ratio"] == pytest.approx(2300 / 1000)
    assert len(store.rows()) == 3
    assert len(rec.calls) == 1                                                       # 預設只通知 50 點那個
    c = rec.calls[0]
    assert c["key"] == f"shadow:day:{START + M0 * 60}" and c["priority"] == "default" and c["cooldown"] == 3600.0
    assert "急漲" in c["title"] and "+60 點" in c["title"] and "2.3 倍" in c["title"]
    assert "沒有下單" in c["message"] and "09:25" in c["message"] and "47,060" in c["message"] and "沒有找到優勢" in c["message"]


def test_the_same_signal_is_not_recorded_or_notified_twice(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    rows = prior_rows() + today_rows(42)
    check(eng, NOW, rows)
    assert check(eng, NOW + 20, rows) == []
    assert check(sh.ShadowEngine(store), NOW + 40, rows) == []                       # 重啟後重新偵測也不重複
    assert len(store.rows()) == 3 and len(rec.calls) == 1


def test_old_signals_found_after_a_restart_are_recorded_but_not_notified(env):
    store, rec = env
    late = START + (M0 + 30) * 60
    check(sh.ShadowEngine(store), late, prior_rows() + today_rows(M0 + 29))
    assert len(store.rows()) == 3 and rec.calls == []


def test_notify_setting(env, monkeypatch):
    assert sh.notify_points() == {50}
    for raw, want in [("off", set()), ("", set()), ("30,40,50", {30, 40, 50}), ("40, 99", {40}), ("abc", set()), ("30", {30})]:
        monkeypatch.setenv("SHADOW_NOTIFY", raw)
        assert sh.notify_points() == want, raw
    store, rec = env
    monkeypatch.setenv("SHADOW_NOTIFY", "30,40,50")
    check(sh.ShadowEngine(store), NOW, prior_rows() + today_rows(42))
    assert len(rec.calls) == 1 and "+60 點" in rec.calls[0]["title"]                  # 同一分鐘三個變體只通知一則
    assert "≥ 50 點" in rec.calls[0]["message"]                                        # 而且是達到的最高門檻那個
    store2, rec2 = sh.SignalStore(store.path.with_name("b.db")), Rec()
    sh.notify = rec2
    monkeypatch.setenv("SHADOW_NOTIFY", "off")
    check(sh.ShadowEngine(store2), NOW, prior_rows() + today_rows(42))
    assert len(store2.rows()) == 3 and rec2.calls == []


def test_a_smaller_move_only_triggers_the_lower_thresholds(env):
    store, rec = env
    new = check(sh.ShadowEngine(store), NOW, prior_rows() + today_rows(42, jump=35.0))
    assert sorted(s["rule"] for s in new) == ["burst30"]                              # 35 點 ≥ 30、< 40
    assert rec.calls == []                                                            # 預設只通知 50


def test_no_signal_without_enough_history_or_volume_surge(env):
    store, rec = env
    assert check(sh.ShadowEngine(store), NOW, prior_rows(PRIOR_DAYS[:9]) + today_rows(42)) == []            # 只有 9 個前面的時段
    rows = prior_rows()
    t = today_rows(42)
    for r in t:
        r["buy_vol"], r["sell_vol"] = 100, 100                                                               # 量沒有放大
    assert check(sh.ShadowEngine(store), NOW, rows + t) == []
    assert store.rows() == [] and rec.calls == []


def test_nothing_is_detected_outside_a_session(env):
    store, rec = env
    sat = int(tpe(2026, 10, 17, 10, 0).timestamp())
    assert check(sh.ShadowEngine(store), sat, prior_rows() + today_rows(42)) == []
    assert store.rows() == []


def test_the_engine_swallows_errors(env):
    store, rec = env

    class Boom:
        def flow_1m(self, *a, **k):
            raise RuntimeError("db locked")

    assert asyncio.run(sh.ShadowEngine(store).check(NOW, Boom())) == []


# ── 訊號之後的 K 棒與結果 ────────────────────────────────────

def test_the_path_after_the_signal_is_filled_and_completed(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    check(eng, NOW, prior_rows() + today_rows(42))
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    assert s["done"] == 0 and s["entry_ts"] == START + 41 * 60 and s["entry_price"] == BASE + 60          # 進場＝下一分鐘的開盤（＝上一根收盤）
    assert len(json.loads(s["path_json"])) == 2                                                          # 第 41、42 分鐘
    later = START + (M0 + 1 + 60) * 60 + 200
    check(eng, later, prior_rows() + today_rows(M0 + 70))
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    assert s["done"] == 1 and len(json.loads(s["path_json"])) == 60 and s["id"]
    assert store.pending() == []


def test_outcomes_follow_the_registered_definition(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    check(eng, START + (M0 + 70) * 60, prior_rows() + today_rows(M0 + 70))
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    o = sh.outcomes_of(s, cost=4.0 / BASE)
    assert set(o) == {5, 15, 30}
    entry = BASE + 60
    # 之後每分鐘多走 1 點：第 41 分鐘收 +1、…、第 41＋H−1 分鐘收 +H（相對第 40 分鐘的收盤）
    for h in (5, 15, 30):
        assert o[h].gross == pytest.approx(h / entry) and o[h].net == pytest.approx(h / entry - 4.0 / BASE)
        assert o[h].mfe >= o[h].gross and o[h].mae >= 0


def test_outcomes_only_count_horizons_the_data_covers(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    check(eng, NOW, prior_rows() + today_rows(42))               # 才走了 2 分鐘
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    assert sh.outcomes_of(s) == {}
    check(eng, START + 60 * 60, prior_rows() + today_rows(M0 + 10))
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    assert set(sh.outcomes_of(s)) == {5}                           # 走了 10 分鐘：只有 5 分鐘那個算（15 還不夠）


def test_minutes_without_trades_keep_the_previous_price(env):
    s = {"ts": 1000 * 60, "entry_price": 100.0, "direction": 1,
         "path_json": json.dumps([[1001 * 60, 100.0, 101.0, 99.0, 100.0, 5], [1004 * 60, 103.0, 104.0, 102.0, 103.0, 5]])}
    a = sh.path_arrays(s)
    assert list(a["c"][:5]) == [100.0, 100.0, 100.0, 103.0, 103.0]                              # 第 1、2 分鐘沒成交 → 沿用 100；第 3 分鐘 103；第 4 分鐘沒成交 → 沿用 103
    assert a["h"][1] == 100.0 and a["l"][1] == 100.0 and a["h"][3] == 104.0                       # 沒成交的分鐘：高低收都是上一個收盤
    assert sh.path_arrays({"ts": 1, "entry_price": None, "path_json": None}) is None
    assert sh.path_arrays({"ts": 1, "entry_price": 1.0, "path_json": "[]"}) is None


# ── 彙總與 API ───────────────────────────────────────────────

def _filled(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    check(eng, START + (M0 + 70) * 60, prior_rows() + today_rows(M0 + 70))
    return store


def test_summary_and_report(env):
    store = _filled(env)
    r = sh.summary(store.rows())
    assert r["total"] == 3 and r["done"] == 3 and set(r["rules"]) == {"burst30", "burst40", "burst50"}
    h = r["rules"]["burst50"]["by_session"]["day"]["horizons"]["15"]
    assert h["n"] == 1 and h["win"] == 1.0 and h["mean_net_pts"] == pytest.approx(15 / (BASE + 60) * BASE - 4.0, rel=1e-3) and h["mean_gross_pts"] > h["mean_net_pts"]
    assert r["rules"]["burst50"]["by_session"]["night"]["horizons"]["15"] == {"n": 0}
    text = sh.format_summary(r)
    assert "50 點變體：1 個訊號" in text and "日盤（1）" in text and "夜盤（0）" in text and "樣本要夠多" in text


def test_bracket_results_from_the_stored_path(env):
    store = _filled(env)
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    entry = BASE + 60
    b = sh.bracket_of(s, 30, 50, cost=4.0 / BASE)
    # 之後每分鐘多走 1 點：停利價 entry×(1＋30/47000)，高點是收盤＋1
    assert b.reason == "tp" and b.gross == pytest.approx(30 / BASE, rel=1e-6) and b.net == pytest.approx(30 / BASE - 4.0 / BASE, rel=1e-6)
    assert b.minutes <= 30
    wide = sh.bracket_of(s, 80, 20)
    assert wide.reason == "time" and wide.minutes == 30 and wide.gross == pytest.approx(30 / entry)      # 停利 80 點碰不到、停損 20 點沒碰到 → 持有滿 30 分鐘，第 30 分鐘的收盤＝entry＋30
    assert sh.bracket_of({"ts": 1, "entry_price": None, "path_json": None, "direction": 1}, 30, 50) is None
    r = sh.summary(store.rows())
    x = r["rules"]["burst50"]["brackets"]["30/50"]
    assert x["n"] == 1 and x["win"] == 1.0 and x["mean_net_pts"] > 0 and set(r["rules"]["burst50"]["brackets"]) == {f"{t}/{s_}" for t in sr.TP_GRID for s_ in sr.SL_GRID}
    assert "50 點變體加停損停利" in sh.format_summary(r)


def test_bracket_uses_the_recorded_open_for_a_gap_through_the_stop():
    t0 = 1000 * 60 + 60
    bars = [[t0 + 60 * k, 47000.0, 47005.0, 46995.0, 47000.0, 5] for k in range(30)]
    bars[3] = [t0 + 180, 46950.0, 46960.0, 46940.0, 46985.0, 5]          # 開盤跳空到 46,950（越過停損價 46,980），收盤 46,985
    s = {"ts": 1000 * 60, "entry_price": 47000.0, "direction": 1, "path_json": json.dumps(bars), "done": 1}
    b = sh.bracket_of(s, 50, 20, cost=0.0)
    assert b.reason == "sl" and b.minutes == 4 and b.gross == pytest.approx(-50 / 47000)     # 用開盤價 46,950 出場（不是停損價 46,980、也不是收盤）
    assert sh.path_arrays(s)["o"][3] == 46950.0


def test_bracket_needs_thirty_minutes_of_data(env):
    store, rec = env
    eng = sh.ShadowEngine(store)
    check(eng, START + (M0 + 10) * 60, prior_rows() + today_rows(M0 + 9))               # 才走了 8 分鐘
    s = [r for r in store.rows() if r["rule"] == "burst50"][0]
    assert sh.bracket_of(s, 30, 50) is None
    assert sh.summary(store.rows())["rules"]["burst50"]["brackets"]["30/50"] == {"n": 0}


def test_api_returns_summary_and_recent(env, monkeypatch):
    store = _filled(env)
    monkeypatch.setattr(sh, "_default_store", store)
    out = asyncio.run(rs.shadow(limit=2))
    assert out["enabled"] is True and out["cost_pts"] == 4.0 and out["summary"]["total"] == 3
    assert len(out["recent"]) == 2 and out["recent"][0]["rule"] in ("burst30", "burst40", "burst50") and out["recent"][0]["time"].startswith("2026-10-14T09:25")
    assert "path_json" not in out["recent"][0] and out["recent"][0]["done"] is True and out["recent"][0]["move_pts"] == 60.0


# ── 儲存與輔助 ───────────────────────────────────────────────

def test_store_roundtrip_and_uniqueness(tmp_path):
    st = sh.SignalStore(tmp_path / "x" / "s.db")
    assert st.add("burst30", 100, "day", 1, 0.002, 2.5, 47000.0) is True
    assert st.add("burst30", 100, "day", 1, 0.002, 2.5, 47000.0) is False             # 同一個 (rule, ts)
    assert st.add("burst40", 100, "day", 1, 0.002, 2.5, 47000.0) is True               # 不同變體同一分鐘可以
    assert len(st.rows()) == 2
    sid = st.pending()[0]["id"]
    st.set_path(sid, 160, 47001.0, [[160, 47001.0, 47002.0, 47000.0, 47001.0, 3]], True)
    assert len(st.pending()) == 1 and sid not in [r["id"] for r in st.pending()]                 # 完成的不再是 pending，另一個還是
    assert st.rows(limit=1)[0]["ts"] == 100 and len(st.rows(since_ts=101)) == 0


def test_session_start_ts_and_array_building():
    assert sh.session_start_ts("day", "2026-10-14") == int(tpe(2026, 10, 14, 8, 45).timestamp())
    assert sh.session_start_ts("night", "2026-10-14") == int(tpe(2026, 10, 14, 15, 0).timestamp())
    rows = [row(START, 1.0, 2.0, 0.5, 1.5, 10), row(START + 60, 1.5, 3.0, 1.0, 2.0, 7), {"ts": START + 120, "open": 0.0, "high": 1, "low": 1, "close": 1, "buy_vol": 1}]
    s = sh.sessions_from_flow(rows)
    assert s["day"][TODAY] == {0: (1.0, 2.0, 0.5, 1.5, 10.0), 1: (1.5, 3.0, 1.0, 2.0, 7.0)} and s["night"] == {}
    a = sh.to_arrays(s["day"][TODAY], 300)
    assert a["c"][0] == 1.5 and a["v"][1] == 7.0 and np.isnan(a["c"][2])


def test_message_for_a_down_move(env):
    t, b = sh.message({"direction": -1, "move": -55 / BASE, "ratio": 3.04, "ts": int(tpe(2026, 10, 14, 21, 30).timestamp()), "session": "night", "points": 50, "close": 46_900.0})
    assert "急跌" in t and "-55 點" in t and "3.0 倍" in t and "夜盤 21:30" in b and "46,900" in b


def test_loop_is_off_when_disabled(monkeypatch, env):
    monkeypatch.setenv("SIGNAL_SHADOW", "false")
    asyncio.run(asyncio.wait_for(sh.shadow_loop(FakeStore([])), timeout=3))


@pytest.mark.parametrize("name", ["core/signal_rules.py", "core/signal_study.py", "core/signal_shadow.py", "api/routes_signals.py"])
def test_the_signal_modules_cannot_place_orders(name):
    src = (ROOT / name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
    assert not ({"broker", "core.broker", "manual_monitor", "strategy_engine"} & imported), imported
    assert "place_order" not in src and "cancel_order" not in src
