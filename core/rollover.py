"""換月：近月合約結算後，讓後端（透過 watchdog）重啟、重新取得近月合約（update.md 發現 38）。

問題：worker 在第一次用到時把「近月」（TMFR1 等連續月合約）解析成實際合約並快取（shioaji_worker 的 _contracts），後端又只在啟動時訂閱；
shioaji 1.5.3 的合約檔只在登入時下載。所以後端跨過結算日不重啟，會一直停在已到期的舊合約：沒有報價、下單被拒。
解法：後端自己知道訂閱到哪個合約（worker 在 subscribe 的回覆裡帶 target_code，broker.resolved_codes 記著）。偵測到「訂閱的合約已經過了最後交易時間」，
就在 /api/health 帶 restart_wanted=rollover；watchdog（run_live.sh／run_sim.sh）看到就正常關閉並重啟——重啟＝重新登入＝重新下載合約檔。
什麼時候試：R1 什麼時候換成下個月，官方文件沒寫；文件寫了合約檔每天的更新時間（07:50 期貨商品、08:00 全市場商品、14:45 與 17:15 期貨夜盤商品）。
所以只在結算日 14:46、17:16、隔天 07:51、08:01 這四個「更新之後」的時間點各試一次（試過之後，已經過去的時間點作廢），不盲目輪詢；
四次之後還是舊合約就通知一次、放棄。狀態存 data/rollover_state_<mode>.json（重啟之間要記得試過幾次）。
不確定的事：新的 R1 到底是不是在這幾個時間點之一換掉——沒有實測過，結算日當天要看 log／通知確認。
代碼認不得（target_code 沒有值、拿到 TMFR1 之類）就什麼都不做。ROLLOVER_RESTART=false 關閉。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from core import reminders
from core.daytrade import TAIPEI
from core.notify import notify

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
POLL_SEC = 30.0
MIN_UPTIME = 90.0                      # 剛啟動：等 worker 登入、訂閱完（resolved_codes 有值）再判斷
LAST_TRADE = dtime(13, 30)             # 期貨最後交易日的收盤時間
SLOTS = ((0, 14, 46), (0, 17, 16), (1, 7, 51), (1, 8, 1))      # (結算日之後第幾天, 時, 分)：合約檔更新時間（14:45、17:15、07:50、08:00）之後一分鐘
_STARTED = time.time()
_wanted: Optional[str] = None


def enabled() -> bool:
    return os.getenv("ROLLOVER_RESTART", "true").lower() != "false"


def wanted() -> Optional[str]:
    """health 用：要 watchdog 重啟時回 "rollover"，否則 None。只讀記憶體。"""
    return _wanted


def state_path() -> Path:
    mode = "sim" if os.getenv("SIMULATION", "true").lower() == "true" else "live"
    return ROOT / "data" / f"rollover_state_{mode}.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("version") == 1:
            return {"expiry": d.get("expiry"), "done_until": float(d.get("done_until") or 0.0), "attempts": int(d.get("attempts") or 0),
                    "gave_up": bool(d.get("gave_up")), "confirmed": bool(d.get("confirmed")), "stale_code": str(d.get("stale_code") or "")}
    except (OSError, ValueError, TypeError):
        pass
    return {"expiry": None, "done_until": 0.0, "attempts": 0, "gave_up": False, "confirmed": False, "stale_code": ""}


def save_state(path: Path, st: dict[str, Any]) -> None:
    """原子寫入；寫不進去只記 log（最壞情況是多試一次）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, **st}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("換月狀態存不進去：%r", e)


def stale_contract(resolved: dict[str, str], now: datetime, is_open: Callable[[date], bool]) -> Optional[dict[str, Any]]:
    """訂閱的合約中，已經過了最後交易時間（結算日 13:30）的那一個；沒有就 None。resolved＝{前綴: 實際合約代碼}。"""
    today = now.astimezone(TAIPEI).date()
    out: list[dict[str, Any]] = []
    for prefix, code in sorted(resolved.items()):
        ym = reminders.code_month(str(code), today)
        if ym is None or not ym[2]:                 # 認不得（例如還是 TMFR1）或不是期貨：不判斷
            continue
        e = reminders.expiry_date(ym[0], ym[1], is_open)
        if now >= datetime.combine(e, LAST_TRADE, tzinfo=TAIPEI):
            out.append({"prefix": prefix, "code": str(code), "expiry": e})
    return min(out, key=lambda x: x["expiry"]) if out else None


