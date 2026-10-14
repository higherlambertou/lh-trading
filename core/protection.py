"""持倉保護狀態（純函式＋小檔案，update.md 發現 18、32）：誰沒有停損保護、持倉狀態檔、重啟／凍結時的警示訊息。

為什麼要有這層：停損停利只活在後端進程的記憶體裡（手動單的 ManualOrderMonitor、策略的狀態）；進程凍結或重啟的那段時間、
以及重啟之後沒人恢復它們的時候，券商帳上的持倉其實沒有任何保護。這裡不碰下單，只回答三個問題：
  1. 現在哪些持倉沒有人在看著停損？（`unprotected`，給畫面的橫幅用）
  2. watchdog 在後端凍結時要怎麼知道「有沒有持倉」？（後端每次刷新持倉就寫一個小檔案，watchdog 讀它，不必問已凍結的後端）
  3. 警示訊息怎麼寫（`build_watchdog_message`／`startup_message`）。
全部只依賴標準函式庫，`python -m core.notify watchdog` 在 watchdog 裡跑也不會載入券商 SDK。"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DIR_ZH = {"Buy": "多", "Sell": "空"}


def state_enabled() -> bool:
    return os.getenv("POSITION_STATE_FILE", "true").lower() == "true"


def state_path(mode: str) -> Path:
    custom = os.getenv("POSITION_STATE_PATH", "").strip()
    return Path(custom) if custom else DATA_DIR / f"position_state_{mode}.json"


def _watch_matches(w: dict[str, Any], code: str) -> bool:
    mc = str(w.get("match_code") or "")
    if not mc:
        return False
    return code == mc if w.get("is_option") else code.startswith(mc)


def unprotected(positions: list[dict[str, Any]], watches: list[dict[str, Any]], strategy_running: bool = False) -> list[dict[str, Any]]:
    """沒有人看著停損的持倉（每個持倉列算『還沒被覆蓋』的口數）。
    覆蓋 ＝ 一個『已經看到部位』、停損點數 > 0、沒有放棄平倉的手動監看（同合約、同方向；口數累加）；
    或策略執行中且持倉是微台（策略只交易 TMF；策略自己有沒有設停損是使用者的選擇，這裡不替它判斷）。
    只有停利、沒有停損的監看不算保護。"""
    out: list[dict[str, Any]] = []
    for p in positions or []:
        code, direction = str(p.get("code", "")), str(p.get("direction", ""))
        qty = int(p.get("quantity", 0) or 0)
        if qty <= 0 or not code:
            continue
        if strategy_running and code.startswith("TMF"):
            continue
        covered = 0
        for w in watches or []:
            if w.get("close_gave_up") or not w.get("seen", True) or int(w.get("stop_loss_pts") or 0) <= 0:
                continue
            if w.get("direction") == direction and _watch_matches(w, code):
                covered += int(w.get("remaining") or w.get("quantity") or 0)
        if qty - covered > 0:
            out.append({"code": code, "direction": direction, "quantity": qty - covered, "total": qty})
    return out


def describe(items: list[dict[str, Any]], qty_key: str = "quantity") -> str:
    """「TMFJ6 多 1 口、MXFJ6 空 2 口」"""
    return "、".join(f"{i.get('code', '?')} {DIR_ZH.get(str(i.get('direction')), '?')} {int(i.get(qty_key, 0) or 0)} 口" for i in items)


# ── 持倉狀態檔（watchdog 在後端凍結時讀它）──────────────────────────

def write_position_state(positions: list[dict[str, Any]], unprot: list[dict[str, Any]], mode: str, *, path: Optional[Path] = None,
                         now: Optional[float] = None) -> bool:
    """原子寫入（先寫暫存檔再 rename）；任何失敗都只記 log、回傳 False，絕不影響交易。"""
    path = path or state_path(mode)
    data = {"version": 1, "ts": now if now is not None else time.time(), "mode": mode,
            "positions": [{"code": p.get("code"), "direction": p.get("direction"), "quantity": int(p.get("quantity", 0) or 0), "price": p.get("price")}
                          for p in positions or []],
            "unprotected": unprot}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("持倉狀態檔寫不進去（watchdog 凍結時就不知道有沒有持倉）：%r", e)
        return False


def load_position_state(path: Path) -> Optional[dict[str, Any]]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("positions"), list) else None


def _age_text(sec: float) -> str:
    if sec < 90:
        return "不到 1 分鐘前"
    if sec < 3600:
        return f"{int(sec // 60)} 分鐘前"
    return f"{sec / 3600:.1f} 小時前"


# ── 警示訊息 ─────────────────────────────────────────────────────

_EVENT_TEXT = {"freeze": "後端凍結或斷線", "exit": "後端進程結束"}
_DETAIL_TEXT = {"down": "連 /health 都沒有回應", "nobroker": "沒有連上券商", "gone": "進程已不存在"}


def build_watchdog_message(event: str, detail: str, state: Optional[dict[str, Any]], now: Optional[float] = None) -> Optional[tuple[str, str]]:
    """watchdog 要重啟後端之前的警示。不知道持倉（沒有狀態檔）或最後已知沒有持倉 → None（不打擾）。
    狀態檔很舊但最後已知有持倉也照樣警示（寧可多提醒），並寫出是多久以前的資訊。"""
    if not state or not state.get("positions"):
        return None
    now = time.time() if now is None else now
    age = max(0.0, now - float(state.get("ts") or 0))
    held = describe(state["positions"])
    unprot = state.get("unprotected") or []
    title = f"⚠️ {_EVENT_TEXT.get(event, '後端異常')}，持倉暫時沒有停損保護"
    lines = [f"持倉：{held}（{_age_text(age)}的資料）。", f"原因：{_DETAIL_TEXT.get(detail, detail or '不明')}。",
             "watchdog 正在重啟後端；重新登入通常要 1～3 分鐘，停損要等重啟完成才會恢復。行情劇烈的話請先到券商 App 確認，必要時手動平倉。"]
    if unprot:
        lines.insert(1, f"（凍結前就沒有停損保護的：{describe(unprot)}）")
    return title, "\n".join(lines)


def _stop_price(w: dict[str, Any]) -> Optional[float]:
    entry, sl = float(w.get("entry_price") or 0), int(w.get("stop_loss_pts") or 0)
    if not entry or sl <= 0:
        return None
    return entry - sl if w.get("direction") in (1, "Buy") else entry + sl


def startup_message(positions: list[dict[str, Any]], restored: list[dict[str, Any]], unprot: list[dict[str, Any]], status: str = "ok") -> Optional[tuple[str, str]]:
    """後端（重新）啟動、對帳完之後的報告。沒有持倉就不打擾（None）。"""
    if not positions:
        return None
    lines = [f"持倉：{describe(positions)}。"]
    for w in restored:
        sp = _stop_price(w)
        tp = int(w.get("take_profit_pts") or 0)
        lines.append(f"已恢復手動停損監控：{w.get('match_code')} {DIR_ZH.get('Buy' if w.get('direction') in (1, 'Buy') else 'Sell')} {w.get('quantity')} 口，"
                     f"停損 {int(w.get('stop_loss_pts') or 0)} 點" + (f"（約 {sp:,.0f}）" if sp else "") + (f"、停利 {tp} 點" if tp else "") + "。")
    if status == "broker_unavailable":
        lines.append("券商一直連不上，停損監控還沒恢復。")
    if unprot:
        lines.append(f"沒有停損保護：{describe(unprot)}（這些口數沒有策略在跑、也沒有停損監控覆蓋）。後端不會自動重啟策略，請到儀表板重新設定停損或手動平倉。")
        return "⚠️ 後端已重啟：持倉沒有停損保護", "\n".join(lines)
    return "後端已重啟：持倉的停損監控已恢復", "\n".join(lines)
