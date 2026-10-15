"""持倉中波動放大提醒（update.md 發現 37）：時段對照、基準（只用當天以前）、掃到機率內插、判斷、訊息、背景檢查。只通知，測試用假的 notify，不碰網路。"""
from __future__ import annotations

import ast
import asyncio
import math
import statistics
from datetime import datetime
from pathlib import Path

import pytest

import core.vol_watch as vw
from core import stop_suggest
from core.daytrade import TAIPEI

ROOT = Path(__file__).resolve().parent.parent
TABLE = {"ks": [0.5, 1.0, 1.5, 2.0, 3.0],
         "day": {"table": {"30": {"touch": [0.57, 0.27, 0.12, 0.05, 0.013]}}}}


def tpe(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TAIPEI)


def alt(amp, n=100, base=20000.0):
    """交替 +amp／0 的收盤價：每個變動的絕對值都是 amp，σ（樣本標準差）可以獨立算出來。"""
    return {i: base + (amp if i % 2 else 0.0) for i in range(n)}


def expected_sigma(amp, n_changes=30):
    ch = [amp if i % 2 == 0 else -amp for i in range(n_changes)]
    return statistics.stdev(ch)


def live_rows(now_ts, amp, n=31):
    last_done = int(now_ts // 60) * 60 - 60
    return [{"ts": last_done - 60 * i, "close": 20000.0 + (amp if i % 2 else 0.0)} for i in range(n - 1, -1, -1)]


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(vw, "notify", r)
    monkeypatch.delenv("VOL_ALERT_RATIO", raising=False)
    monkeypatch.delenv("VOL_WATCH", raising=False)
    return r


# ── 時段 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("t,expect", [
    (tpe(2026, 10, 14, 8, 44), None),
    (tpe(2026, 10, 14, 8, 45), ("day", "2026-10-14", 0)),
    (tpe(2026, 10, 14, 13, 44), ("day", "2026-10-14", 299)),
    (tpe(2026, 10, 14, 13, 45), None),
    (tpe(2026, 10, 14, 14, 59), None),
    (tpe(2026, 10, 14, 15, 0), ("night", "2026-10-14", 0)),
    (tpe(2026, 10, 14, 23, 59), ("night", "2026-10-14", 539)),
    (tpe(2026, 10, 15, 0, 0), ("night", "2026-10-14", 540)),            # 午夜之後還是前一天的夜盤
    (tpe(2026, 10, 15, 4, 59), ("night", "2026-10-14", 839)),
    (tpe(2026, 10, 15, 5, 0), None),
    (tpe(2026, 10, 17, 3, 0), ("night", "2026-10-16", 720)),            # 週六凌晨＝週五夜盤
    (tpe(2026, 10, 17, 16, 0), None),                                     # 週六下午沒有夜盤
    (tpe(2026, 10, 18, 10, 0), None),                                     # 週日
    (tpe(2026, 10, 19, 3, 0), None),                                      # 週一凌晨：週日沒有夜盤
    (tpe(2026, 10, 17, 10, 0), None),                                     # 週六沒有日盤
])
def test_session_pos(t, expect):
    assert vw.session_pos(t) == expect


def test_bars_and_live_minutes_use_the_same_index():
    """bars_1m 的 hhmm 是 K 棒結束時間（10:30 結束的那根涵蓋 10:29~10:30）；即時資料的分鐘序號用起點。兩邊要對到同一格，差一格基準就錯位。"""
    bars = vw.day_sessions([{"date": "2026-10-14", "hhmm": 1030, "close": 1.0}])
    live = vw.session_pos(tpe(2026, 10, 14, 10, 29))
    assert list(bars["2026-10-14"]) == [live[2]] == [104]


def test_day_sessions_edges_and_missing():
    rows = [{"date": "d", "hhmm": 845, "close": 1.0}, {"date": "d", "hhmm": 846, "close": 2.0}, {"date": "d", "hhmm": 1345, "close": 3.0},
            {"date": "d", "hhmm": 1346, "close": 4.0}, {"date": "d", "hhmm": 900, "close": None}, {"date": "d", "hhmm": "x", "close": 5.0}]
    assert vw.day_sessions(rows) == {"d": {0: 2.0, 299: 3.0}}


