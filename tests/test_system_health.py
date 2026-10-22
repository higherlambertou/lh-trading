"""系統健康（發現 66）：每一項的判斷（正常／注意／異常）、watchdog log 的解析、備份與雲端副本、期交所資料、lh-intel、磁碟；
讀不到的項目只影響自己；API 只讀檔案與記憶體（測試裡全部指到暫存檔）。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from core import system_health as sh
from core.daytrade import TAIPEI


def ts(*a):
    return datetime(*a, tzinfo=TAIPEI).timestamp()


NOW = ts(2026, 10, 22, 17, 0)


def test_broker_item():
    ok = sh.broker_item(True, None, {"last_quote_age": 3.0}, {"connections": 1, "percent": 2.8}, 7200)
    assert ok["status"] == "ok" and "最近報價 3 秒前" in ok["text"] and "券商連線 1 條（上限 5）" in ok["text"] and "今日流量 2.8%" in ok["text"] and "後端已跑 2.0 小時" in ok["text"]
    stale = sh.broker_item(True, "日盤中 200 秒沒有收到任何報價", {"last_quote_age": 200.0}, None, None)
    assert stale["status"] == "warn" and "連線假死" in stale["text"]
    assert sh.broker_item(False, None, {}, None, None)["status"] == "bad"
    assert "SDK 1.5.8" in sh.broker_item(True, None, {"last_quote_age": 3.0}, None, None, "1.5.8")["text"]
    many = sh.broker_item(True, None, {"last_quote_age": 1.0}, {"connections": 4, "percent": 1}, None)
    assert many["status"] == "warn" and "孤兒 worker" in many["detail"]


WD = """2026-10-14 09:00:00  [watchdog-live]  判定故障(down) → kill -9 PID=1 並重啟（自動重連）
2026-10-19 10:04:29  [watchdog-live]  健康檢查目標：http://x/api/health
2026-10-21 14:46:39  [watchdog-live]  換月：後端要求重啟（近月合約已結算）→ 正常關閉 PID=84418 後重啟
2026-10-22 10:57:30  [watchdog-live]  判定券商連線假死(stale) → 正常關閉 PID=2 後重啟
2026-10-22 11:30:00  [watchdog-live]  判定故障(nobroker) → kill -9 PID=3 並重啟（自動重連）
2026-10-22 12:00:00  [watchdog-live]  進程已不存在（登入失敗或自行結束），重啟
2026-10-22 16:33:00  [watchdog-live]  收到結束訊號，關閉正式盤 (PID=4)…
亂七八糟的一行
"""


def test_watchdog_events_and_restarts_item():
    ev = sh.watchdog_events(WD, NOW)
    assert [k for _, k, _ in ev] == ["start", "rollover", "stale", "freeze", "gone", "stop"]          # 10/14 那筆超過 7 天
    it = sh.restarts_item(ev, NOW)
    assert it["status"] == "warn"                                                        # 24 小時內有假死／凍結
    assert it["text"].startswith("近 7 天自動重啟 4 次（連線假死 1、凍結／斷線 1、行程結束 1、換月 1）") and "最近一次 10/22 12:00 行程結束" in it["text"]
    calm = sh.restarts_item(sh.watchdog_events(WD, ts(2026, 10, 24, 17, 0)), ts(2026, 10, 24, 17, 0))
    assert calm["status"] == "ok"
    assert sh.restarts_item([], NOW)["text"] == "近 7 天沒有自動重啟"
    assert sh.restarts_item(None, NOW, "讀不到 /tmp/x")["status"] == "off"


def make_backup(root: Path, day: str, n=4):
    d = root / day
    d.mkdir(parents=True)
    for k in range(n):
        (d / f"f{k}.db").write_bytes(b"x" * 1000)


def test_backup_item(tmp_path):
    assert sh.backup_item(tmp_path / "none", NOW)["status"] == "bad"
    make_backup(tmp_path, "2026-10-22")
    it = sh.backup_item(tmp_path, NOW)
    assert it["status"] == "warn" and "最近備份 10/22（4 個檔" in it["text"] and "雲端副本：沒有設定" in it["text"]
    (tmp_path / "mirror_state.json").write_text(json.dumps({"last_attempt_day": "2026-10-22", "last_status": "ok", "last_ok_day": "2026-10-22"}))
    it = sh.backup_item(tmp_path, NOW)
    assert it["status"] == "ok" and "雲端副本 10/22 成功" in it["text"]
    (tmp_path / "mirror_state.json").write_text(json.dumps({"last_attempt_day": "2026-10-22", "last_status": "skipped", "reason": "雲端資料夾不在", "last_ok_day": "2026-10-20"}))
    it = sh.backup_item(tmp_path, NOW)
    assert it["status"] == "warn" and "略過（雲端資料夾不在）" in it["text"] and "上次成功 10/20" in it["text"]
    old = tmp_path / "old"
    make_backup(old, "2026-10-15")
    (old / "mirror_state.json").write_text(json.dumps({"last_attempt_day": "2026-10-15", "last_status": "ok", "last_ok_day": "2026-10-15"}))
    assert sh.backup_item(old, NOW)["status"] == "warn"                                 # 雲端副本也成功、只差在超過 3 天
    assert sh.backup_item(old, ts(2026, 10, 18, 17, 0))["status"] == "ok"                # 3 天內（週末）


def test_taifex_item():
    assert sh.taifex_item(None, NOW)["status"] == "warn"
    st = {"tx": {"ok_date": "2026-10-22", "summary": "下載 1 個"}, "night": {"ok_date": "2026-10-22"}, "vrp": {"ok_date": "2026-10-16"}}
    it = sh.taifex_item(st, NOW)
    assert it["status"] == "ok" and it["text"] == "TX 日行情 10/22、夜盤模型 10/22、每日 IV 10/16"
    st["tx"]["ok_date"] = "2026-10-17"
    assert sh.taifex_item(st, NOW)["status"] == "warn"


def test_intel_item():
    assert sh.intel_item({"available": False, "reason": "找不到"})["status"] == "bad"
    assert sh.intel_item({"available": True, "service_ok": False, "heartbeat_age_sec": 900, "items": [], "problems": []})["status"] == "bad"
    w = sh.intel_item({"available": True, "service_ok": True, "heartbeat_age_sec": 5, "items": [{}] * 18, "problems": ["fred_daily"]})
    assert w["status"] == "warn" and "1 個收集器失敗：fred_daily" in w["text"]
    ok = sh.intel_item({"available": True, "service_ok": True, "heartbeat_age_sec": 5, "items": [{}] * 18, "problems": []})
    assert ok["status"] == "ok" and ok["text"] == "18 個收集器正常｜心跳 5 秒前"


def test_disk_item():
    assert sh.disk_item(int(229e9), int(81e6)) == {"key": "disk", "title": "磁碟與 log", "status": "ok", "text": "磁碟剩 229 GB｜app log 81 MB", "detail": None}
    assert sh.disk_item(int(5e9), None)["status"] == "warn" and sh.disk_item(int(100e9), int(300e6))["status"] == "warn"


def test_collect_survives_missing_everything(tmp_path):
    r = sh.collect(NOW, "live", True, None, {"last_quote_age": 1.0}, None, 10.0, lambda: {"available": False, "reason": "x"}, tmp_path / "bk",
                   watchdog_log=tmp_path / "no.log", app_log=tmp_path / "no_app.log", taifex_state=tmp_path / "no.json")
    keys = [i["key"] for i in r["items"]]
    assert keys == ["broker", "restarts", "backup", "taifex", "intel", "disk"] and r["overall"] == "bad"
    assert next(i for i in r["items"] if i["key"] == "restarts")["status"] == "off"


def test_collect_reads_the_files(tmp_path):
    (tmp_path / "wd.log").write_text(WD, encoding="utf-8")
    (tmp_path / "app.log").write_bytes(b"x" * 2_000_000)
    make_backup(tmp_path / "bk", "2026-10-22")
    (tmp_path / "bk" / "mirror_state.json").write_text(json.dumps({"last_attempt_day": "2026-10-22", "last_status": "ok", "last_ok_day": "2026-10-22"}))
    (tmp_path / "st.json").write_text(json.dumps({"tx": {"ok_date": "2026-10-22"}, "night": {"ok_date": "2026-10-22"}, "vrp": {"ok_date": "2026-10-22"}}))
    intel = {"available": True, "service_ok": True, "heartbeat_age_sec": 5, "items": [{}] * 18, "problems": []}
    r = sh.collect(ts(2026, 10, 24, 12, 0), "live", True, None, {"last_quote_age": 1.0}, {"connections": 1, "percent": 1.0}, 10.0, lambda: intel,
                   tmp_path / "bk", watchdog_log=tmp_path / "wd.log", app_log=tmp_path / "app.log", taifex_state=tmp_path / "st.json")
    assert r["overall"] == "ok", r["items"]
    assert "app log 2 MB" in next(i for i in r["items"] if i["key"] == "disk")["text"]


def test_api_route_reads_memory_and_temp_files(monkeypatch, tmp_path):
    from api import routes_system as rs
    monkeypatch.setitem(sh.WATCHDOG_LOG, "live", tmp_path / "wd.log")
    monkeypatch.setitem(sh.WATCHDOG_LOG, "sim", tmp_path / "wd.log")
    monkeypatch.setitem(sh.APP_LOG, "live", tmp_path / "app.log")
    monkeypatch.setitem(sh.APP_LOG, "sim", tmp_path / "app.log")
    monkeypatch.setattr(sh, "TAIFEX_STATE", tmp_path / "st.json")
    r = asyncio.run(rs.get_health())
    assert r["mode"] in ("live", "sim") and len(r["items"]) == 6 and r["overall"] in ("ok", "warn", "bad")
    import shioaji
    assert f"SDK {shioaji.__version__}" in r["items"][0]["text"]                       # 跑測試的那個 Python 的版本
    assert next(i for i in r["items"] if i["key"] == "intel")["status"] == "bad"           # 測試沒有 lh-intel


def test_not_used_by_trading_modules():
    root = Path(sh.__file__).resolve().parent.parent
    for p in list((root / "strategies").glob("*.py")) + [root / "core" / n for n in ("broker.py", "manual_monitor.py", "quote_hub.py", "shioaji_worker.py")]:
        assert "system_health" not in p.read_text(encoding="utf-8"), p