def evaluate(now: datetime, resolved: dict[str, str], is_open: Callable[[date], bool], state: dict[str, Any], uptime: float
             ) -> tuple[Optional[str], dict[str, Any], Optional[dict[str, Any]]]:
    """回傳 (動作, 新狀態, 資訊)。動作：None／"restart"（要 watchdog 重啟，已把這個時間點記成用掉了）／"gave_up"（四次都換不掉，通知一次）。"""
    st = dict(state)
    info = stale_contract(resolved, now, is_open) if resolved else None
    if info is None:
        return None, st, None
    e: date = info["expiry"]
    if st.get("expiry") != e.isoformat():                       # 新的一個到期日：從頭來
        st = {"expiry": e.isoformat(), "done_until": 0.0, "attempts": 0, "gave_up": False, "confirmed": False, "stale_code": info["code"]}
    info["total"] = len(SLOTS)
    if uptime < MIN_UPTIME or st.get("gave_up"):
        return None, st, info
    slot_times = [datetime.combine(e + timedelta(days=d), dtime(h, m), TAIPEI) for d, h, m in SLOTS]
    pending = [t for t in slot_times if t.timestamp() > st["done_until"]]
    if not pending:                                              # 每個更新時間之後都試過了，還是舊的
        st["gave_up"] = True
        info["attempts"] = st["attempts"]
        return "gave_up", st, info
    if now >= pending[0]:
        st["done_until"] = now.timestamp()                       # 這個時間點與更早的都作廢
        st["attempts"] = int(st.get("attempts", 0)) + 1
        info["attempts"] = st["attempts"]
        return "restart", st, info
    return None, st, info


def done_message(st: dict[str, Any], resolved: dict[str, str]) -> tuple[str, str]:
    now_codes = "、".join(f"{p} → {c}" for p, c in sorted(resolved.items()))
    return (f"✅ 換月完成：近月合約已換成 {resolved.get('TMF') or next(iter(resolved.values()))}",
            f"{st.get('stale_code') or '舊合約'} 已在 {st.get('expiry')} 結算；重啟後訂閱的是：{now_codes}（第 {st.get('attempts')} 次嘗試）。")


def messages(action: str, info: dict[str, Any]) -> tuple[str, str]:
    e: date = info["expiry"]
    if action == "restart":
        return (f"🔄 換月重啟：{info['code']} 已結算（第 {info['attempts']}/{info['total']} 次）",
                f"{info['prefix']} 訂閱的 {info['code']} 已在 {e:%m/%d} 13:30 最後交易、結算；後端要重新登入才會取得下個月合約。"
                "watchdog 會正常關閉並重啟（約 1～2 分鐘；有持倉的話啟動後會再通知哪些有停損保護，策略不會自動回來）。沒有 watchdog 的話請手動重啟。")
    return ("🚨 換月失敗：重啟後近月合約還是舊的",
            f"{info['code']} 已在 {e:%m/%d} 結算，但在 14:46、17:16、隔天 07:51、08:01 各重啟一次之後，{info['prefix']} 解析到的還是 {info['code']}"
            "（可能是券商的合約檔還沒換）。請等合約檔更新後手動重啟後端；在那之前這個合約沒有報價、也不能下單。")


async def rollover_loop(get_resolved: Callable[[], dict[str, str]], path: Optional[Path] = None) -> None:
    """背景：每 30 秒看一次。get_resolved 由呼叫端提供（讀記憶體，不查券商）。例外全部吞掉。"""
    global _wanted
    if not enabled():
        logger.info("換月自動重啟已關閉（ROLLOVER_RESTART=false）")
        return
    path = path or state_path()
    warned = False
    while True:
        await asyncio.sleep(POLL_SEC)
        try:
            resolved = dict(get_resolved() or {})
            if not resolved:
                continue
            if not warned and any(reminders.code_month(str(c), datetime.now(TAIPEI).date()) is None for c in resolved.values()):
                warned = True
                logger.warning("換月：解析不出實際合約代碼（%s）；worker 沒有帶 target_code？這樣就不會自動換月，請在結算後手動重啟後端", resolved)
            st = await asyncio.to_thread(load_state, path)
            action, new_st, info = await asyncio.to_thread(evaluate, datetime.now(TAIPEI), resolved, reminders.is_open_day, st, time.time() - _STARTED)
            if new_st != st:
                await asyncio.to_thread(save_state, path, new_st)     # 先記下來再要求重啟：就算行程馬上被關，這個時間點也算用掉了
            if action is None:
                if info is None and new_st.get("attempts", 0) > 0 and new_st.get("expiry") and not new_st.get("confirmed"):
                    parsed = [c for c in resolved.values() if reminders.code_month(str(c), datetime.now(TAIPEI).date())]
                    if parsed:                                              # 重啟過、而且現在訂閱的合約都沒過期：換月完成，通知一次
                        new_st = {**new_st, "confirmed": True}
                        await asyncio.to_thread(save_state, path, new_st)
                        title, body = done_message(new_st, resolved)
                        logger.info("換月完成：%s", resolved)
                        notify(title, body, key=f"rollover:{new_st['expiry']}:done", cooldown=86400.0, priority="default")
                continue
            title, body = messages(action, info or {})
            if action == "restart":
                _wanted = "rollover"
                logger.warning("換月：%s 已結算，要求 watchdog 重啟（第 %s 次）", info["code"], info["attempts"])
                notify(title, body, key=f"rollover:{info['expiry']}:{info['attempts']}", cooldown=3600.0, priority="high")
            else:
                logger.error("換月失敗：%s 重啟 %s 次後還是舊合約", info["code"], info["attempts"])
                notify(title, body, key=f"rollover:{info['expiry']}:gaveup", cooldown=86400.0, priority="urgent")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("換月檢查例外（已忽略）")