def test_night_sessions_split_across_midnight_and_ignore_day_rows():
    rows = [{"ts": tpe(2026, 10, 14, 15, 0).timestamp(), "close": 1.0}, {"ts": tpe(2026, 10, 15, 0, 30).timestamp(), "close": 2.0},
            {"ts": tpe(2026, 10, 14, 10, 0).timestamp(), "close": 9.0}, {"ts": tpe(2026, 10, 15, 6, 0).timestamp(), "close": 8.0}]
    assert vw.night_sessions(rows) == {"2026-10-14": {0: 1.0, 570: 2.0}}


# ── 基準 ─────────────────────────────────────────────────────

def test_sigma_at_matches_the_stop_card_definition():
    closes = alt(2.0)
    assert vw.sigma_at(closes, 40) == pytest.approx(expected_sigma(2.0))
    assert vw.sigma_at(closes, 40) == pytest.approx(stop_suggest.sigma_from_closes([closes[i] for i in range(10, 41)])[0])
    assert vw.sigma_at(closes, 20) is None                   # 前面不夠 30 分鐘 → 25 個有效變動都湊不到


def _sessions(n, amp, start="2026-09-%02d"):
    return {start % (d + 1): alt(amp) for d in range(n)}


def test_baseline_is_the_median_across_sessions():
    s = _sessions(12, 2.0)
    for k in list(s)[:5]:                                     # 5 個時段的波動大一點：中位數還是 2.0 那組
        s[k] = alt(4.0)
    b = vw.build_baseline(s, "2026-10-14")
    assert b[40] == pytest.approx(expected_sigma(2.0))
    assert min(b) == 30 and max(b) == 99


def test_baseline_never_peeks_at_today_or_later():
    """金絲雀：同一天與之後的時段就算波動超大，也不能進基準。"""
    s = _sessions(12, 2.0)
    s["2026-10-14"] = alt(50.0)
    s["2026-10-20"] = alt(80.0)
    assert vw.build_baseline(s, "2026-10-14")[40] == pytest.approx(expected_sigma(2.0))


def test_baseline_uses_only_the_latest_n_sessions():
    s = {f"2026-07-{d:02d}": alt(100.0) for d in range(1, 13)}          # 12 個舊的大波動：取最舊的 20 個就會讓中位數變 100
    s.update({f"2026-09-{d:02d}": alt(2.0) for d in range(1, 21)})
    assert vw.build_baseline(s, "2026-10-14")[40] == pytest.approx(expected_sigma(2.0))


def test_today_and_later_sessions_do_not_count_toward_the_minimum():
    s = _sessions(vw.MIN_SESSIONS - 1, 2.0)
    s["2026-10-14"] = alt(2.0)                                 # 今天自己
    s["2026-10-20"] = alt(2.0)                                 # 之後
    assert vw.build_baseline(s, "2026-10-14") == {}


def test_baseline_needs_enough_sessions():
    assert vw.build_baseline(_sessions(vw.MIN_SESSIONS - 1, 2.0), "2026-10-14") == {}
    assert 40 in vw.build_baseline(_sessions(vw.MIN_SESSIONS, 2.0), "2026-10-14")


def test_typical_sigma():
    assert vw.typical_sigma({}) is None
    assert vw.typical_sigma({1: 1.0, 2: 3.0, 3: 2.0}) == 2.0


# ── 掃到機率 ─────────────────────────────────────────────────

@pytest.mark.parametrize("k,expect", [(1.0, (0.27, "≈")), (1.25, (0.195, "≈")), (0.25, (0.785, "≈")), (0.0, (1.0, "≈")), (3.0, (0.013, "≈")), (5.0, (0.013, "≤"))])
def test_touch_prob_interpolates(k, expect):
    p, kind = vw.touch_prob(TABLE, "day", 30, k)
    assert (p, kind) == (pytest.approx(expect[0]), expect[1])


def test_touch_prob_without_table():
    assert vw.touch_prob(None, "day", 30, 1.0) is None
    assert vw.touch_prob(TABLE, "night", 30, 1.0) is None
    assert vw.touch_prob(TABLE, "day", 15, 1.0) is None
    assert vw.touch_prob(TABLE, "day", 30, float("nan")) is None


def test_real_calibration_table_is_usable():
    t = stop_suggest.load_table()
    p, kind = vw.touch_prob(t, "day", vw.HORIZON, 2.0)
    assert kind == "≈" and 0.04 < p < 0.07                      # 實測 5.4%


# ── 判斷 ─────────────────────────────────────────────────────

