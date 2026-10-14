import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from core.broker import broker
from core.daytrade import close_octype
from core.quote_hub import quote_hub
from core.trade_log import current_mode, trade_log

logger = logging.getLogger(__name__)

# 平倉單的確認與重送：送出後不立刻移除監看，確認結果才決定移除或重送
MAX_CLOSE_ATTEMPTS = 8        # 平倉單最多送幾次（每次重送前都先確認前一張的結果）
CLOSE_RETRY_MIN_GAP = 2.0     # 前一張確定沒成交（取消/失敗）後，至少隔幾秒才重送
CLOSE_FILLED_GRACE = 5.0      # 前一張有成交但這個監看還有口數沒平：等持倉刷新再補平剩餘
CLOSE_LIVE_TIMEOUT = 15.0     # IOC 不該掛這麼久：狀態一直是「處理中」超過這麼久，視為沒成交
CLOSE_UNKNOWN_TIMEOUT = 10.0  # 送單逾時/查不到委託狀態（結果不明）：等這麼久再決定


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def default_state_path() -> Path | None:
    """手動停損停利監控落地的檔案（重啟後和券商部位對帳、恢復；update.md 發現 32）。
    MANUAL_WATCH_PERSIST=false 關閉；正式盤與模擬盤各一個檔（兩個進程共用 data/）；MANUAL_WATCH_STATE 可指定路徑。"""
    if os.getenv("MANUAL_WATCH_PERSIST", "true").lower() != "true":
        return None
    custom = os.getenv("MANUAL_WATCH_STATE", "").strip()
    return Path(custom) if custom else DATA_DIR / f"manual_watches_{current_mode()}.json"


def _txo_round_tick(p: float) -> float:
    """把選擇權權利金 round 到合法跳動點。"""
    if p < 10:
        return round(p * 10) / 10
    if p < 50:
        return round(p * 2) / 2
    if p < 500:
        return float(round(p))
    if p < 1000:
        return float(round(p / 5) * 5)
    return float(round(p / 10) * 10)


@dataclass
class ManualWatch:
    id: str
    contract: str
    direction: int
    quantity: int
    entry_price: float
    stop_loss_pts: int
    take_profit_pts: int
    order_id: str = ""
    seen: bool = False
    waited: int = 0
    # 選擇權專用
    is_option: bool = False
    match_code: str = ""
    multiplier: float = 0.0
    exit_buffer_pts: float = 0.0
    # 選擇權合約識別（取代原 contract_obj）
    delivery_month: str = ""
    strike_price: int = 0
    option_right: str = ""
    option_category: str = "TXO"
    # 平倉追蹤：送出平倉單後不立刻移除監看，確認結果才決定移除或重送
    remaining: int = 0             # 這個監看還要平掉幾口（0 = 尚未設定，視為 quantity）
    close_order_id: str = ""       # 最近一張尚未結算的平倉單 id（空 = 沒有）
    close_qty: int = 0             # 那張平倉單的口數
    close_sent_at: float = 0.0
    close_attempts: int = 0        # 已送出幾次平倉單（重送時選擇權限價會逐次放寬）
    retry_after: float = 0.0       # 在此（monotonic）時間之前不可再送平倉單
    close_gave_up: bool = False    # 超過 MAX_CLOSE_ATTEMPTS 仍未平倉：不再自動重送，需人工處理
    octype: str = "Auto"           # 期貨平倉單的倉別：開倉勾了當沖（DayTrade）就沿用（見 core/daytrade.py）；選擇權固定 Auto
    created_at: float = 0.0        # 登記時間（epoch 秒）：重啟後多個監看分配同一個部位的口數時，依建立先後


# 落地的欄位：只存「使用者設定了什麼」；平倉追蹤（close_*、retry_after…）是暫時狀態，重啟後從頭來
_SAVED_FIELDS = ("id", "contract", "direction", "quantity", "entry_price", "stop_loss_pts", "take_profit_pts", "order_id",
                 "is_option", "match_code", "multiplier", "exit_buffer_pts", "delivery_month", "strike_price", "option_right",
                 "option_category", "octype", "created_at")


def _to_saved(w: "ManualWatch") -> dict:
    d = {k: getattr(w, k) for k in _SAVED_FIELDS}
    d["quantity"] = w.remaining or w.quantity            # 已經平掉一部分的，只存還剩的口數
    return d


