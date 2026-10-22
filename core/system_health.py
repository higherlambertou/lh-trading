"""系統健康（update.md 發現 66）：把散在各處的狀態集中成一張卡——只讀檔案與記憶體，不查券商、不碰下單。

  券商連線（連線假死偵測、最近報價、連線數、今日流量）／近 7 天 watchdog 的自動重啟（讀 watchdog 的 log）／
  資料庫備份與雲端副本／期交所資料自動更新／lh-intel 的收集器與心跳／磁碟剩餘與 app log 大小。
每一項回 {"key", "title", "status": "ok"|"warn"|"bad"|"off", "text", "detail"}；任何一項讀不到就那一項寫原因，不影響其他。"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from core.daytrade import TAIPEI

ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_LOG = {"live": Path("/tmp/lh_live_watchdog.log"), "sim": Path("/tmp/lh_sim_watchdog.log")}
APP_LOG = {"live": Path("/tmp/lh_live.log"), "sim": Path("/tmp/lh_sim.log")}
TAIFEX_STATE = ROOT / "data" / "taifex" / "update_state.json"
CONNECTION_LIMIT = 5
DISK_WARN_GB = 10.0
LOG_WARN_MB = 200.0
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[watchdog-\w+\]\s+(.*)$")
_KINDS = (("stale", "判定券商連線假死"), ("freeze", "判定故障("), ("rollover", "換月：後端要求重啟"), ("gone", "進程已不存在"),
          ("stop", "收到結束訊號"), ("start", "健康檢查目標"))
KIND_TEXT = {"stale": "連線假死", "freeze": "凍結／斷線", "gone": "行程結束", "rollover": "換月", "stop": "手動停止", "start": "watchdog 啟動"}


def item(key: str, title: str, status: str, text: str, detail: Optional[str] = None) -> dict[str, Any]:
    return {"key": key, "title": title, "status": status, "text": text, "detail": detail}


def _ago(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    if sec < 90:
        return f"{int(sec)} 秒前"
    if sec < 5400:
        return f"{int(sec // 60)} 分鐘前"
    if sec < 172800:
        return f"{sec / 3600:.1f} 小時前"
    return f"{int(sec // 86400)} 天前"


def _md(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


# ── 各項（純函式）────────────────────────────────────────────────

def broker_item(connected: bool, stale: Optional[str], snap: dict[str, Any], usage: Optional[dict[str, Any]], uptime: Optional[float],
                sdk: Optional[str] = None) -> dict[str, Any]:
    q = snap.get("last_quote_age")
    parts = [f"最近報價 {_ago(q)}"]
    if usage:
        parts.append(f"券商連線 {usage.get('connections', '—')} 條（上限 {CONNECTION_LIMIT}）")
        if usage.get("percent") is not None:
            parts.append(f"今日流量 {usage['percent']}%")
    if uptime is not None:
        parts.append(f"後端已跑 {_ago(uptime).replace('前', '')}")
    if sdk:
        parts.append(f"SDK {sdk}")
    if not connected:
        return item("broker", "券商連線", "bad", "沒有連上券商（登入中或斷線）｜" + "｜".join(parts))
    if stale:
        return item("broker", "券商連線", "warn", f"連線假死：{stale}（watchdog 會重啟）", "｜".join(parts))
    n = (usage or {}).get("connections")
    status = "warn" if isinstance(n, int) and n >= CONNECTION_LIMIT - 1 else "ok"
    return item("broker", "券商連線", status, "連線正常｜" + "｜".join(parts),
                "連線數快到上限：可能有殘留的 worker（見 OPERATION.md〈孤兒 worker〉）" if status == "warn" else None)


def watchdog_events(text: str, now: float, days: int = 7) -> list[tuple[float, str, str]]:
    """watchdog 的 log → [(時間, 種類, 原文)]，只留最近 days 天。"""
    cutoff, out = now - days * 86400, []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI).timestamp()
        except ValueError:
            continue
        if t < cutoff:
            continue
        kind = next((k for k, pat in _KINDS if pat in m.group(2)), None)
        if kind:
            out.append((t, kind, m.group(2)))
    return out


def restarts_item(events: Optional[list[tuple[float, str, str]]], now: float, note: Optional[str] = None) -> dict[str, Any]:
    if events is None:
        return item("restarts", "自動重啟（近 7 天）", "off", note or "讀不到 watchdog 的 log")
    auto = [e for e in events if e[1] in ("stale", "freeze", "gone", "rollover")]
    counts = {k: sum(1 for e in auto if e[1] == k) for k in ("stale", "freeze", "gone", "rollover")}
    recent_bad = [e for e in auto if e[1] in ("stale", "freeze", "gone") and now - e[0] < 86400]
    if not auto:
        text = "近 7 天沒有自動重啟"
    else:
        text = f"近 7 天自動重啟 {len(auto)} 次（" + "、".join(f"{KIND_TEXT[k]} {n}" for k, n in counts.items() if n) + "）"
        last = auto[-1]
        text += f"；最近一次 {datetime.fromtimestamp(last[0], TAIPEI):%m/%d %H:%M} {KIND_TEXT[last[1]]}"
    detail = "\n".join(f"{datetime.fromtimestamp(t, TAIPEI):%m/%d %H:%M} {KIND_TEXT[k]}：{raw[:80]}" for t, k, raw in events[-8:]) or None
    return item("restarts", "自動重啟（近 7 天）", "warn" if recent_bad else "ok", text, detail)


def backup_item(root: Path, now: float) -> dict[str, Any]:
    days = sorted(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)) if root.exists() else []
    if not days:
        return item("backup", "資料庫備份", "bad", f"{root} 裡沒有任何備份")
    last = days[-1]
    files = [f for f in (root / last).iterdir() if f.suffix == ".db"]
    mb = sum(f.stat().st_size for f in files) / 1e6
    age_days = (datetime.fromtimestamp(now, TAIPEI).date() - date.fromisoformat(last)).days
    text = f"最近備份 {_md(last)}（{len(files)} 個檔、{mb:.0f} MB）"
    status = "warn" if age_days > 3 else "ok"
    try:
        ms = json.loads((root / "mirror_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ms = None
    if not ms:
        text += "｜雲端副本：沒有設定（只在這台電腦上）"
        status = "warn" if status == "ok" else status
    elif ms.get("last_status") == "ok":
        text += f"｜雲端副本 {_md(ms['last_attempt_day'])} 成功"
    else:
        ok_day = ms.get("last_ok_day")
        text += f"｜雲端副本 {_md(ms['last_attempt_day'])} {'略過' if ms.get('last_status') == 'skipped' else '失敗'}（{(ms.get('reason') or '')[:40]}）" + (f"，上次成功 {_md(ok_day)}" if ok_day else "")
        status = "warn"
    return item("backup", "資料庫備份", status, text, str(root / last))


def taifex_item(state: Optional[dict[str, Any]], now: float) -> dict[str, Any]:
    if not state:
        return item("taifex", "期交所資料自動更新", "warn", "還沒有成功更新過（平日 15:10 後自動跑）")
    today = datetime.fromtimestamp(now, TAIPEI).date()
    names = {"tx": "TX 日行情", "night": "夜盤模型", "vrp": "每日 IV"}
    parts, stale = [], False
    for k, n in names.items():
        d = (state.get(k) or {}).get("ok_date")
        parts.append(f"{n} {_md(d) if d else '—'}")
        limit = 9 if k == "vrp" else 4
        if d is None or (today - date.fromisoformat(d)).days > limit:
            stale = True
    return item("taifex", "期交所資料自動更新", "warn" if stale else "ok", "、".join(parts),
                "\n".join(f"{names[k]}：{(state.get(k) or {}).get('summary', '')}" for k in names if state.get(k)))


def intel_item(st: dict[str, Any]) -> dict[str, Any]:
    if not st.get("available"):
        return item("intel", "lh-intel 資訊收集", "bad", f"讀不到：{st.get('reason', '原因不明')}")
    n, probs = len(st.get("items") or []), st.get("problems") or []
    hb = st.get("heartbeat_age_sec")
    if not st.get("service_ok"):
        return item("intel", "lh-intel 資訊收集", "bad", f"服務沒在跑（心跳 {_ago(hb)}）")
    if probs:
        return item("intel", "lh-intel 資訊收集", "warn", f"{len(probs)} 個收集器失敗：{'、'.join(probs[:4])}｜心跳 {_ago(hb)}")
    return item("intel", "lh-intel 資訊收集", "ok", f"{n} 個收集器正常｜心跳 {_ago(hb)}")


def tasks_item(tasks: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """背景工作（core/task_watch）：有任何一個丟例外停掉就是紅的。"""
    if not tasks:
        return item("tasks", "背景工作", "off", "沒有登記任何背景工作（舊版後端？）")
    by = {s: [t for t in tasks if t["state"] == s] for s in ("running", "crashed", "off", "done", "cancelled")}
    detail = "\n".join(f"{t['name']}：{ {'running': '執行中', 'crashed': '停了', 'off': '依設定關閉', 'done': '已完成', 'cancelled': '已取消'}[t['state']] }"
                       + (f"（{t['error']}）" if t.get("error") else "") for t in tasks)
    if by["crashed"]:
        return item("tasks", "背景工作", "bad", f"{len(by['crashed'])} 個意外停止：" + "、".join(t["name"] for t in by["crashed"]) + "（重啟後端才會恢復）", detail)
    text = f"{len(by['running'])} 個在跑"
    if by["done"]:
        text += f"、{len(by['done'])} 個開機工作已完成"
    if by["off"]:
        text += f"、{len(by['off'])} 個依設定關閉（" + "、".join(t["name"] for t in by["off"]) + "）"
    return item("tasks", "背景工作", "ok", text, detail)


def disk_item(free_bytes: Optional[int], log_bytes: Optional[int]) -> dict[str, Any]:
    free_gb = None if free_bytes is None else free_bytes / 1e9
    log_mb = None if log_bytes is None else log_bytes / 1e6
    text = f"磁碟剩 {free_gb:.0f} GB" if free_gb is not None else "磁碟：讀不到"
    text += f"｜app log {log_mb:.0f} MB" if log_mb is not None else "｜app log：沒有"
    warn = (free_gb is not None and free_gb < DISK_WARN_GB) or (log_mb is not None and log_mb > LOG_WARN_MB)
    return item("disk", "磁碟與 log", "warn" if warn else "ok", text)


# ── 收集（同步、讀檔；在執行緒裡）──────────────────────────────

def _tail(path: Path, max_bytes: int = 300_000) -> Optional[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def collect(now: float, mode: str, connected: bool, stale: Optional[str], snap: dict[str, Any], usage: Optional[dict[str, Any]],
            uptime: Optional[float], intel_status: Callable[[], dict[str, Any]], backup_root: Path,
            watchdog_log: Optional[Path] = None, app_log: Optional[Path] = None, taifex_state: Optional[Path] = None,
            sdk: Optional[str] = None, tasks: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    items = [broker_item(connected, stale, snap, usage, uptime, sdk), tasks_item(tasks)]
    wl = watchdog_log or WATCHDOG_LOG.get(mode, WATCHDOG_LOG["live"])
    text = _tail(wl)
    items.append(restarts_item(None if text is None else watchdog_events(text, now), now, None if text is not None else f"讀不到 {wl}"))
    try:
        items.append(backup_item(backup_root, now))
    except OSError as e:
        items.append(item("backup", "資料庫備份", "bad", f"讀不到備份資料夾：{e}"))
    try:
        state = json.loads((taifex_state or TAIFEX_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    items.append(taifex_item(state, now))
    items.append(intel_item(intel_status()))
    try:
        free = shutil.disk_usage(ROOT / "data").free
    except OSError:
        free = None
    al = app_log or APP_LOG.get(mode, APP_LOG["live"])
    try:
        size = al.stat().st_size
    except OSError:
        size = None
    items.append(disk_item(free, size))
    worst = "bad" if any(i["status"] == "bad" for i in items) else "warn" if any(i["status"] == "warn" for i in items) else "ok"
    return {"as_of": now, "mode": mode, "overall": worst, "items": items}