POS = [{"code": "TMFJ6", "direction": "Buy", "quantity": 2, "price": 20000.0, "last_price": 20000.0}]
WATCH = [{"match_code": "TMF", "direction": "Buy", "seen": True, "is_option": False, "entry_price": 20000.0, "stop_loss_pts": 40}]
NOW = tpe(2026, 10, 14, 10, 30, 20)                              # 日盤第 105 分鐘
IDX = 105


def assess(amp=3.0, ref=1.0, positions=POS, watches=WATCH, idx=IDX, rows=None, **kw):
    ts = NOW.timestamp()
    return vw.assess(ts, live_rows(ts, amp) if rows is None else rows, {idx - 1: ref} if ref is not None else {}, "day", idx, positions, watches, TABLE, **kw)


def test_alert_when_ratio_reaches_the_threshold():
    a = assess(amp=3.0, ref=expected_sigma(1.5))                 # 比值 2.0
    assert a["ratio"] == pytest.approx(2.0) and a["alert"] is True
    b = assess(amp=3.0, ref=expected_sigma(1.5) * 1.001)         # 差一點點 → 不響
    assert b["alert"] is False and b["stops"] == []


def test_the_threshold_itself_alerts():
    """邊界：比值剛好等於門檻就響（≥）。"""
    r = assess(amp=3.0, ref=expected_sigma(1.0))["ratio"]
    assert assess(amp=3.0, ref=expected_sigma(1.0), ratio_th=r)["alert"] is True
    assert assess(amp=3.0, ref=expected_sigma(1.0), ratio_th=r * 1.0000001)["alert"] is False


def test_ratio_threshold_from_env(monkeypatch):
    assert assess(amp=3.0, ref=expected_sigma(1.0))["alert"] is True                 # 3×
    monkeypatch.setenv("VOL_ALERT_RATIO", "4")
    assert assess(amp=3.0, ref=expected_sigma(1.0))["alert"] is False
    monkeypatch.setenv("VOL_ALERT_RATIO", "0.5")                                       # 不合理的值退回 2.0
    assert vw.ratio_threshold() == 2.0
    monkeypatch.setenv("VOL_ALERT_RATIO", "abc")
    assert vw.ratio_threshold() == 2.0


@pytest.mark.parametrize("pos", [[], None, [{"code": "TXO20000J6", "direction": "Buy", "quantity": 1}], [{"code": "TMFJ6", "direction": "Buy", "quantity": 0}]])
def test_flat_still_alerts_with_the_scale_but_no_stop_lines(pos):
    """空手（或只有選擇權）也提醒（發現 39）：訊息講現在的尺度與建議的停利停損點數，不列「離停損還有幾點」。"""
    a = assess(amp=3.0, ref=expected_sigma(1.0), positions=pos)
    assert a["alert"] is True and a["held"] == [] and a["stops"] == []
    title, body = vw.message(a)
    assert "3.0 倍" in title
    assert "目前沒有期貨持倉" in body and "如果現在要進場" in body and "不會動任何委託" in body
    assert "平衡型停利" in body and "（平常是" in body
    assert "離停損" not in body and "TMFJ6" not in body and "停損的意義變了" not in body


def test_balanced_row_is_the_stop_card_row():
    assert vw._balanced(2.0, "day", 30) == (11, 16)               # 2.0 × √30 = 10.95：停利 1.0×、停損 1.5×
    a = assess(amp=3.0, ref=expected_sigma(1.0))
    assert a["suggest"]["now"] == vw._balanced(a["sigma"], "day", 30) and a["suggest"]["usual"] == vw._balanced(a["ref"], "day", 30)
    assert a["suggest"]["now"][1] > a["suggest"]["usual"][1]       # 波動放大 → 建議停損變遠


def test_no_decision_without_sigma_or_baseline():
    few = live_rows(NOW.timestamp(), 3.0, n=10)
    a = assess(rows=few)
    assert a["ratio"] is None and a["alert"] is False
    b = assess(ref=None)
    assert b["ratio"] is None and b["alert"] is False
    c = assess(idx=0)
    assert c["alert"] is False


def test_stops_listed_only_when_alerting():
    a = assess(amp=3.0, ref=expected_sigma(1.0))
    assert [s["code"] for s in a["stops"]] == ["TMFJ6"]
    assert a["stops"][0]["stop"] == 40


# ── 停損的暴露程度 ───────────────────────────────────────────

