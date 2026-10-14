"""手機推播（Telegram／ntfy／自訂 webhook）。只在持倉可能沒保護的時候用：後端凍結、重啟後沒有停損監控（update.md 發現 18、32）。

設計重點
  - 只用標準函式庫（urllib），不加新套件；`python -m core.notify …` 在 watchdog（shell）裡也能用，不會載入券商 SDK。
  - 永遠不丟例外、在背景執行緒送（不卡 asyncio event loop）；送不出去至少寫一行 WARNING（訊息本身也寫進 log）。
  - 同一件事有冷卻（key＋冷卻秒數；跨進程記在 data/notify_cooldown.json，後端重啟或 watchdog 每次呼叫都是新進程也擋得住洗版）。
  - 沒設定任何通道＝不推播，只寫 ERROR log；畫面上的「沒有停損保護」橫幅另外有。
  - 訊息只放合約、方向、口數與停損點數，不放帳號、權益等資料；token／網址不會出現在 log。

設定（.env；可以同時設多個通道）
  NOTIFY_TELEGRAM_TOKEN / NOTIFY_TELEGRAM_CHAT_ID   Telegram bot（私人、免費；建議）
  NOTIFY_NTFY_URL            https://ntfy.sh/<主題>（主題名稱就是唯一的密碼：用一長串隨機字，或自架 ntfy）
  NOTIFY_WEBHOOK_URL         收 JSON {title, message, priority} 的任何網址
先試：python -m core.notify test"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 8.0
MAX_LEN = 1500
_PRIORITY = {"default": 3, "high": 4, "urgent": 5}
_SECRET_ENV = ("NOTIFY_TELEGRAM_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID", "NOTIFY_NTFY_URL", "NOTIFY_WEBHOOK_URL")

_clock: Callable[[], float] = time.time           # 測試可替換
_memory_last: dict[str, float] = {}               # 沒有冷卻檔時的記憶體備援
_lock = threading.Lock()


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def channels() -> list[str]:
    out = []
    if _env("NOTIFY_TELEGRAM_TOKEN") and _env("NOTIFY_TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if _env("NOTIFY_NTFY_URL"):
        out.append("ntfy")
    if _env("NOTIFY_WEBHOOK_URL"):
        out.append("webhook")
    return out


def _mask(text: str) -> str:
    """錯誤訊息裡若帶到 token／網址，換成 ***（log 不洩漏通道的密碼）。"""
    secrets = [_env(n) for n in _SECRET_ENV]
    u = urllib.parse.urlsplit(_env("NOTIFY_NTFY_URL"))
    secrets += [u.path.strip("/"), u.netloc]
    for s in sorted({x for x in secrets if x and len(x) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


def _http_post(url: str, body: bytes, headers: dict[str, str], timeout: float = TIMEOUT) -> int:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("通知網址只接受 http／https")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310（上面已限制 scheme）
        return int(r.status)


def _http_get(url: str, timeout: float = TIMEOUT) -> tuple[int, str]:
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("通知網址只接受 http／https")
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:      # noqa: S310（上面已限制 scheme）
        return int(r.status), r.read().decode("utf-8", "replace")


def telegram_chats(updates: dict) -> list[dict]:
    """getUpdates 的回應 → 對 bot 說過話的聊天室（去重、保持出現順序）：{id, type, name}。結構不對的項目略過。"""
    seen: dict[int, dict] = {}
    for u in updates.get("result", []) if isinstance(updates, dict) else []:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            obj = u.get(key) if isinstance(u, dict) else None
            chat = obj.get("chat") if isinstance(obj, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int) and chat["id"] not in seen:
                name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
                seen[chat["id"]] = {"id": chat["id"], "type": chat.get("type", "?"), "name": name}
    return list(seen.values())


def _send_telegram(title: str, message: str, priority: str) -> int:
    url = f"https://api.telegram.org/bot{_env('NOTIFY_TELEGRAM_TOKEN')}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": _env("NOTIFY_TELEGRAM_CHAT_ID"), "text": f"{title}\n{message}"}).encode("utf-8")
    return _http_post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})


def _send_ntfy(title: str, message: str, priority: str) -> int:
    # 用 JSON 發佈（標題有中文，放在 HTTP 標頭會有編碼問題）：POST 到伺服器根目錄、主題寫在 JSON 裡
    u = urllib.parse.urlsplit(_env("NOTIFY_NTFY_URL"))
    topic = u.path.strip("/")
    if u.scheme not in ("http", "https") or not u.netloc or not topic:
        raise ValueError("NOTIFY_NTFY_URL 要寫成 https://主機/主題")
    body = json.dumps({"topic": topic, "title": title, "message": message, "priority": _PRIORITY.get(priority, 4), "tags": ["warning"]}, ensure_ascii=False).encode("utf-8")
    return _http_post(f"{u.scheme}://{u.netloc}/", body, {"Content-Type": "application/json; charset=utf-8"})


def _send_webhook(title: str, message: str, priority: str) -> int:
    body = json.dumps({"title": title, "message": message, "priority": priority}, ensure_ascii=False).encode("utf-8")
    return _http_post(_env("NOTIFY_WEBHOOK_URL"), body, {"Content-Type": "application/json; charset=utf-8"})


_SENDERS = {"telegram": _send_telegram, "ntfy": _send_ntfy, "webhook": _send_webhook}


def send_sync(title: str, message: str, priority: str = "high") -> dict[str, str]:
    """送到每個已設定的通道，回傳 {通道: 'ok' 或錯誤說明}。不丟例外。沒有通道時回 {}（並寫 ERROR log）。"""
    message = message if len(message) <= MAX_LEN else message[:MAX_LEN] + "…"
    logger.warning("NOTIFY %s｜%s", title, message.replace("\n", " ／ "))     # 不論推不推得出去，log 裡都留一份
    names = channels()
    if not names:
        logger.error("沒有設定任何推播通道（NOTIFY_TELEGRAM_*／NOTIFY_NTFY_URL／NOTIFY_WEBHOOK_URL），上面這則警示只有 log；"
                     "設定後用 python -m core.notify test 測試")
        return {}
    result: dict[str, str] = {}
    for name in names:
        try:
            code = _SENDERS[name](title, message, priority)
            result[name] = "ok" if 200 <= code < 300 else f"HTTP {code}"
        except Exception as e:  # noqa: BLE001
            result[name] = _mask(f"{type(e).__name__}: {e}")
    bad = {k: v for k, v in result.items() if v != "ok"}
    if bad:
        logger.warning("推播有通道失敗：%s", bad)
    else:
        logger.info("推播已送出（%s）", "、".join(result))
    return result


# ── 冷卻 ─────────────────────────────────────────────────────────

def _cooldown_file() -> Optional[Path]:
    custom = os.getenv("NOTIFY_COOLDOWN_FILE")
    if custom is not None:
        return Path(custom) if custom.strip() else None            # 設成空字串＝只用記憶體（測試用）
    return ROOT / "data" / "notify_cooldown.json"


def _claim(key: str, cooldown: float) -> bool:
    """這個 key 在冷卻秒數內送過就回 False；否則記下現在時間並回 True。"""
    now = _clock()
    path = _cooldown_file()
    with _lock:
        last: dict[str, float] = {}
        if path is not None:
            try:
                last = {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
            except (OSError, ValueError, AttributeError):
                last = {}
        last.update({k: v for k, v in _memory_last.items() if v > last.get(k, 0)})
        if key in last and 0 <= now - last[key] < cooldown:
            return False
        last[key] = now
        _memory_last[key] = now
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps({k: v for k, v in last.items() if now - v < 86400}), encoding="utf-8")
                os.replace(tmp, path)
            except OSError as e:
                logger.debug("冷卻檔寫不進去（改用記憶體）：%r", e)
        return True


def _release(key: str) -> None:
    """推播全部失敗時把冷卻還回去：下一次事件可以重試，不會因為通道暫時壞掉就沉默 10 分鐘。"""
    path = _cooldown_file()
    with _lock:
        _memory_last.pop(key, None)
        if path is None:
            return
        try:
            last = {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
            if last.pop(key, None) is not None:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(last), encoding="utf-8")
                os.replace(tmp, path)
        except (OSError, ValueError, AttributeError):
            pass


def _deliver(title: str, message: str, priority: str, key: Optional[str], cooldown: float) -> dict[str, str]:
    """（已通過冷卻）實際送出；有設定通道卻全部失敗就把冷卻還回去。不丟例外。"""
    try:
        res = send_sync(title, message, priority)
    except Exception:  # noqa: BLE001
        logger.exception("推播執行緒例外（已忽略）")
        res = {"error": "exception"}
    if key and cooldown > 0 and res and not any(v == "ok" for v in res.values()):
        _release(key)
    return res


def notify(title: str, message: str, *, key: Optional[str] = None, cooldown: float = 0.0, priority: str = "high", wait: bool = False) -> bool:
    """推播一則警示。回傳 False ＝ 這件事在冷卻期內已經送過（沒有再送）。預設在背景執行緒送（不卡 event loop）；wait=True 在原地送。"""
    try:
        if key and cooldown > 0 and not _claim(key, cooldown):
            logger.info("推播略過（%s 在 %.0f 秒冷卻內已送過）", key, cooldown)
            return False
        if wait:
            _deliver(title, message, priority, key, cooldown)
        else:
            threading.Thread(target=_deliver, args=(title, message, priority, key, cooldown), name="notify", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("推播失敗（已忽略）")
        return False


# ── CLI ─────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:                                        # pragma: no cover
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  notify  %(message)s")
    ap = argparse.ArgumentParser(prog="python -m core.notify", description="手機推播（持倉保護警示）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="列出已設定的通道（不顯示 token／網址）")
    sub.add_parser("test", help="送一則測試訊息到所有通道")
    sub.add_parser("chat-id", help="Telegram：列出對你的 bot 傳過訊息的聊天室 id（先在 Telegram 對 bot 傳一則訊息；不會送任何訊息）")
    s = sub.add_parser("send", help="送一則訊息")
    s.add_argument("title")
    s.add_argument("message")
    s.add_argument("--key", default="")
    s.add_argument("--cooldown", type=float, default=0.0)
    w = sub.add_parser("watchdog", help="watchdog 要重啟後端之前呼叫：依持倉狀態檔決定要不要警示")
    w.add_argument("--event", choices=("freeze", "exit"), default="freeze")
    w.add_argument("--detail", default="")
    w.add_argument("--mode", default="live")
    w.add_argument("--cooldown", type=float, default=600.0)
    a = ap.parse_args(argv)

    if a.cmd == "status":
        names = channels()
        print("已設定的通道：" + ("、".join(names) if names else "（沒有）"))
        return 0 if names else 2
    if a.cmd == "chat-id":
        token = _env("NOTIFY_TELEGRAM_TOKEN")
        if not token:
            print("還沒有 NOTIFY_TELEGRAM_TOKEN：先把 bot 的 token 寫進 .env（NOTIFY_TELEGRAM_TOKEN=…；見 .env.example）")
            return 2
        try:
            status, body = _http_get(f"https://api.telegram.org/bot{token}/getUpdates?limit=100")
            data = json.loads(body)
        except Exception as e:  # noqa: BLE001
            print("向 Telegram 查詢失敗：" + _mask(f"{type(e).__name__}: {e}") + "（token 寫錯會是 HTTP Error 401）")
            return 1
        if not isinstance(data, dict) or not data.get("ok"):
            print("Telegram 回報失敗：" + _mask(str(data.get("description", data) if isinstance(data, dict) else data)))
            return 1
        chats = telegram_chats(data)
        if not chats:
            print("還沒看到任何訊息：先在 Telegram 對你的 bot 傳一則訊息（例如 /start），再跑一次這個指令。")
            return 1
        for c in chats:
            print(f"聊天室 id：{c['id']}　（{c['type']}{'，' + c['name'] if c['name'] else ''}）")
        print(f"\n把這一行加進 .env（個人通知選 private 那個）：\nNOTIFY_TELEGRAM_CHAT_ID={chats[0]['id']}")
        return 0
    if a.cmd == "test":
        if not channels():
            print("沒有設定任何通道（見 core/notify.py 開頭的說明）")
            return 2
        res = send_sync("推播測試", "這是 lh-trading 的測試訊息：收到代表持倉警示的通道是通的。", "default")
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res and all(v == "ok" for v in res.values()) else 1
    if a.cmd == "send":
        return 0 if notify(a.title, a.message, key=a.key or None, cooldown=a.cooldown, wait=True) else 3
    from core.protection import build_watchdog_message, load_position_state, state_path
    state = load_position_state(state_path(a.mode))
    msg = build_watchdog_message(a.event, a.detail, state)
    if msg is None:
        print("沒有持倉（或不知道持倉）：不推播" if state is not None else "沒有持倉狀態檔：不知道有沒有持倉，不推播")
        return 0
    key = f"watchdog:{a.event}:{a.mode}"
    if not _claim(key, a.cooldown):
        print("冷卻中：不重複推播")
        return 0
    res = _deliver(msg[0], msg[1], "urgent", key, a.cooldown)
    print(json.dumps(res, ensure_ascii=False) if res else "沒有設定任何通道（只寫了 log）")
    return 2 if not res else (0 if any(v == "ok" for v in res.values()) else 1)


if __name__ == "__main__":
    sys.exit(main())
