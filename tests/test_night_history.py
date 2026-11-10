"""夜盤 1 分 K 歷史（core/night_history.py，update.md 發現 81）：K 棒時間對到哪一晚哪一根、資料庫、抓取（假的 shioaji：連線數保護、分段、重試、一定 logout）、
交易路徑不得引用。"""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core import night_history as nh

ROOT = Path(nh.__file__).resolve().parent.parent


def ns(*a) -> int:
    """券商 K 棒時間：台灣當地時間當成 UTC 的 epoch 奈秒（標結束）。"""
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1e9)


def raw_of(times, price=100.0):
    n = len(times)
    return {"ts": [ns(*t) for t in times], "open": [price] * n, "high": [price + 1] * n, "low": [price - 1] * n,
            "close": [price + 0.5] * n, "volume": [10] * n}


def test_night_seq_boundaries():
    assert nh.night_seq(15 * 60 + 1) == 1                       # 15:01 第一根
    assert nh.night_seq(23 * 60 + 59) == 539
    assert nh.night_seq(0) == 540                               # 標 00:00 的那根
    assert nh.night_seq(1) == 541
    assert nh.night_seq(5 * 60) == nh.NIGHT_BARS == 840         # 05:00 最後一根
    assert nh.night_seq(5 * 60 + 1) is None                     # 05:01 之後不是夜盤
    for mins in (8 * 60 + 46, 13 * 60 + 45, 14 * 60 + 59, 15 * 60):
        assert nh.night_seq(mins) is None, mins                 # 日盤、15:00（盤前試算）都不算


def test_rows_belong_to_the_night_that_started_the_day_before_midnight():
    r = nh.night_session_minutes(raw_of([(2026, 11, 3, 15, 1), (2026, 11, 3, 23, 59), (2026, 11, 4, 0, 0), (2026, 11, 4, 0, 1), (2026, 11, 4, 5, 0)]))
    assert [(x["date"], x["seq"], x["hhmm"]) for x in r] == [("2026-11-03", 1, 1501), ("2026-11-03", 539, 2359), ("2026-11-03", 540, 0), ("2026-11-03", 541, 1), ("2026-11-03", 840, 500)]


def test_friday_night_ends_on_saturday_and_day_bars_are_dropped():
    r = nh.night_session_minutes(raw_of([(2026, 11, 6, 8, 46), (2026, 11, 6, 13, 45), (2026, 11, 6, 15, 1), (2026, 11, 7, 3, 0), (2026, 11, 7, 5, 1)]))
    assert [(x["date"], x["seq"]) for x in r] == [("2026-11-06", 1), ("2026-11-06", 720)]             # 週六 03:00 屬於週五的夜盤；05:01、日盤都不收


def test_sorted_and_incomplete_prices_dropped():
    raw = raw_of([(2026, 11, 4, 0, 5), (2026, 11, 3, 15, 2), (2026, 11, 3, 15, 3)])
    raw["close"][1] = 0                                          # 價格不完整
    r = nh.night_session_minutes(raw)
    assert [x["seq"] for x in r] == [3, 545]                      # seq 2（close＝0）丟掉；依時間升冪（輸入是亂序的）
    assert nh.night_session_minutes({"ts": [], "open": [], "high": [], "low": [], "close": [], "volume": []}) == []


def test_store_roundtrip_is_idempotent_and_ordered(tmp_path):
    st = nh.NightStore(tmp_path / "n.db")
    rows = nh.night_session_minutes(raw_of([(2026, 11, 3, 15, 2), (2026, 11, 3, 15, 1), (2026, 11, 4, 0, 1), (2026, 11, 9, 15, 1)]))
    st.upsert("TXF", rows)
    st.upsert("TXF", rows)                                       # 重複寫入不增加
    n = st.nights()
    assert list(n) == ["2026-11-03", "2026-11-09"] and [b[0] for b in n["2026-11-03"]] == [1, 2, 541]
    assert n["2026-11-03"][0] == (1, 100.0, 101.0, 99.0, 100.5, 10)
    assert list(st.nights(start="2026-11-04")) == ["2026-11-09"] and list(st.nights(end="2026-11-03")) == ["2026-11-03"]
    assert st.nights("MXF") == {}
    assert st.coverage() == [("2026", 2, 4)]
    st.upsert("TXF", [])                                         # 空的不丟


