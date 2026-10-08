"""盤中即時狀態（純記憶體、僅顯示、不影響下單）：外/內盤比例、日盤振幅。

外/內盤比例只算「真實成交」：實測 TMF 的行情事件只有約 27% 是真的成交（volume>0），
其餘是帶著上一筆成交 tick_type 的報價更新；全部計入的話，比例會被「重複計算的上一筆方向」主導
（scalp 目前就是這樣統計，見 update.md「發現」）。
判定「真實成交」：volume>0 且 total_volume 比上次增加（去除重複回報）；
內外盤別看 tick_type（1=外盤/買方主動、2=內盤/賣方主動；0=無法判定，不計入比例）。

只追蹤一個合約（預設 TMF，使用者實際交易的）。fed 由 QuoteHub._inject_quote（event loop 執行緒）呼叫，
snapshot() 也只應在 event loop 上呼叫（async 路由），所以不需要鎖。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

FLOW_WINDOWS = (20, 100, 300)       # 最近幾筆「真實成交」
SESSION_START_HHMM = 845            # 日盤 08:45~13:45
SESSION_END_HHMM = 1345
LATE_START_HHMM = 850               # 日盤第一筆資料晚於此時間 → 振幅可能不完整（重啟後重新累計）

# 顯示用的判讀門檻（⚠️ 未經驗證，僅供參考；視覺化需求文件要求先做回放驗證才能當訊號用）
FLOW_UP = 0.60                      # 100 筆外盤占比 >= 此值 → 買方主動
FLOW_DOWN = 0.40                    # <= 此值 → 賣方主動
FLOW_MARGIN = 0.025                  # 遲滯帶：要超過門檻 2.5 個百分點才轉向、回到門檻內 2.5 個百分點才解除（避免在門檻上下閃爍）
BIG_MOVE = 1.5                      # 日盤振幅 / 近 20 日均振幅 >= 此值 → 大波動（與 stats 的 big_move 一致）
QUIET = 0.6                         # <= 此值 → 清淡


class TradeDetector:
    """判斷一個行情事件是不是「真實成交」：volume>0 且 total_volume 比上次增加（去除重複回報）。
    total_volume 變小（換盤/換月）視為新成交；沒有 total_volume 欄位時只看 volume。
    即時面板與 scalp（flow_source=1）共用同一套判定，數字才會一致。"""

    def __init__(self) -> None:
        self._last_total: dict[str, int] = {}

    def is_trade(self, code: str, volume: int, total_volume: int) -> bool:
        if not volume or volume <= 0:                                # 純報價更新，不是成交
            return False
        if total_volume:
            prev = self._last_total.get(code)
            self._last_total[code] = total_volume
            if prev is not None and total_volume == prev:            # 重複回報同一筆成交
                return False
        return True


def flow_direction(share: float | None, prev: int = 0, up: float = FLOW_UP, down: float = FLOW_DOWN,
                   margin: float = FLOW_MARGIN) -> int:
    """外盤占比 → +1 買方主動／-1 賣方主動／0 中性，帶遲滯（Schmitt trigger）。
    占比在門檻上下來回時不會一直翻：進入要超過門檻 margin、離開要回到門檻內 margin。
    實測（update.md／THRESHOLDS.md）：100 筆視窗的硬門檻每小時翻 177 次，遲滯帶可省 60%。沒有資料（None）回 0。"""
    if share is None:
        return 0
    if prev == 1:
        if share >= up - margin:
            return 1
        return -1 if share <= down - margin else 0
    if prev == -1:
        if share <= down + margin:
            return -1
        return 1 if share >= up + margin else 0
    if share >= up + margin:
        return 1
    if share <= down - margin:
        return -1
    return 0


class LiveState:
    def __init__(self, prefix: str = "TMF") -> None:
        self.prefix = prefix
        self._trades: deque[tuple[float, int, int]] = deque(maxlen=max(FLOW_WINDOWS))   # (ts, 方向 1/2, 口數)
        self._detector = TradeDetector()
        self.last_price: float | None = None
        self.last_ts: float = 0.0
        self._day = ""
        self._hi: float | None = None
        self._lo: float | None = None
        self._since = 0.0
        self._since_hhmm = 0
        self._dir: dict[int, int] = {n: 0 for n in FLOW_WINDOWS}      # 各視窗目前的買賣方向（帶遲滯，每筆成交更新）

    # ── 餵資料（event loop 執行緒）────────────────────────────────
    def feed(self, code: str, price: float, volume: int, total_volume: int, tick_type: int, ts: float) -> None:
        if not code.startswith(self.prefix) or not price:
            return
        self.last_price, self.last_ts = price, ts
        lt = time.localtime(ts)
        hhmm = lt.tm_hour * 100 + lt.tm_min
        if SESSION_START_HHMM <= hhmm <= SESSION_END_HHMM:           # 日盤高低（報價更新事件的價格也是最新成交價）
            day = f"{lt.tm_year}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            if day != self._day:
                self._day, self._hi, self._lo = day, price, price
                self._since, self._since_hhmm = ts, hhmm
            else:
                self._hi = price if self._hi is None else max(self._hi, price)
                self._lo = price if self._lo is None else min(self._lo, price)

        if not self._detector.is_trade(code, volume, total_volume):
            return
        if tick_type in (1, 2):
            self._trades.append((ts, tick_type, int(volume)))
            self._update_dirs()

    def _update_dirs(self) -> None:
        """每筆新成交後，依各視窗的外盤占比更新買賣方向。視窗不足 n 筆時用手上全部（與 snapshot 一致）。"""
        total = len(self._trades)
        need = {n: min(n, total) for n in FLOW_WINDOWS}
        marks = set(need.values())
        buys_at: dict[int, int] = {}
        buys = seen = 0
        for _, side, _ in reversed(self._trades):
            seen += 1
            buys += side == 1
            if seen in marks:
                buys_at[seen] = buys
        for n, k in need.items():
            self._dir[n] = flow_direction(buys_at[k] / k, self._dir[n])

    # ── 讀取 ──────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        trades = list(self._trades)
        flow: dict[str, Any] = {}
        for n in FLOW_WINDOWS:
            w = trades[-n:]
            buy_n = sum(1 for _, s, _ in w if s == 1)
            buy_v = sum(v for _, s, v in w if s == 1)
            sell_v = sum(v for _, s, v in w if s == 2)
            flow[str(n)] = {
                "n": len(w), "buy": buy_n, "sell": len(w) - buy_n,
                "share": round(buy_n / len(w), 3) if w else None,                     # 外盤筆數占比
                "vol_share": round(buy_v / (buy_v + sell_v), 3) if buy_v + sell_v else None,   # 外盤口數占比
                "span_sec": round(w[-1][0] - w[0][0]) if len(w) > 1 else 0,           # 這個視窗涵蓋多久
                "dir": self._dir[n],                                                  # +1 買方主動／-1 賣方主動／0 中性（帶遲滯）
            }
        now = time.time()
        lt = time.localtime(now)
        hi, lo = self._hi, self._lo
        return {
            "prefix": self.prefix, "last": self.last_price,
            "last_age": round(now - self.last_ts, 1) if self.last_ts else None,
            "flow": flow,
            "high": hi, "low": lo, "range": round(hi - lo, 1) if hi is not None and lo is not None else None,
            "session_day": self._day,
            "since": time.strftime("%H:%M:%S", time.localtime(self._since)) if self._since else None,
            "partial": bool(self._since) and self._since_hhmm > LATE_START_HHMM,
            "in_session": SESSION_START_HHMM <= lt.tm_hour * 100 + lt.tm_min <= SESSION_END_HHMM,
        }


live_state = LiveState()