def _valid_saved(w: object) -> bool:
    return (isinstance(w, dict) and isinstance(w.get("id"), str) and bool(w.get("id")) and w.get("direction") in (1, -1)
            and isinstance(w.get("match_code"), str) and bool(w.get("match_code"))
            and isinstance(w.get("quantity"), int) and not isinstance(w.get("quantity"), bool) and w["quantity"] >= 1
            and isinstance(w.get("stop_loss_pts"), int) and w["stop_loss_pts"] >= 0
            and isinstance(w.get("take_profit_pts"), int) and w["take_profit_pts"] >= 0
            and isinstance(w.get("entry_price"), (int, float)) and not isinstance(w.get("entry_price"), bool))


def _load_saved(path: Path) -> list[dict]:
    """讀落地檔。讀不懂的整個檔改名成 .bad-時間 保留（不刪、也不拿來恢復）；讀得懂但某一筆壞掉就只略過那一筆。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logger.error("手動停損停利存檔讀不懂（%r）：已改名保留，這次的停損監控不會恢復，請檢查持倉！", e)
        try:
            path.rename(path.with_name(f"{path.name}.bad-{int(time.time())}"))
        except OSError:
            pass
        return []
    items = raw.get("watches") if isinstance(raw, dict) else None
    out = []
    for w in items if isinstance(items, list) else []:
        if _valid_saved(w):
            out.append(w)
        else:
            logger.warning("手動停損停利存檔裡有一筆格式不對，略過：%r", w)
    return out


def reconcile(saved: list[dict], positions: list[dict], live_orders: set[str]) -> tuple[list[dict], list[dict]]:
    """把落地的監看和券商『現在』的部位對帳。回傳 (要恢復的, 丟掉的)；丟掉的帶 reason。
    規則（重點是不能把舊的停損掛到不相干的新部位上）：
      · 同一個部位（合約代碼＋方向，與 _check 用同一個比對、同樣取第一個符合的部位列）的口數，由監看依建立先後分配，
        每個監看最多拿到自己的口數；
      · 部位不在、或口數已被前面的監看用完 → 開倉單還掛在券商（live_orders）就留著等成交，否則丟掉。"""
    rows = [{"code": str(p.get("code", "")), "direction": str(p.get("direction", "")), "left": int(p.get("quantity", 0) or 0)} for p in positions or []]
    restored: list[dict] = []
    dropped: list[dict] = []
    for w in sorted(saved, key=lambda x: float(x.get("created_at") or 0)):
        direction = "Buy" if w["direction"] == 1 else "Sell"
        match = w["match_code"]
        row = next((r for r in rows if r["direction"] == direction and (r["code"] == match if w.get("is_option") else r["code"].startswith(match))), None)
        take = min(int(w["quantity"]), row["left"]) if row else 0
        if take >= 1:
            row["left"] -= take
            restored.append({**w, "quantity": take, "seen": True})
        elif w.get("order_id") and w["order_id"] in live_orders:
            restored.append({**w, "seen": False})
        else:
            dropped.append({**w, "reason": "部位已不存在" if row is None else "部位口數已被其他監看用完"})
    return restored, dropped


def _from_saved(d: dict) -> "ManualWatch":
    known = {f.name for f in fields(ManualWatch)}
    w = ManualWatch(**{k: v for k, v in d.items() if k in known and k != "remaining"})
    w.remaining = w.quantity                              # 重啟後這個監看要平的口數 ＝ 對帳後分到的口數
    return w


class ManualOrderMonitor:
    def __init__(self) -> None:
        self._watches: dict[str, ManualWatch] = {}
        self._task: asyncio.Task | None = None
        self._clock = time.monotonic          # 測試可替換
        self.state_path: Path | None = None   # 落地檔；None ＝ 不落地（測試預設；後端啟動時 setup() 才指定）
        self.last_restore: dict | None = None

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self.state_path = default_state_path()
        self._task = loop.create_task(self._poll_loop())
        logger.info("ManualOrderMonitor 已啟動（停損監控落地：%s）", self.state_path or "關閉")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ManualOrderMonitor 已關閉")

    def add(
        self,
        contract: str,
        direction: int,
        quantity: int,
        entry_price: float,
        stop_loss_pts: int,
        take_profit_pts: int,
        order_id: str = "",
        is_option: bool = False,
        match_code: str = "",
        multiplier: float = 0.0,
        exit_buffer_pts: float = 0.0,
        delivery_month: str = "",
        strike_price: int = 0,
        option_right: str = "",
        option_category: str = "TXO",
        octype: str = "Auto",
    ) -> str:
        watch_id = str(uuid.uuid4())[:8]
        self._watches[watch_id] = ManualWatch(
            id=watch_id,
            contract=contract,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss_pts=stop_loss_pts,
            take_profit_pts=take_profit_pts,
            order_id=order_id,
            is_option=is_option,
            match_code=match_code or contract,
            multiplier=multiplier,
            exit_buffer_pts=exit_buffer_pts,
            delivery_month=delivery_month,
            strike_price=strike_price,
            option_right=option_right,
            option_category=option_category,
            octype=octype,
            created_at=time.time(),
        )
        self._save()
        # 選擇權：立刻訂閱報價（fire-and-forget，不阻塞）
        if is_option and delivery_month and strike_price and option_right:
            broker.subscribe_option_sync(delivery_month, strike_price, option_right, option_category)
        logger.info(
            "ManualWatch 登記: id=%s %s %s SL=%d TP=%d%s",
            watch_id, match_code or contract, "多/買" if direction == 1 else "空/賣",
            stop_loss_pts, take_profit_pts,
            "（選擇權）" if is_option else "",
        )
        return watch_id

    def remove(self, watch_id: str) -> None:
        if self._watches.pop(watch_id, None) is not None:
            self._save()

    def update(
        self,
        watch_id: str,
        stop_loss_pts: int | None = None,
        take_profit_pts: int | None = None,
    ) -> bool:
        watch = self._watches.get(watch_id)
        if not watch:
            return False
        if stop_loss_pts is not None:
            watch.stop_loss_pts = stop_loss_pts
        if take_profit_pts is not None:
            watch.take_profit_pts = take_profit_pts
        self._save()
        logger.info(
            "ManualWatch 更新: id=%s SL=%d TP=%d",
            watch_id, watch.stop_loss_pts, watch.take_profit_pts,
        )
        return True

    def list_watches(self) -> list[dict]:
        return [
            {
                "id": w.id,
                "contract": w.contract,
                "direction": "Buy" if w.direction == 1 else "Sell",
                "quantity": w.quantity,
                "entry_price": w.entry_price,
                "stop_loss_pts": w.stop_loss_pts,
                "take_profit_pts": w.take_profit_pts,
                "is_option": w.is_option,
                "match_code": w.match_code,
                "close_attempts": w.close_attempts,
                "close_gave_up": w.close_gave_up,
                "octype": w.octype,
                "seen": w.seen,                                   # 已經在券商部位裡看到了（還是在等開倉單成交）
                "remaining": w.remaining or w.quantity,           # 這個監看還要平幾口
            }
            for w in self._watches.values()
        ]

    # ── 落地與重啟後恢復（update.md 發現 32）──────────────────────────
    # 手動停損停利以前只存在記憶體，後端重啟（含 watchdog 在凍結後的自動重啟）就消失，持倉從此沒有任何保護。
    # 現在每次登記、修改、移除都把「使用者設定了什麼」寫進檔案；重啟後和券商『現在』的部位對帳再恢復（reconcile）。

    def _save(self) -> None:
        """原子寫入（暫存檔 → rename）。任何失敗只記 ERROR、不丟例外：存不了不能讓下單或監看出事。"""
        path = self.state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "saved_at": time.time(), "watches": [_to_saved(w) for w in self._watches.values()]},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.error("手動停損停利監控存檔失敗（重啟後不會自動恢復，請留意）: %r", e)

    async def _fetch_positions(self, wait_sec: float, poll_sec: float) -> list[dict] | None:
        """等券商連上並查到部位；一直不行（逾時）回 None。"""
        deadline = self._clock() + wait_sec
        while True:
            if broker.is_connected:
                try:
                    return list(await asyncio.wait_for(broker.list_positions(), timeout=8) or [])
                except Exception as e:  # noqa: BLE001
                    logger.warning("恢復手動停損：查詢券商部位失敗（%r），稍後重試", e)
            if self._clock() >= deadline:
                return None
            await asyncio.sleep(poll_sec)

    async def restore(self, positions: list[dict] | None = None, *, wait_sec: float = 600.0, poll_sec: float = 2.0) -> dict:
        """後端（重新）啟動後呼叫一次：把落地的監看和券商目前的部位對帳，恢復還有部位的、丟掉沒有部位的（reconcile 的規則）。
        positions 沒給就自己等券商連上、查部位（逾時回 status=broker_unavailable，落地檔原封不動，下次啟動再試）。
        回傳 {status: none|ok|broker_unavailable, restored, dropped, positions}；不丟例外給呼叫端以外的人。"""
        saved = _load_saved(self.state_path) if self.state_path else []
        report: dict = {"status": "none", "restored": [], "dropped": [], "positions": list(positions or []), "saved": len(saved)}
        if positions is None:
            positions = await self._fetch_positions(wait_sec, poll_sec)
            if positions is None:
                logger.error("手動停損恢復：%.0f 秒內等不到券商部位，落地的 %d 個監看先原封不動保留", wait_sec, len(saved))
                report["status"] = "broker_unavailable" if saved else "none"
                self.last_restore = report
                return report
            report["positions"] = positions
        if not saved:
            self.last_restore = report
            return report
        live: set[str] = set()
        if any(w.get("order_id") for w in saved):               # 開倉單還掛著的（限價單等成交）才需要查委託狀態
            try:
                trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=8)
                live = {tid for tid, st in self._index_trades(trades)[0].items() if st in self._LIVE_ORDER_STATES}
            except Exception as e:  # noqa: BLE001
                logger.warning("恢復手動停損：查詢委託狀態失敗（%r），沒有部位的監看一律丟掉", e)
        restored, dropped = reconcile(saved, positions, live)
        installed = []
        for d in restored:
            if d["id"] in self._watches:                        # 後端剛起來就有人登記了同一個（不應該發生）：以記憶體為準
                continue
            self._watches[d["id"]] = _from_saved(d)
            installed.append(d)
        for d in dropped:
            logger.warning("手動停損停利監控 [%s] %s 不恢復（%s）", d["id"], d["match_code"], d["reason"])
        for d in installed:
            logger.info("手動停損停利監控 [%s] 已恢復：%s %s %d 口 停損 %d 點 停利 %d 點", d["id"], d["match_code"], "多" if d["direction"] == 1 else "空",
                        d["quantity"], d["stop_loss_pts"], d["take_profit_pts"])
        self._save()                                            # 對帳後的結果寫回去（沒有部位的舊監看從檔案裡消失）
        report.update(status="ok", restored=installed, dropped=dropped)
        self.last_restore = report
        return report

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if not self._watches:
                continue
            try:
                positions = await asyncio.wait_for(broker.list_positions(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("ManualMonitor 查詢部位逾時")
                continue
            except Exception as e:
                logger.warning("ManualMonitor 查詢部位失敗: %s", e)
                continue

            order_status: dict[str, str] = {}
            order_fills: dict[str, int] = {}
            if self._needs_order_status():
                try:
                    trades = await asyncio.wait_for(broker.list_trades_with_status(), timeout=5)
                    order_status, order_fills = self._index_trades(trades)
                except asyncio.TimeoutError:
                    logger.warning("ManualMonitor 查詢委託狀態逾時")
                except Exception as e:
                    logger.warning("ManualMonitor 查詢委託狀態失敗: %s", e)

            for watch in list(self._watches.values()):
                await self._check(watch, positions, order_status, order_fills)

    _LIVE_ORDER_STATES = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
    _DEAD_ORDER_STATES = {"Cancelled", "Canceled", "Failed", "Inactive", "Expired"}

    def _needs_order_status(self) -> bool:
        """有監看還在等委託單的結果（開倉單尚未成交，或平倉單送出後要確認）才需要查委託狀態。"""
        return any((not w.seen) or w.close_attempts > 0 for w in self._watches.values())

    @staticmethod
    def _index_trades(trades: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
        """委託清單 → ({id: 狀態}, {id: 已成交口數})。"""
        status: dict[str, str] = {}
        fills: dict[str, int] = {}
        for t in trades:
            tid = str(t.get("id", "") or "")
            if tid:
                status[tid] = str(t.get("status", ""))
                fills[tid] = int(t.get("deal_quantity", 0) or 0)
        return status, fills

    async def _check(
        self, watch: ManualWatch, positions: list[dict], order_status: dict[str, str],
        order_fills: dict[str, int] | None = None,
    ) -> None:
        # 選擇權：確保仍在訂閱（重連後自動補回）
        if watch.is_option and watch.delivery_month and watch.strike_price and watch.option_right:
            broker.subscribe_option_sync(
                watch.delivery_month, watch.strike_price,
                watch.option_right, watch.option_category,
            )

        target_dir = "Buy" if watch.direction == 1 else "Sell"

        def _code_match(code: str) -> bool:
            return code == watch.match_code if watch.is_option else code.startswith(watch.match_code)

        pos = next(
            (
                p for p in positions
                if _code_match(p.get("code", ""))
                and p.get("direction", "") == target_dir
            ),
            None,
        )

        if pos is None:
            if watch.seen:
                logger.info("手動監控 [%s] 部位已平倉，移除監控", watch.id)
                self.remove(watch.id)
                return

            status = order_status.get(watch.order_id) if watch.order_id else None
            if status in self._LIVE_ORDER_STATES:
                watch.waited = 0
                return
            if status in self._DEAD_ORDER_STATES:
                logger.info(
                    "手動監控 [%s] 委託單已取消/失效（%s）且未成交，移除監控",
                    watch.id, status,
                )
                self.remove(watch.id)
                return
            watch.waited += 1
            if watch.waited >= 600:
                logger.info("手動監控 [%s] 逾 10 分鐘無單無部位，移除監控", watch.id)
                self.remove(watch.id)
            return

        watch.seen = True
        watch.waited = 0

        if watch.entry_price == 0:
            watch.entry_price = float(pos.get("price", 0))
            self._save()
            return

        # 先結算上一張平倉單：全數成交 → 這個監看的任務完成（持倉裡剩下的口數屬於別的單，不能動）
        if watch.close_order_id and self._settle(watch, order_status, order_fills or {}):
            logger.info("手動監控 [%s] 平倉單已全數成交，移除監控", watch.id)
            self.remove(watch.id)
            return

        if watch.is_option:
            cached = quote_hub.get_last_price(watch.match_code)
            if cached is None:
                return
            current_price = float(cached)
        else:
            current_price = float(pos.get("last_price") or pos.get("price", 0))

        pts = (current_price - watch.entry_price) * watch.direction

        triggered = False
        reason = ""
        kind = ""
        if watch.take_profit_pts > 0 and pts >= watch.take_profit_pts:
            triggered = True
            reason = f"停利 +{pts:.0f}點"
            kind = "tp"
        elif watch.stop_loss_pts > 0 and pts <= -watch.stop_loss_pts:
            triggered = True
            reason = f"停損 {pts:.0f}點"
            kind = "sl"

        if triggered:
            await self._try_close(watch, pos, kind, reason, current_price)

    def _settle(self, watch: ManualWatch, order_status: dict[str, str], order_fills: dict[str, int]) -> bool:
        """結算最近一張平倉單：有結果就記帳（扣掉已成交口數）並決定何時能重送。
        回傳 True = 這個監看要平的口數已全數成交。"""
        oid, now = watch.close_order_id, self._clock()
        status = order_status.get(oid)
        if status == "Filled" or status in self._DEAD_ORDER_STATES:
            # Filled = 全部成交；取消/失敗之前可能已部分成交（IOC 常見）
            filled = order_fills.get(oid, watch.close_qty if status == "Filled" else 0)
            watch.remaining = max(0, (watch.remaining or watch.quantity) - filled)
            watch.close_order_id, watch.close_qty = "", 0
            if watch.remaining == 0:
                return True
            # 有成交（持倉可能還沒刷新）等久一點再補平剩餘；完全沒成交就很快重送
            watch.retry_after = now + (CLOSE_FILLED_GRACE if filled else CLOSE_RETRY_MIN_GAP)
        elif status in self._LIVE_ORDER_STATES:
            if now - watch.close_sent_at >= CLOSE_LIVE_TIMEOUT:      # IOC 不該處理這麼久
                watch.close_order_id, watch.close_qty = "", 0
                watch.retry_after = now
        elif now - watch.close_sent_at >= CLOSE_UNKNOWN_TIMEOUT:     # 查不到這張單的狀態
            watch.close_order_id, watch.close_qty = "", 0
            watch.retry_after = now
        return False

    async def _try_close(self, watch: ManualWatch, pos: dict, kind: str, reason: str,
                         current_price: float) -> None:
        """停損/停利觸發：送平倉單。前一張還在等結果、冷卻中、或已放棄時不送，避免重複平倉變成反向開倉。"""
        if watch.close_order_id or watch.close_gave_up:
            return
        if self._clock() < watch.retry_after:
            return
        if watch.close_attempts >= MAX_CLOSE_ATTEMPTS:
            watch.close_gave_up = True
            logger.error("手動監控 [%s] 平倉單已送 %d 次仍未平倉，不再自動重送，請立刻手動處理！",
                         watch.id, watch.close_attempts)
            return
        remaining = watch.remaining or watch.quantity
        pos_qty = int(pos.get("quantity", 0) or 0)
        qty = min(remaining, pos_qty) if pos_qty > 0 else remaining      # 不超過這個監看剩下的口數與當下部位
        if watch.close_attempts:
            logger.warning("手動下單 [%s] %s @ %.0f，前一張平倉單未成交，第 %d 次重送（%d 口）",
                           watch.id, reason, current_price, watch.close_attempts + 1, qty)
        else:
            logger.info("手動下單 [%s] %s @ %.0f，執行平倉", watch.id, reason, current_price)
        # 成交紀錄用：設定的停損/停利價位與觸發當下的價格（滑價 = 實際成交價 vs 這些）
        ref = watch.entry_price + watch.direction * (
            watch.take_profit_pts if kind == "tp" else -watch.stop_loss_pts)
        await self._close(watch, kind, ref, current_price, qty)

    async def _close(self, watch: ManualWatch, kind: str = "exit", ref_price: float | None = None,
                     signal_price: float | None = None, qty: int | None = None) -> None:
        """送出一張平倉單。送出後**不移除監看**：由 _check 確認結果（全數成交才移除、沒成交再重送）。"""
        close_action = "Sell" if watch.direction == 1 else "Buy"
        qty = int(qty or watch.quantity)
        attempt = watch.close_attempts              # 0 = 第一次
        now = self._clock()
        watch.close_attempts += 1
        # 先設好「結果不明時」的保護：就算送單丟例外/逾時（單可能已送達券商），也要等一下才重送，避免重複平倉
        watch.close_order_id, watch.close_qty = "", 0
        watch.retry_after = now + CLOSE_UNKNOWN_TIMEOUT

        try:
            # kind / ref_price / signal_price 只給成交紀錄用，不影響下單
            with trade_log.context(strategy="manual_monitor", reason=kind,
                                   signal_price=signal_price, ref_price=ref_price, attempt=watch.close_attempts):
                if watch.is_option and watch.delivery_month:
                    last = quote_hub.get_last_price(watch.match_code) or watch.entry_price
                    # 選擇權平倉是限價 IOC：沒成交時重送會逐次放寬緩衝（每次再讓 ~1% 權利金），提高成交機會
                    buf = (watch.exit_buffer_pts or 0) + attempt * max(1.0, round(last * 0.01, 1))
                    raw = last - buf if watch.direction == 1 else last + buf
                    limit_price = _txo_round_tick(max(0.1, raw))
                    res = await broker.place_option_order(
                        delivery_month=watch.delivery_month,
                        strike=watch.strike_price,
                        right=watch.option_right,
                        category=watch.option_category,
                        action=close_action,
                        quantity=qty,
                        price=limit_price,
                        order_type="IOC",
                    )
                else:
                    res = await broker.place_order(
                        contract_code=watch.contract,
                        action=close_action,
                        quantity=qty,
                        price=0,
                        price_type="MKT",
                        order_type="IOC",
                        octype=close_octype(watch.octype),     # 預設 Auto（不變）；開倉是當沖就用當沖平倉，日盤以外退回 Auto
                    )
            trade_id = str(res.get("trade_id", "") or "") if isinstance(res, dict) else ""
            watch.close_order_id, watch.close_qty, watch.close_sent_at = trade_id, qty, now
            logger.info("手動停損停利平倉單已送出 [%s]（第 %d 次，%d 口）trade_id=%s，等待成交確認",
                        watch.id, watch.close_attempts, qty, trade_id or "?")
        except Exception as e:
            logger.error("手動停損停利平倉失敗 [%s]（第 %d 次，結果不明，%.0f 秒後確認部位再決定是否重送）: %s",
                         watch.id, watch.close_attempts, CLOSE_UNKNOWN_TIMEOUT, e)


manual_monitor = ManualOrderMonitor()
