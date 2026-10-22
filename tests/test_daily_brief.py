"""盤前摘要（update.md 發現 37）：組訊息、時間窗、一天一則、取不到的欄位不影響其他。只通知，測試用假的 notify、假的資料庫與日曆，不碰網路與真實資料庫。"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import core.daily_brief as db
from core.daytrade import TAIPEI


def tpe(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TAIPEI)


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message, *, key=None, cooldown=0.0, priority="high", wait=False):
        self.calls.append({"title": title, "message": message, "key": key, "cooldown": cooldown, "priority": priority})
        return True


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(db, "notify", r)
    monkeypatch.setattr(db, "sim_blocked", lambda: False)
    monkeypatch.delenv("DAILY_BRIEF_TIME", raising=False)
    monkeypatch.delenv("DAILY_BRIEF", raising=False)
    return r


STATE = {"ready": True, "hurst": {"value": 0.52, "label": "隨機漫步", "z": 0.3}, "iv": {"percentile": 41.0, "label": "正常"}, "direction_label": "偏多"}
NIGHT = {"value": -0.0042, "missing_reason": None, "detail": {"night_close": 48500.0, "prev_close": 48705.0}}


def data(**kw):
    d = {"date": "2026-10-14", "weekday": "三", "state": STATE, "night": NIGHT, "r20": 312.4, "sigma": 4.2, "n_sessions": 20,
         "table": None, "last_trading_day": False, "errors": []}
    d.update(kw)
    return d


# ── 組訊息 ───────────────────────────────────────────────────

DAY_IV = {"expect": {"lo": 180.0, "mid": 240.0, "hi": 330.0}, "r20": 300.0, "vix": 22.4, "vix_date": "2026-10-13", "atm_equiv": 20.8, "n": 1400}
CPI = {"kind": "CPI", "label": "美國 CPI", "when": "今晚 20:30", "time": 0.0, "note": "歷史上隔天日盤開盤跳空約平常的 1.6 倍"}


def test_compose_day_range_and_events():
    _, body = db.compose(data(day_iv=DAY_IV, events=[CPI]))
    assert "預期日盤振幅：約 240 點（68% 落在 180～330；臺指 VIX 22.4，10-13）" in body
    assert "📅 今晚 20:30 美國 CPI：歷史上隔天日盤開盤跳空約平常的 1.6 倍" in body
    _, body = db.compose(data(day_iv={"note": "還沒有前一天的臺指選擇權波動率指數"}, events=[]))
    assert "預期日盤振幅：還沒有前一天的臺指選擇權波動率指數" in body and "📅" not in body and "事件" not in body
    _, body = db.compose(data(events=None))
    assert "預期日盤振幅：取不到" in body and "事件：讀不到 lh-intel 的事件行事曆" in body


def test_compose_full():
    title, body = db.compose(data())
    assert title == "📋 盤前摘要 10/14（三）"
    assert "夜盤 TXF：-0.42%（收 48,500，前一日日盤收 48,705）" in body
    assert "Hurst 0.52（隨機漫步，z=+0.3）" in body and "IV 百分位 41%（正常）" in body and "方向 偏多" in body
    assert "R20 ≈ 312 點" in body
    assert "典型 σ ≈ 4.2 點／分鐘" in body and "30 分鐘雜訊範圍約 23 點" in body
    assert "平衡型停利" in body and "1 口微台停損 NT$" in body
    assert "占權益" not in body and "權益" not in body              # 通知通道是第三方服務：不放權益（含間接洩漏的百分比）
    assert "不是進場訊號" in body and "最後交易日" not in body


def test_compose_balanced_row_matches_the_stop_card():
    from core import stop_suggest
    row = next(r for r in stop_suggest.build(4.2, 30, session="day", horizon=30, contract="TMF", equity=None, table=None)["rows"] if r["name"] == "平衡")
    body = db.compose(data())[1]
    assert f"停利 {row['tp']}／停損 {row['sl']} 點" in body and f"NT${row['loss_twd']:,}" in body


def test_compose_missing_pieces_are_named_not_hidden():
    _, body = db.compose(data(night={"value": None, "missing_reason": "no_ticks: 夜盤沒有 TXF 成交紀錄（後端那段時間沒開？）"}, state=None, r20=None, sigma=None,
                              errors=["R20：OperationalError"]))
    assert "夜盤 TXF：沒有資料（no_ticks" in body
    assert "市場狀態：還沒算出來" in body
    assert "典型 σ：取不到" in body and "R20" in body.split("取不到：")[-1]
    assert "R20 ≈" not in body


def test_compose_on_the_last_trading_day():
    _, body = db.compose(data(last_trading_day=True))
    assert "📅 今天是 10 月合約最後交易日（13:30 收盤）" in body


def test_compose_state_not_ready():
    assert "還沒算出來" in db.compose(data(state={"ready": False}))[1]


def test_brief_minute_env(monkeypatch):
    monkeypatch.delenv("DAILY_BRIEF_TIME", raising=False)
    assert db.brief_minute() == 8 * 60 + 35
    for raw, expect in [("0900", 540), ("8:40", 520), ("0835", 515), ("2560", 515), ("abc", 515), ("", 515)]:
        monkeypatch.setenv("DAILY_BRIEF_TIME", raw)
        assert db.brief_minute() == expect, raw


# ── 取資料 ───────────────────────────────────────────────────

class Store:
    def __init__(self, rows=None, boom=False):
        self.rows, self.boom = rows or [], boom

    def bars_1m(self, code, since=None):
        if self.boom:
            raise RuntimeError("db locked")
        return [r for r in self.rows if since is None or r["date"] >= since]


def _bars(dates, amp=1.0):
    out = []
    for d in dates:
        for i in range(300):
            total = 525 + i + 1
            out.append({"date": d, "hhmm": (total // 60) * 100 + total % 60, "open": 20000.0, "high": 20000.0 + amp + 1, "low": 19999.0, "close": 20000.0 + (amp if i % 2 else 0.0), "volume": 10})
    return out


DATES = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
         "2026-10-09", "2026-10-12", "2026-10-13"]
NO_TICKS = Path("/nonexistent/ticks.db")


def test_gather_collects_what_it_can():
    d = db.gather(tpe(2026, 10, 14, 8, 35), Store(_bars(DATES)), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["date"] == "2026-10-14" and d["weekday"] == "三"
    assert d["sigma"] == pytest.approx(1.0173, rel=1e-3)             # 交替 ±1 的 30 個變動
    assert d["n_sessions"] == 12
    assert d["night"]["value"] is None and d["night"]["missing_reason"]       # 沒有 ticks.db：寫原因、不丟例外
    assert d["last_trading_day"] is False and d["errors"] == []
    assert d["events"] is None                                     # 測試沒有 lh-intel：事件讀不到是 None（訊息寫「讀不到」），不丟例外
    assert d["day_iv"] is not None


def test_gather_takes_events_from_precheck_at_now(monkeypatch):
    from core import precheck
    seen = []
    monkeypatch.setattr(precheck, "fetch_events", lambda t: seen.append(t) or [CPI])
    now = tpe(2026, 10, 14, 8, 35)
    d = db.gather(now, Store(_bars(DATES)), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["events"] == [CPI] and seen == [now.timestamp()]


def test_gather_sigma_does_not_use_todays_session():
    """今天（還沒走完）的資料不能進典型 σ：前面只有 9 個時段，加上今天湊成 10 個也不能算數。"""
    rows = _bars(DATES[:9]) + _bars(["2026-10-14"], amp=50.0)
    d = db.gather(tpe(2026, 10, 14, 8, 35), Store(rows), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["sigma"] is None and d["n_sessions"] == 9


def test_gather_r20_is_the_mean_range_of_the_previous_20_sessions():
    dates = [(date(2026, 8, 3) + timedelta(days=i)).isoformat() for i in range(25)]
    rows = _bars(dates, amp=1.0)                                      # 每個時段 高 20002、低 19999 → 振幅 3
    rows += _bars(["2026-10-14"], amp=500.0)                          # 今天自己的（還沒走完）不能進 R20
    d = db.gather(tpe(2026, 10, 14, 8, 35), Store(rows), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["r20"] == pytest.approx(3.0)


def test_iv_not_ready_is_shown():
    s = dict(STATE, iv={"percentile": None, "label": "累積中 3/60"})
    assert "IV 百分位 —（累積中 3/60）" in db.compose(data(state=s))[1]


def test_gather_marks_last_trading_day():
    d = db.gather(tpe(2026, 10, 21, 8, 35), Store(_bars(DATES)), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["last_trading_day"] is True


def test_gather_survives_a_broken_database():
    d = db.gather(tpe(2026, 10, 14, 8, 35), Store(boom=True), STATE, lambda x: True, lambda x: x - timedelta(days=1), ticks_path=NO_TICKS)
    assert d["sigma"] is None and d["r20"] is None
    assert any("典型 σ" in e for e in d["errors"]) and any("R20" in e for e in d["errors"])
    title, body = db.compose(d)                                      # 壞掉的部分有寫出來，其他照給
    assert "取不到：" in body and title.startswith("📋")


# ── 時間窗與一天一則 ─────────────────────────────────────────

def _run(brief, now, store=None, is_open=lambda d: True):
    return asyncio.run(brief.run_once(now, lambda: STATE, store or Store(_bars(DATES)), is_open, lambda d: d - timedelta(days=1)))


@pytest.fixture(autouse=True)
def _no_real_ticks(monkeypatch):
    """gather 預設會讀本機 ticks.db 與期交所資料；測試沒指定就一律指到不存在的路徑（不碰真實資料）。"""
    real = db.gather
    monkeypatch.setattr(db, "gather", lambda *a, **k: real(*a, **{"ticks_path": NO_TICKS, "tx_dir": Path("/nonexistent/tx"), **k}))


def test_sends_once_at_the_time(rec):
    b = db.DailyBrief()
    assert _run(b, tpe(2026, 10, 14, 8, 34)) is None and rec.calls == []
    assert _run(b, tpe(2026, 10, 14, 8, 35)) == "sent"
    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["key"] == "brief:2026-10-14" and c["cooldown"] == 20 * 3600.0 and c["priority"] == "default"
    assert "權益" not in c["message"]
    assert _run(b, tpe(2026, 10, 14, 8, 40)) is None and len(rec.calls) == 1    # 一天一則


def test_late_start_still_sends_until_the_window_ends(rec):
    assert _run(db.DailyBrief(), tpe(2026, 10, 14, 9, 29)) == "sent"
    b = db.DailyBrief()
    assert _run(b, tpe(2026, 10, 14, 9, 30)) is None and len(rec.calls) == 1    # 開盤後一陣子就不補了


def test_not_on_weekends_or_closed_days(rec):
    b = db.DailyBrief()
    assert _run(b, tpe(2026, 10, 17, 8, 40)) is None                              # 週六
    assert _run(b, tpe(2026, 10, 14, 8, 40), is_open=lambda d: False) == "closed"
    assert rec.calls == []
    assert _run(b, tpe(2026, 10, 14, 8, 41)) is None                              # 休市日判斷過就不再看


def test_not_in_simulation_mode(monkeypatch, rec):
    monkeypatch.setattr(db, "sim_blocked", lambda: True)
    assert _run(db.DailyBrief(), tpe(2026, 10, 14, 8, 40)) == "sim"
    assert rec.calls == []


def test_custom_time(monkeypatch, rec):
    monkeypatch.setenv("DAILY_BRIEF_TIME", "0900")
    b = db.DailyBrief()
    assert _run(b, tpe(2026, 10, 14, 8, 40)) is None
    assert _run(b, tpe(2026, 10, 14, 9, 0)) == "sent"


def test_loop_is_off_when_disabled(monkeypatch, rec):
    monkeypatch.setenv("DAILY_BRIEF", "false")
    asyncio.run(asyncio.wait_for(db.brief_loop(lambda: STATE, Store()), timeout=3))        # 直接返回
    assert rec.calls == []


# ── 換月後第一天的夜盤報酬（發現 65）─────────────────────────────

TX_HEAD = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量"


def tx_file(d: Path, rows):
    d.mkdir(parents=True, exist_ok=True)
    (d / "tx_2026_10.csv").write_bytes(("\n".join([TX_HEAD] + rows) + "\n").encode("cp950"))


def ticks_db(p: Path, rows):
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE ticks(code TEXT, ts REAL, price REAL, volume INTEGER)")
        c.executemany("INSERT INTO ticks VALUES (?,?,?,?)", rows)


def test_taifex_month():
    assert db.taifex_month("TXFK6", date(2026, 10, 21)) == "202611"
    assert db.taifex_month("TXFA7", date(2026, 12, 16)) == "202701"
    assert db.taifex_month("TXFJ6", date(2026, 10, 21)) == "202610"
    assert db.taifex_month("TXFJ6", date(2026, 11, 2)) is None                          # 已經過了的月份（十年內找不到）：不猜
    assert db.taifex_month("TXF202611", date(2026, 10, 1)) is None and db.taifex_month("", date(2026, 10, 1)) is None


def test_night_fallback_uses_the_official_close(tmp_path):
    tx_file(tmp_path / "tx", ["2026/10/21,TX,202610  ,1,1,1,47000,0,0%,100,47000,1,1,1,1,1,,一般,",
                              "2026/10/21,TX,202611  ,1,1,1,46900,0,0%,100,46900,1,1,1,1,1,,盤後,",      # 夜盤列排前面：也只能取日盤列
                              "2026/10/21,TX,202611  ,1,1,1,47100,0,0%,100,47100,1,1,1,1,1,,一般,"])
    p = tmp_path / "ticks.db"
    ticks_db(p, [("TXFK6", tpe(2026, 10, 21, 15, 0).timestamp(), 47050, 1), ("TXFK6", tpe(2026, 10, 22, 4, 59).timestamp(), 47571, 1)])
    feat = {"value": None, "missing_reason": "no_ticks: TXFK6 在 2026-10-21 日盤沒有成交紀錄"}
    fb = db.night_fallback(feat, p, date(2026, 10, 21), tmp_path / "tx")
    assert fb["value"] == pytest.approx(47571 / 47100 - 1) and fb["detail"]["prev_close"] == 47100 and fb["fallback"] == "taifex_close"
    assert db.night_fallback({"missing_reason": "incomplete_session: …"}, p, date(2026, 10, 21), tmp_path / "tx") is None     # 別的原因：照原樣
    assert db.night_fallback(feat, p, date(2026, 10, 21), tmp_path / "nothing") is None                                       # 沒有期交所資料
    p2 = tmp_path / "t2.db"
    ticks_db(p2, [("TXFK6", tpe(2026, 10, 22, 3, 0).timestamp(), 47571, 1)])                                                   # 夜盤最後一筆離收盤太遠
    assert db.night_fallback(feat, p2, date(2026, 10, 21), tmp_path / "tx") is None
    line = db._night_line({**fb, "detail": {**fb["detail"]}})
    assert line.startswith("夜盤 TXF：+1.00%") and "換月後第一天：前一日收盤取自期交所日行情" in line


def test_gather_uses_the_fallback_after_rollover(tmp_path):
    tx_file(tmp_path / "tx", ["2026/10/21,TX,202611  ,1,1,1,47100,0,0%,100,47100,1,1,1,1,1,,一般,"])
    p = tmp_path / "ticks.db"
    ticks_db(p, [("TXFJ6", tpe(2026, 10, 21, 13, 44).timestamp(), 47000, 1),                    # 結算日白天只有舊合約
                 ("TXFK6", tpe(2026, 10, 21, 15, 0).timestamp(), 47050, 5), ("TXFK6", tpe(2026, 10, 22, 4, 59).timestamp(), 47571, 5)])
    d = db.gather(tpe(2026, 10, 22, 8, 35), Store(_bars(DATES)), STATE, lambda x: True, lambda x: date(2026, 10, 21), ticks_path=p, tx_dir=tmp_path / "tx")
    assert d["night"]["fallback"] == "taifex_close" and d["night"]["value"] == pytest.approx(47571 / 47100 - 1)
