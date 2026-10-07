"""每日市場狀態總結：整合 Hurst（第一層）+ IV（第二層）→ 策略對應表 → 每日日誌。

對應《市場狀態判斷系統.md》：
  - 每天開盤前（MARKET_STATE_TIME，預設 08:30）算一次今日狀態，盤中不改判斷
    （除非手動 refresh / 手動輸入 IV，視為「重大事件」）
  - 收盤後（MARKET_STATE_POST_TIME，預設 13:50）更新日 K 快取、補當日 IV、算當日振幅比
  - 每 30 秒取樣各策略的已實現損益，累加成「策略日績效」→ 日誌的結果欄與驗證統計
  - 預設只「顯示 + 警告」，不擋下單：策略啟動與建議不符時回警告（MARKET_STATE_GATE=off 關閉）

為什麼不直接併進 hurst_analyzer / iv_monitor：那兩個是純計算（好測、無 I/O），
這裡才有排程、資料庫、券商呼叫。券商呼叫一律走 broker 的 async 介面（event loop 鐵則）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from core.broker import broker
from core.hurst_analyzer import HURST_LABEL, TREND_TH, REVERT_TH, aggregate_daily, analyze, trend_direction
from core.iv_monitor import (
    HIGH_PCT, LOOKBACK, LOW_PCT, evaluate_iv, fetch_atm_iv,
)
from core.live_state import BIG_MOVE, FLOW_DOWN, FLOW_UP, QUIET, live_state
from core.market_store import MarketStore
from core.trade_log import trade_log

logger = logging.getLogger(__name__)

STATE_LABEL = {
    "TREND": "趨勢", "REVERT": "均值回歸", "UNCLEAR": "不明確",
    "IV_LOW": "IV 偏低", "IV_HIGH": "IV 偏高",
}
DIRECTION_LABEL = {1: "偏多", -1: "偏空", 0: "中性"}

# 策略風格（STRATEGIES.md 的分類）；scalp 兩邊都能用（靠 market_bias 偏向）
TREND_STYLE = frozenset({"ma_cross", "breakout", "momentum", "orb"})
REVERT_STYLE = frozenset({"rsi", "bollinger", "vwap_revert"})

KBAR_CHUNK_DAYS = 25        # 每次向券商查詢的日曆天數（避免單次太久卡住 worker）
MIN_BAR_COUNT = 30          # 日 K 當天少於這麼多根 1 分 K 視為殘缺，不存
TICK_SEC = 30


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Config:
    code: str = "TXF"
    window: int = 60
    trend_th: float = TREND_TH
    revert_th: float = REVERT_TH
    min_z: float = 0.0
    iv_low: float = LOW_PCT
    iv_high: float = HIGH_PCT
    iv_min_history: int = 60
    iv_lookback: int = LOOKBACK
    iv_required: bool = False
    iv_auto: bool = True
    gate: str = "warn"
    pre_hhmm: int = 830
    post_hhmm: int = 1350

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            window=int(_env("HURST_WINDOW", "60")),
            trend_th=float(_env("HURST_TREND_TH", str(TREND_TH))),
            revert_th=float(_env("HURST_REVERT_TH", str(REVERT_TH))),
            min_z=float(_env("HURST_MIN_Z", "0")),
            iv_low=float(_env("IV_LOW_PCT", str(LOW_PCT))),
            iv_high=float(_env("IV_HIGH_PCT", str(HIGH_PCT))),
            iv_min_history=int(_env("IV_MIN_HISTORY", "60")),
            iv_required=_env("IV_REQUIRED", "false").lower() == "true",
            iv_auto=_env("IV_AUTO", "true").lower() == "true",
            gate=_env("MARKET_STATE_GATE", "warn").lower(),
            pre_hhmm=int(_env("MARKET_STATE_TIME", "0830")),
            post_hhmm=int(_env("MARKET_STATE_POST_TIME", "1350")),
        )


# ── 策略對應表 ────────────────────────────────────────────────────

def combine(hurst_state: str, iv_state: str, iv_required: bool = False) -> tuple[str, list[str], str]:
    """文件的策略對應表 → (市場狀態, 建議策略, 說明)。
    IV 低/高優先（文件：任何 Hurst 狀態都建議選擇權）；IV 未知時預設只看 Hurst。"""
    if iv_state == "LOW":
        return "IV_LOW", [], "IV 偏低：市場過於平靜，考慮建立勒式等待大波動（選擇權，本系統不自動下單）"
    if iv_state == "HIGH":
        return "IV_HIGH", [], "IV 偏高：大波動已發生，考慮單向選擇權，不建跨式/勒式（期貨策略不建議）"
    if iv_state == "UNKNOWN" and iv_required:
        return "UNCLEAR", [], "IV 資料不足（IV_REQUIRED=true）：不操作，等待狀態清晰"
    if hurst_state == "TREND":
        return "TREND", ["scalp", "orb"], "scalp 偏順勢方向 + 考慮 orb"
    if hurst_state == "REVERT":
        return "REVERT", ["scalp", "vwap_revert"], "scalp 偏逆勢方向 + 考慮 vwap_revert"
    return "UNCLEAR", [], "不操作，等待狀態清晰"


def format_summary(s: dict[str, Any]) -> str:
    """文件規定的輸出格式（外加方向與備註）。"""
    h, iv = s["hurst"], s["iv"]
    hv = "—" if h["value"] is None else f"{h['value']:.2f}"
    z = "" if h["z"] is None else f"，z={h['z']:+.1f}"
    if iv["percentile"] is not None:
        ivt = f"{iv['percentile']:.0f}%（{iv['label']}）"
    else:
        ivt = f"—（{iv['label']}）"
    if iv.get("value") is not None:
        ivt += f"  ATM IV={iv['value']:.1f}%"
    lines = [
        f"=== 今日市場狀態 === {s['date']}",
        f"Hurst 指數：{hv}（{h['label']}{z}）",
        f"IV 百分位：{ivt}",
        f"方向：{s['direction_label']}（日K收盤 vs 20日均線）",
        f"建議策略：{s['hint']}",
    ]
    lines += [f"※ {n}" for n in s.get("notes", [])]
    lines.append("====================")
    return "\n".join(lines)


def build_summary(store: MarketStore, cfg: Config, today: date, mode: str, phase: str) -> dict[str, Any]:
    """純讀資料庫（日 K 快取 + IV 歷史）→ 今日市場狀態。同步函式，event loop 上請 to_thread。"""
    ds = today.isoformat()
    bars = store.bars(cfg.code, cfg.window, before=ds)          # 只用「今天以前」的日 K
    closes = [b["close"] for b in bars]
    hurst = analyze(closes, [b["date"] for b in bars], cfg.window, cfg.trend_th, cfg.revert_th, cfg.min_z)
    direction = trend_direction(closes)

    notes: list[str] = []
    cur = store.iv_on(ds) or store.iv_latest(ds)
    iv = evaluate_iv(
        cur["iv"] if cur else None, store.iv_history(ds, cfg.iv_lookback),
        min_history=cfg.iv_min_history, low=cfg.iv_low, high=cfg.iv_high, lookback=cfg.iv_lookback,
    )
    iv.update(value=cur["iv"] if cur else None, source=cur["source"] if cur else None,
              as_of=cur["date"] if cur else None)
    if cur and cur["date"] != ds:
        notes.append(f"今日尚無 IV，沿用 {cur['date']} 的值")
    if iv["state"] == "UNKNOWN" and not cfg.iv_required:
        notes.append("IV 層未就緒，僅依 Hurst 判斷")
    if hurst.note:
        notes.append(hurst.note)
    if hurst.value is not None and cfg.min_z <= 0 and hurst.state in ("TREND", "REVERT") \
            and hurst.z is not None and abs(hurst.z) < 1.0:
        notes.append(f"H 與隨機漫步差 <1σ（z={hurst.z:+.1f}，窗口 {hurst.window} 根雜訊 ±{hurst.se}）：此判斷統計上不顯著")

    state, strategies, hint = combine(hurst.state, iv["state"], cfg.iv_required)
    s = {
        "date": ds, "mode": mode, "phase": phase,
        "hurst": hurst.to_dict(),
        "iv": iv,
        "direction": direction, "direction_label": DIRECTION_LABEL[direction],
        "state": state,
        "state_label": f"{HURST_LABEL[hurst.state]} + IV{iv['label']}",     # 例：趨勢 + IV正常／IV累積中 0/60
        "strategies": strategies, "hint": hint,
        "notes": notes,
    }
    s["text"] = format_summary(s)
    return s


# ── 服務 ──────────────────────────────────────────────────────────

class MarketStateService:
    def __init__(self, store: MarketStore | None = None, cfg: Config | None = None,
                 broker_: Any = None) -> None:
        self.store = store or MarketStore()
        self.cfg = cfg or Config.from_env()
        self.broker = broker_ or broker
        self.summary: dict[str, Any] | None = None
        self._lock: asyncio.Lock | None = None
        self._pre_done = ""
        self._post_done = ""
        self._retry_at: dict[str, float] = {}
        # 策略取樣狀態
        self._last_pnl: dict[str, float] = {}
        self._last_trades: dict[str, int] = {}
        self._was_running: dict[str, bool] = {}
        self._row_day = ""
        self._row_seen: set[str] = set()
        self._avg_range_cache: tuple[str, float | None] = ("", None)

    @property
    def mode(self) -> str:
        return "sim" if os.getenv("SIMULATION", "true").lower() == "true" else "live"

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── 對外讀取（純記憶體）──────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        cfg = {
            "window": self.cfg.window, "trend_th": self.cfg.trend_th, "revert_th": self.cfg.revert_th,
            "min_z": self.cfg.min_z, "iv_min_history": self.cfg.iv_min_history,
            "iv_auto": self.cfg.iv_auto, "gate": self.cfg.gate,
            "pre_hhmm": self.cfg.pre_hhmm, "post_hhmm": self.cfg.post_hhmm,
        }
        s = self.summary
        if s is None:
            return {"ready": False, "mode": self.mode, "config": cfg}
        return {"ready": True, **s, "mode": self.mode, "config": cfg}

    def _set_summary(self, s: dict[str, Any]) -> None:
        """設定目前的今日判斷，並把市場狀態標記同步給成交紀錄（之後的委託會標上這組狀態）。"""
        self.summary = s
        try:
            trade_log.set_market_tag({
                "market_state": s.get("state"), "hurst_state": s["hurst"]["state"],
                "iv_state": s["iv"]["state"], "direction": s.get("direction"), "as_of": s.get("date"),
            })
        except Exception:
            logger.debug("set_market_tag 失敗（已忽略）", exc_info=True)

    # ── 盤中即時狀態（純記憶體 + 每天一次的小查詢）────────────────
    async def _avg_range(self) -> float | None:
        """近 20 個交易日的日盤平均振幅（點）；每天只查一次資料庫。"""
        today = date.today().isoformat()
        if self._avg_range_cache[0] == today:
            return self._avg_range_cache[1]
        bars = await asyncio.to_thread(self.store.bars, self.cfg.code, 25, today)
        rngs = [b["high"] - b["low"] for b in bars][-20:]
        val = round(sum(rngs) / len(rngs), 1) if len(rngs) >= 5 else None
        self._avg_range_cache = (today, val)
        return val

    async def live_snapshot(self) -> dict[str, Any]:
        """盤中即時狀態（真實成交的外/內盤比例、日盤振幅）+ 與盤前判斷是否同向。
        純顯示；判讀門檻見 live_state（未經驗證）。"""
        live = live_state.snapshot()
        today = date.today().isoformat()
        avg = await self._avg_range()
        rng = live["range"] if live["session_day"] == today else None      # 不是今天的日盤 → 不顯示
        ratio = round(rng / avg, 2) if rng is not None and avg else None
        range_label = None if ratio is None else ("大波動" if ratio >= BIG_MOVE else "清淡" if ratio <= QUIET else "正常")

        want, why = self.bias_direction(2)      # 盤前判斷偏向放行的方向：+1 做多／-1 做空／0 今日不偏向任何方向
        s = self._valid_summary()
        share = live["flow"]["100"]["share"]
        flow_dir = 0 if share is None else 1 if share >= FLOW_UP else -1 if share <= FLOW_DOWN else 0
        side = {1: "做多", -1: "做空"}
        flow_side = {1: "偏多（外盤主動）", -1: "偏空（內盤主動）", 0: "中性"}
        if want == 0:
            coherence, text = None, f"今日判斷不偏向任何方向：{why}"
        elif share is None:
            coherence, text = None, "尚無足夠的成交可判斷買賣力道"
        elif flow_dir == 0:
            coherence, text = 0, f"盤前偏向{side[want]}；現在買賣力道中性"
        else:
            coherence = 1 if flow_dir == want else -1
            text = f"{'協調' if coherence == 1 else '矛盾'}：盤前偏向{side[want]}，現在買賣力道{flow_side[flow_dir]}"
        return {
            **live, "ready": live["last"] is not None, "as_of": time.time(),
            "range": rng, "avg_range": avg, "range_ratio": ratio, "range_label": range_label,
            "pre": {"date": s["date"] if s else None, "state": s["state"] if s else None,
                    "hint": s["hint"] if s else None, "want": want, "want_reason": why},
            "flow_dir": flow_dir, "coherence": coherence, "coherence_text": text,
            "thresholds": {"flow_up": FLOW_UP, "flow_down": FLOW_DOWN, "big_move": BIG_MOVE, "quiet": QUIET},
        }

    def _valid_summary(self, max_age_days: int = 3) -> dict[str, Any] | None:
        s = self.summary
        if not s:
            return None
        try:
            age = (date.today() - date.fromisoformat(s["date"])).days
        except (KeyError, ValueError):
            return None
        return s if age <= max_age_days else None      # 容許週末/連假沿用上一個交易日的判斷

    # ── 策略啟動 gate / scalp 偏向 ───────────────────────────────
    def check_strategy(self, name: str) -> str | None:
        """啟動策略前呼叫：與今日市場狀態不符時回傳警告文字（不擋啟動）。"""
        if self.cfg.gate != "warn":
            return None
        s = self._valid_summary()
        if not s:
            return None
        st = s["state"]
        if st == "UNCLEAR":
            return f"今日市場狀態不明確（{s['hurst']['label']}）：文件建議不操作"
        if st in ("IV_LOW", "IV_HIGH"):
            return f"{s['hint']}"
        if st == "TREND" and name in REVERT_STYLE:
            return f"今日為趨勢狀態，{name} 屬逆勢策略（建議：{s['hint']}）"
        if st == "REVERT" and name in TREND_STYLE:
            return f"今日為均值回歸狀態，{name} 屬順勢策略（建議：{s['hint']}）"
        return None

    def bias_direction(self, bias: int) -> tuple[int, str]:
        """scalp market_bias → (允許的訊號方向 +1/-1, 理由)。方向 0 = 擋下所有進場，理由說明原因。
        bias: 1=順勢（跟日K方向）／-1=逆勢（反日K方向）／2=自動（趨勢→順勢、均值回歸→逆勢、其餘不進場）。"""
        s = self._valid_summary()
        if not s:
            return 0, "無近期市場狀態（market_bias 需要日K資料），不進場"
        mult = bias
        if bias == 2:
            if s["state"] == "TREND":
                mult = 1
            elif s["state"] == "REVERT":
                mult = -1
            else:
                return 0, f"今日市場狀態「{STATE_LABEL.get(s['state'], s['state'])}」，market_bias=自動 → 不進場"
        if s["direction"] == 0:
            return 0, "日K收盤貼近 20 日均線，無方向可偏，不進場"
        return s["direction"] * mult, ""

    # ── 資料同步（券商）──────────────────────────────────────────
    async def _sync_bars(self, include_today: bool) -> int:
        """增量向券商抓 1 分 K，合成日盤日 K 存進快取。回傳存入根數。"""
        code, now = self.cfg.code, datetime.now()
        today = now.date()
        need = self.cfg.window + 2
        have = await asyncio.to_thread(self.store.count_bars, code)
        last = await asyncio.to_thread(self.store.last_bar_date, code)
        if have < need or not last:
            start = today - timedelta(days=int(need * 1.6) + 20)
        else:
            start = date.fromisoformat(last) - timedelta(days=3)
        end = today + timedelta(days=1)             # end 包含/不包含皆可涵蓋今天
        complete_today = include_today or now.hour * 100 + now.minute >= 1346

        days: dict[str, dict[str, Any]] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=KBAR_CHUNK_DAYS), end)
            raw = await self.broker.kbars(code, cursor.isoformat(), chunk_end.isoformat())
            for b in aggregate_daily(raw):
                days[b["date"]] = b
            cursor = chunk_end + timedelta(days=1)

        ds = today.isoformat()
        bars = [b for b in days.values()
                if b["nbars"] >= MIN_BAR_COUNT and (b["date"] < ds or complete_today)]
        await asyncio.to_thread(self.store.upsert_bars, code, bars)
        # 日盤一天應有 ~300 根 1 分 K；平均偏離很多代表 kbars 的 ts 時間軸/時段假設有誤
        avg = sum(b["nbars"] for b in bars) / len(bars) if bars else 0
        logger.info("日K同步：%s ~ %s，存入 %d 根（平均 %.0f 分K/日，快取共 %d 根）", start, end, len(bars),
                    avg, await asyncio.to_thread(self.store.count_bars, code))
        return len(bars)

    async def _sample_iv(self) -> dict[str, Any]:
        res = await fetch_atm_iv(self.broker)
        today = date.today()
        if today.weekday() < 5:
            detail = json.dumps({k: res[k] for k in ("strike", "month", "call", "put", "forward", "days")})
            await asyncio.to_thread(self.store.upsert_iv, today.isoformat(), res["iv"], "shioaji", detail)
        logger.info("ATM IV=%.2f%%（%s 履約價 %s，距到期 %.0f 天）",
                    res["iv"], res["month"], res["strike"], res["days"])
        return res

    # ── 計算今日狀態 ─────────────────────────────────────────────
    async def refresh(self, phase: str = "manual", fetch: bool = True) -> dict[str, Any]:
        async with self._get_lock():
            notes: list[str] = []
            fetched = not fetch                    # 沒要求抓資料（手動 IV）視為完整
            if fetch:
                if not self.broker.is_connected:
                    notes.append("券商未連線：使用日K快取與已存 IV")
                else:
                    try:
                        await self._sync_bars(include_today=False)
                        fetched = True
                    except Exception as e:
                        logger.warning("日K同步失敗，改用快取: %r", e)
                        notes.append(f"日K同步失敗（用快取）：{e!r}")
                    if self.cfg.iv_auto:
                        try:
                            await self._sample_iv()
                        except Exception as e:
                            logger.warning("IV 自動抓取失敗: %r", e)
                            notes.append(f"IV 自動抓取失敗：{e!r}")

            today = date.today()
            s = await asyncio.to_thread(build_summary, self.store, self.cfg, today, self.mode, phase)
            s["notes"] = [*notes, *s["notes"]]
            if phase == "pre" and not fetched:
                s["phase"] = "early"               # 沒拿到最新日K：只算預判，排程會再試
            s["computed_at"] = time.time()
            s["text"] = format_summary(s)
            if today.weekday() < 5:                 # 週末不寫日誌
                await asyncio.to_thread(self._persist, s)
            self._set_summary(s)
            logger.info("市場狀態（%s）\n%s", phase, s["text"])
            return s

    def _persist(self, s: dict[str, Any]) -> None:
        self.store.upsert_journal(s["date"], self.mode, {
            "phase": s["phase"], "computed_at": s["computed_at"],
            "hurst": s["hurst"]["value"], "hurst_z": s["hurst"]["z"], "hurst_state": s["hurst"]["state"],
            "iv": s["iv"].get("value"), "iv_pct": s["iv"]["percentile"], "iv_state": s["iv"]["state"],
            "direction": s["direction"], "market_state": s["state"], "strategy_hint": s["hint"],
            "summary_json": json.dumps(s, ensure_ascii=False),
        })

    async def set_manual_iv(self, iv: float, day: str | None = None) -> dict[str, Any]:
        """手動輸入 ATM IV（%）。輸入今日 → 重算今日狀態；輸入過去日期 → 只回填歷史。"""
        today = date.today().isoformat()
        ds = day or today
        await asyncio.to_thread(self.store.upsert_iv, ds, iv, "manual", "")
        if ds != today and self.summary is not None:
            return self.summary
        return await self.refresh("manual", fetch=False)

    # ── 啟動 / 排程 ──────────────────────────────────────────────
    async def startup(self) -> None:
        now = datetime.now()
        ds = now.date().isoformat()
        row = await asyncio.to_thread(self.store.get_journal, ds, self.mode)
        if row and row.get("summary_json"):                 # 重啟不重算：盤中判斷維持不變
            self._set_summary(json.loads(row["summary_json"]))
            if row.get("phase") in ("pre", "manual"):
                self._pre_done = ds
            logger.info("載入今日已存的市場狀態（phase=%s）", row.get("phase"))
            return
        if now.weekday() >= 5:
            latest = await asyncio.to_thread(self.store.latest_journal, self.mode)
            if latest:
                self._set_summary(json.loads(latest["summary_json"]))
                return
        phase = "pre" if now.hour * 100 + now.minute >= self.cfg.pre_hhmm else "early"
        s = await self.refresh(phase)
        if s["phase"] == "pre":
            self._pre_done = ds

    def _retry_ok(self, key: str) -> bool:
        return time.time() >= self._retry_at.get(key, 0.0)

    async def tick(self, strategies: dict[str, Any]) -> None:
        """每 TICK_SEC 秒由背景迴圈呼叫：策略取樣 + 盤前/盤後排程。"""
        try:
            await self.sample_strategies(strategies)
        except Exception as e:
            logger.warning("策略日績效取樣失敗: %r", e)

        now = datetime.now()
        ds, hhmm = now.date().isoformat(), now.hour * 100 + now.minute
        if now.weekday() >= 5:
            return

        if hhmm >= self.cfg.pre_hhmm and self._pre_done != ds and self._retry_ok("pre"):
            try:
                row = await asyncio.to_thread(self.store.get_journal, ds, self.mode)
                if row and row.get("summary_json") and row.get("phase") in ("pre", "manual"):
                    self._set_summary(json.loads(row["summary_json"]))
                    self._pre_done = ds
                else:
                    s = await self.refresh("pre")
                    if s["phase"] == "pre":
                        self._pre_done = ds
                    else:                          # 沒拿到最新日K（券商未連線/查詢失敗）
                        self._retry_at["pre"] = time.time() + 300
                        logger.warning("盤前判斷未取得最新日K，5 分鐘後重試")
            except Exception as e:
                self._retry_at["pre"] = time.time() + 300
                logger.warning("盤前市場狀態計算失敗，5 分鐘後重試: %r", e)

        if hhmm >= self.cfg.post_hhmm and self._post_done != ds and self._retry_ok("post"):
            try:
                await self._post_close(ds)
                self._post_done = ds
            except Exception as e:
                self._retry_at["post"] = time.time() + 300
                logger.warning("盤後更新失敗，5 分鐘後重試: %r", e)

    async def _post_close(self, ds: str) -> None:
        """收盤後：更新日 K 快取（含今日）、補當日 IV、記錄當日振幅比。不重算今日判斷。"""
        if not self.broker.is_connected:
            raise RuntimeError("券商未連線")
        await self._sync_bars(include_today=True)
        if self.cfg.iv_auto:
            try:
                await self._sample_iv()
            except Exception as e:
                logger.warning("盤後 IV 抓取失敗: %r", e)
        await asyncio.to_thread(self._update_range_ratio, ds)

    def _update_range_ratio(self, ds: str) -> None:
        bars = self.store.bars(self.cfg.code, 25)
        today_bar = next((b for b in bars if b["date"] == ds), None)
        prev = [b["high"] - b["low"] for b in bars if b["date"] < ds][-20:]
        if not today_bar or len(prev) < 5:
            return
        avg = sum(prev) / len(prev)
        if avg > 0:
            self.store.set_range_ratio(ds, self.mode, round((today_bar["high"] - today_bar["low"]) / avg, 2))

    # ── 策略日績效取樣 ───────────────────────────────────────────
    async def sample_strategies(self, strategies: dict[str, Any]) -> None:
        """把各策略 state.realized_pnl / _trades_today 的「增量」累加進當日 strategy_day。
        用增量而非絕對值：策略 state 只活在記憶體，進程重啟會歸零。"""
        ds = date.today().isoformat()
        if self._row_day != ds:
            self._row_day, self._row_seen = ds, set()
        for name, s in strategies.items():
            running = bool(s.state.is_running)
            pnl = float(s.state.realized_pnl)
            trades = int(getattr(s, "_trades_today", 0))
            if running and not self._was_running.get(name):
                self._last_trades[name] = 0             # start() 會把 _trades_today 歸零
            self._was_running[name] = running
            d_pnl = pnl - self._last_pnl.get(name, 0.0)
            last_t = self._last_trades.get(name, 0)
            d_trades = trades - last_t if trades >= last_t else trades
            self._last_pnl[name], self._last_trades[name] = pnl, trades
            first_seen = running and name not in self._row_seen
            if d_pnl or d_trades or first_seen:
                await asyncio.to_thread(self.store.add_strategy_day, ds, self.mode, name, d_trades, d_pnl)
                self._row_seen.add(name)


market_state = MarketStateService()


def _main() -> None:  # pragma: no cover
    """python -m core.daily_summary：只讀資料庫，印出今日市場狀態（不連券商）。"""
    from dotenv import load_dotenv
    load_dotenv()
    cfg = Config.from_env()
    mode = "sim" if os.getenv("SIMULATION", "true").lower() == "true" else "live"
    print(build_summary(MarketStore(), cfg, date.today(), mode, "cli")["text"])


if __name__ == "__main__":  # pragma: no cover
    _main()