def exposure(entry=20000.0, last=20000.0, watches=None, sigma=2.0, ref=1.0, sl=40):
    pos = [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": entry, "last_price": last}]
    w = [{"match_code": "TMF", "direction": "Buy", "seen": True, "is_option": False, "entry_price": entry, "stop_loss_pts": sl}] if watches is None else watches
    return vw.stop_exposure(pos, w, sigma, ref, 30, "day", TABLE)[0]


def test_exposure_in_units_of_noise():
    e = exposure()
    rt = math.sqrt(30)
    assert e["dist"] == 40 and e["k_now"] == pytest.approx(40 / (2.0 * rt)) and e["k_ref"] == pytest.approx(40 / (1.0 * rt))
    assert e["p_now"][1] == "≤"                                  # 3.65σ：超出表的最大格點
    assert e["p_ref"][1] == "≤"


def test_distance_to_stop_depends_on_where_price_is_now():
    assert exposure(entry=20000, last=20010)["dist"] == 50       # 已經賺 10 點：離停損多 10 點
    assert exposure(entry=20000, last=19990)["dist"] == 30
    sell = vw.stop_exposure([{"code": "TMFJ6", "direction": "Sell", "quantity": 1, "price": 20000, "last_price": 19990}],
                            [{"match_code": "TMF", "direction": "Sell", "seen": True, "is_option": False, "entry_price": 20000, "stop_loss_pts": 40}],
                            2.0, 1.0, 30, "day", TABLE)[0]
    assert sell["dist"] == 50                                    # 空單價格下跌＝賺


def test_exposure_when_no_watch_or_not_matching():
    assert exposure(watches=[])["stop"] is None
    other_dir = [{"match_code": "TMF", "direction": "Sell", "seen": True, "is_option": False, "entry_price": 1, "stop_loss_pts": 40}]
    assert exposure(watches=other_dir)["stop"] is None
    unseen = [{"match_code": "TMF", "direction": "Buy", "seen": False, "is_option": False, "entry_price": 1, "stop_loss_pts": 40}]
    assert exposure(watches=unseen)["stop"] is None
    nostop = [{"match_code": "TMF", "direction": "Buy", "seen": True, "is_option": False, "entry_price": 1, "stop_loss_pts": 0}]
    assert exposure(watches=nostop)["stop"] is None
    option = [{"match_code": "TMF", "direction": "Buy", "seen": True, "is_option": True, "entry_price": 1, "stop_loss_pts": 40}]
    assert exposure(watches=option)["stop"] is None


def test_exposure_without_a_price_or_already_past_the_stop():
    pos = [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 0.0, "last_price": 0.0}]
    w = [{"match_code": "TMF", "direction": "Buy", "seen": True, "is_option": False, "entry_price": 20000.0, "stop_loss_pts": 40}]
    e = vw.stop_exposure(pos, w, 2.0, 1.0, 30, "day", TABLE)[0]
    assert e["stop"] == 40 and e["dist"] is None and "k_now" not in e
    assert "取不到最新價" in vw.message({"sigma": 2.0, "ref": 1.0, "ratio": 2.0, "horizon": 30, "stops": [e]})[1]
    e = exposure(last=0.0)                                       # 沒有 last_price 但有均價：用均價
    assert e["dist"] == 40
    e = exposure(entry=20000, last=19950)                        # 已經虧 50 點、停損 40
    assert e["dist"] == -10 and "k_now" not in e


def test_message_content():
    a = assess(amp=3.0, ref=expected_sigma(1.0))
    title, body = vw.message(a)
    assert "3.0 倍" in title
    assert "TMFJ6 多 2 口" in body and "停損 40 點" in body and "離停損還有 40 點" in body
    assert "不會動任何委託" in body and "停損的意義變了" in body and "以現在的波動，平衡型停利" in body
    b = assess(amp=3.0, ref=expected_sigma(1.0), watches=[])
    assert "沒有停損監控" in vw.message(b)[1]


# ── 背景檢查 ─────────────────────────────────────────────────