class FakeAPI:
    calls: list = []
    logged_out = 0
    fail_first = False

    def __init__(self, simulation=False):
        assert simulation is True                                # 只讀、用模擬盤登入
        FakeAPI.calls, FakeAPI.logged_out = [], 0
        self.Contracts = types.SimpleNamespace(Futures=types.SimpleNamespace(TXF=types.SimpleNamespace(TXFR1="R1")))

    def login(self, **kw):
        pass

    def kbars(self, contract, start, end, timeout):
        FakeAPI.calls.append((start, end))
        if FakeAPI.fail_first and len(FakeAPI.calls) == 1:
            raise TimeoutError("慢")
        d = date.fromisoformat(start)
        return types.SimpleNamespace(ts=[ns(d.year, d.month, d.day, 15, 1)], Open=[1.0], High=[2.0], Low=[0.5], Close=[1.5], Volume=[3])

    def logout(self):
        FakeAPI.logged_out += 1


@pytest.fixture()
def fake(monkeypatch):
    mod = types.ModuleType("shioaji")
    mod.Shioaji = FakeAPI
    monkeypatch.setitem(sys.modules, "shioaji", mod)
    monkeypatch.setenv("SHIOAJI_API_KEY", "x")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "y")
    monkeypatch.setattr("core.broker_guard.room_for_login", lambda *a, **k: (True, "ok"))
    FakeAPI.fail_first = False
    yield FakeAPI


def test_fetch_chunks_into_25_day_pieces_and_always_logs_out(fake, tmp_path):
    st = nh.NightStore(tmp_path / "n.db")
    s = nh.fetch_history(date(2026, 1, 1), date(2026, 3, 1), st, pause=0, log=lambda *_: None)
    assert fake.calls[0] == ("2026-01-01", "2026-01-26") and fake.calls[1][0] == "2026-01-27"       # 下一段從上一段結束的隔天開始
    assert fake.calls[-1][1] == "2026-03-02"                                                         # 最後一晚的凌晨在隔天
    assert s["failed"] == 0 and s["chunks"] == len(fake.calls) and s["rows"] == len(fake.calls)
    assert fake.logged_out == 1
    assert len(st.nights()) == len(fake.calls)


def test_fetch_retries_once_then_counts_a_failed_chunk(fake, tmp_path):
    fake.fail_first = True
    s = nh.fetch_history(date(2026, 1, 1), date(2026, 1, 10), nh.NightStore(tmp_path / "n.db"), pause=0, log=lambda *_: None)
    assert s["failed"] == 0 and len(fake.calls) == 2 and fake.calls[0] == fake.calls[1]               # 第一次逾時、第二次成功
    class Always(FakeAPI):
        def kbars(self, contract, start, end, timeout):
            raise TimeoutError("慢")
    sys.modules["shioaji"].Shioaji = Always
    s = nh.fetch_history(date(2026, 1, 1), date(2026, 1, 10), nh.NightStore(tmp_path / "m.db"), pause=0, log=lambda *_: None)
    assert s == {"chunks": 0, "failed": 1, "rows": 0}


def test_fetch_never_starts_before_the_earliest_day_and_refuses_when_connections_are_full(fake, tmp_path, monkeypatch):
    nh.fetch_history(date(2019, 1, 1), date(2020, 3, 3), nh.NightStore(tmp_path / "n.db"), pause=0, log=lambda *_: None)
    assert fake.calls[0][0] == "2020-03-02"
    monkeypatch.setattr("core.broker_guard.room_for_login", lambda *a, **k: (False, "連線太多"))
    before = len(fake.calls)
    with pytest.raises(RuntimeError, match="連線太多"):
        nh.fetch_history(date(2026, 1, 1), date(2026, 1, 2), nh.NightStore(tmp_path / "x.db"), pause=0, log=lambda *_: None)
    assert len(fake.calls) == before                                                                   # 沒登入、沒查


def test_trading_path_never_imports_it():
    for sub in ("strategies", "api"):
        for f in (ROOT / sub).glob("*.py"):
            assert "night_history" not in f.read_text(encoding="utf-8"), f
    for f in ("main.py", "core/broker.py", "core/risk.py", "core/manual_monitor.py"):
        p = ROOT / f
        if p.exists():
            assert "night_history" not in p.read_text(encoding="utf-8"), f