def _day_rows(dates):
    rows = []
    for d in dates:
        for i in range(300):
            total = 525 + i + 1
            rows.append({"date": d, "hhmm": (total // 60) * 100 + total % 60, "close": 20000.0 + (1.0 if i % 2 else 0.0)})
    return rows


DATES = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
         "2026-10-09", "2026-10-12", "2026-10-13"]


class FakeStore:
    def __init__(self, amp_live=3.0, boom=False):
        self.day, self.amp, self.boom = _day_rows(DATES), amp_live, boom
        self.bars_calls = self.flow_calls = 0

    def bars_1m(self, code, since=None):
        self.bars_calls += 1
        return self.day

    def flow_1m(self, code, since_ts=None, until_ts=None):
        self.flow_calls += 1
        if self.boom:
            raise RuntimeError("db locked")
        return live_rows(NOW.timestamp(), self.amp)


def run(coro):
    return asyncio.run(coro)


def test_watcher_notifies_when_elevated(rec):
    store, w = FakeStore(3.0), vw.VolWatcher()
    a = run(w.check(NOW.timestamp(), POS, WATCH, store, table=TABLE))
    assert a["alert"] and a["ratio"] == pytest.approx(3.0)
    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["key"] == "volwatch:day" and c["cooldown"] == vw.COOLDOWN_SEC and c["priority"] == "high"
    assert "TMFJ6" in c["message"]


def test_watcher_quiet_when_not_elevated(rec):
    a = run(vw.VolWatcher().check(NOW.timestamp(), POS, WATCH, FakeStore(1.0), table=TABLE))
    assert a["alert"] is False and a["ratio"] == pytest.approx(1.0)
    assert rec.calls == []


def test_watcher_builds_the_baseline_once_per_session(rec):
    store, w = FakeStore(1.0), vw.VolWatcher()
    run(w.check(NOW.timestamp(), POS, WATCH, store, table=TABLE))
    run(w.check(NOW.timestamp() + 60, POS, WATCH, store, table=TABLE))
    assert store.bars_calls == 1 and store.flow_calls == 2


class Spy:
    """記錄有沒有被讀（watcher 會吞例外，所以不能靠『讀了會丟例外』來測）。"""

    def __init__(self):
        self.calls = []

    def bars_1m(self, *a, **k):
        self.calls.append("bars_1m")
        return []

    def flow_1m(self, *a, **k):
        self.calls.append("flow_1m")
        return []


def test_watcher_alerts_even_when_flat(rec):
    """空手也發（發現 39）：同樣的條件、同一個冷卻 key。"""
    store = FakeStore(3.0)
    a = run(vw.VolWatcher().check(NOW.timestamp(), [], WATCH, store, table=TABLE))
    assert a["alert"] is True and a["held"] == []
    assert len(rec.calls) == 1 and rec.calls[0]["key"] == "volwatch:day" and "目前沒有期貨持倉" in rec.calls[0]["message"]


def test_watcher_flat_and_quiet_says_nothing(rec):
    a = run(vw.VolWatcher().check(NOW.timestamp(), [], [], FakeStore(1.0), table=TABLE))
    assert a["alert"] is False and rec.calls == []


def test_watcher_outside_a_session_does_nothing(rec):
    spy = Spy()
    sat = tpe(2026, 10, 17, 10, 0).timestamp()
    assert run(vw.VolWatcher().check(sat, POS, WATCH, spy, table=TABLE)) is None
    assert spy.calls == []


def test_watcher_swallows_errors(rec):
    assert run(vw.VolWatcher().check(NOW.timestamp(), POS, WATCH, FakeStore(boom=True), table=TABLE)) is None
    assert rec.calls == []


def test_notify_failure_does_not_escape(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("push broke")
    monkeypatch.setattr(vw, "notify", boom)
    assert run(vw.VolWatcher().check(NOW.timestamp(), POS, WATCH, FakeStore(3.0), table=TABLE)) is None


def test_loop_is_off_when_disabled(monkeypatch, rec):
    monkeypatch.setenv("VOL_WATCH", "false")
    run(asyncio.wait_for(vw.vol_watch_loop(lambda: POS, lambda: WATCH, FakeStore()), timeout=3))          # 直接返回，不進無限迴圈
    assert rec.calls == []


# ── 純顯示：不能碰下單 ───────────────────────────────────────

@pytest.mark.parametrize("name", ["core/vol_watch.py", "core/daily_brief.py"])
def test_module_cannot_place_orders(name):
    tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0] + "." + (n.module or "").split(".", 1)[-1])
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not any(x in imported for x in ("broker", "core.broker", "manual_monitor", "strategy_engine")), imported
    assert "place_order" not in text and "cancel_order" not in text
