"""盤中即時狀態（純記憶體、僅顯示、不影響下單）：外/內盤比例、日盤振幅，以及報價事件免費帶來的五檔價差／深度、
交易所均價（VWAP）、基差、交易所累計內外盤、開盤後 30／60 分的早盤與之後振幅（給 intraday_context 用）。

外/內盤比例只算「真實成交」：實測 TMF 的行情事件只有約 27% 是真的成交（volume>0），
其餘是帶著上一筆成交 tick_type 的報價更新；全部計入的話，比例會被「重複計算的上一筆方向」主導
（scalp 目前就是這樣統計，見 update.md「發現」）。
判定「真實成交」：volume>0 且 total_volume 比上次增加（去除重複回報）；
內外盤別看 tick_type（1=外盤/買方主動、2=內盤/賣方主動；0=無法判定，不計入比例）。

只追蹤一個合約（預設 TMF，使用者實際交易的）。fed 由 QuoteHub._inject_quote（event loop 執行緒）呼叫，
snapshot() 也只應在 event loop 上呼叫（async 路由），所以不需要鎖。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

FLOW_WINDOWS = (20, 100, 300)       # 最近幾筆「真實成交」
SESSION_START_HHMM = 845            # 日盤 08:45~13:45
SESSION_END_HHMM = 1345
LATE_START_HHMM = 850               # 日盤第一筆資料晚於此時間 → 振幅可能不完整（重啟後重新累計）

# 顯示用的判讀門檻（⚠️ 未經驗證，僅供參考；視覺化需求文件要求先做回放驗證才能當訊號用）
FLOW_UP = 0.60                      # 100 筆外盤占比 >= 此值 → 買方主動
FLOW_DOWN = 0.40                    # <= 此值 → 賣方主動
FLOW_MARGIN = 0.025                  # 遲滯帶：要超過門檻 2.5 個百分點才轉向、回到門檻內 2.5 個百分點才解除（避免在門檻上下閃爍）
BOOK_WINDOW_SEC = 30                # 價差／五檔失衡取最近幾秒的平均（單筆五檔每 0.1 秒就變，只看瞬間值會一直跳）
EX_MISMATCH_LOG_MAX = 5             # 自己累計的日盤高低和交易所給的對不上時，每天最多記幾次 warning（核對用）
SESSION_OPEN_MIN = 8 * 60 + 45      # 日盤第 0 分鐘（08:45）；早盤／之後振幅以此起算
EARLY_CHECKPOINTS = (30, 60)        # 開盤後幾分鐘為「早盤」（update.md 發現 28：早盤振幅有助預測之後的振幅）
BASIS_START_HHMM, BASIS_END_HHMM = 900, 1330     # 現貨指數只在 09:00~13:30 更新；其餘時間（含夜盤）的基差是舊的，不顯示
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


def book_stats(book: dict | None) -> dict[str, Any] | None:
    """五檔 → 最佳買賣價與量、五檔總量、價差、失衡（(買量−賣量)/(買量+賣量)，正＝買方掛得比較厚）。
    資料壞掉（缺欄、型別錯）回 None；價差不是正數（單邊沒掛、鎖單、交叉）時價差為 None，不顯示負的價差。"""
    if not book:
        return None
    try:
        bid1, ask1 = float(book["bp"][0]), float(book["ap"][0])
        bv, av = [int(x) for x in book["bv"]], [int(x) for x in book["av"]]
        bv1, av1 = bv[0], av[0]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    bv5, av5 = sum(bv), sum(av)
    both = bid1 > 0 and ask1 > bid1
    return {"bid1": bid1, "ask1": ask1, "bv1": bv1, "av1": av1, "bv5": bv5, "av5": av5,
            "spread": round(ask1 - bid1, 2) if both else None,
            "imb5": round((bv5 - av5) / (bv5 + av5), 3) if bv5 + av5 > 0 else None}


def session_of(hhmm: int) -> str:
    """依時鐘判斷目前在哪個交易時段：day 日盤 08:45~13:45／night 盤後 15:00~05:00／other。"""
    if SESSION_START_HHMM <= hhmm <= SESSION_END_HHMM:
        return "day"
    if hhmm >= 1500 or hhmm < 500:
        return "night"
    return "other"


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
        # 報價事件附帶的欄位（五檔、交易所均價與累計內外盤、現貨指數、交易所的日盤高低）
        self._book: dict[str, Any] | None = None
        self._book_ts = 0.0
        self._book_hist: deque[tuple[float, float | None, float | None]] = deque()   # (ts, 價差, 失衡)，只留最近 BOOK_WINDOW_SEC 秒
        self._ex: dict[str, Any] = {}
        self._ex_hi: float | None = None
        self._ex_lo: float | None = None
        self._ex_mismatch = 0
        self._ex_mismatch_logged = 0
        self._cp: dict[int, dict[str, list[float | None]]] = {}       # 開盤後 c 分鐘的早盤（early）與之後（post）的 [高, 低]

    # ── 餵資料（event loop 執行緒）────────────────────────────────
    def feed(self, code: str, price: float, volume: int, total_volume: int, tick_type: int, ts: float,
             quote: dict | None = None) -> None:
        """quote＝QuoteHub 收到的整包行情 dict（可省略）：有的話順便吃五檔、交易所均價／累計內外盤／現貨指數／日盤高低。"""
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
                self._ex_hi = self._ex_lo = None
                self._ex_mismatch = self._ex_mismatch_logged = 0
                self._cp = {}
            else:
                self._hi = price if self._hi is None else max(self._hi, price)
                self._lo = price if self._lo is None else min(self._lo, price)
            self._track_checkpoints(price, lt.tm_hour * 60 + lt.tm_min - SESSION_OPEN_MIN)
        if quote:
            try:                                                      # 附加欄位出事不能影響下面的買賣力道統計
                self._feed_extra(code, quote, ts, hhmm)
            except Exception:
                logger.debug("live_state 附加欄位處理失敗（已忽略）", exc_info=True)

        if not self._detector.is_trade(code, volume, total_volume):
            return
        if tick_type in (1, 2):
            self._trades.append((ts, tick_type, int(volume)))
            self._update_dirs()

    def _track_checkpoints(self, price: float, minute: int) -> None:
        """開盤後第 minute 分鐘（08:45 起算）的價格，記進各檢查點的「早盤」（< c 分）或「之後」（>= c 分）的高低。"""
        for c in EARLY_CHECKPOINTS:
            hl = self._cp.setdefault(c, {"early": [None, None], "post": [None, None]})["early" if minute < c else "post"]
            hl[0] = price if hl[0] is None else max(hl[0], price)
            hl[1] = price if hl[1] is None else min(hl[1], price)

    def _feed_extra(self, code: str, quote: dict, ts: float, hhmm: int) -> None:
        b = book_stats(quote.get("book"))
        if b:
            self._book, self._book_ts = b, ts
            self._book_hist.append((ts, b["spread"], b["imb5"]))
        while self._book_hist and self._book_hist[0][0] < ts - BOOK_WINDOW_SEC:
            self._book_hist.popleft()
        for k in ("avg_price", "underlying_price"):                  # 0＝交易所還沒算出來
            if quote.get(k):
                self._ex[k] = float(quote[k])
        for k in ("bid_side_total_vol", "ask_side_total_vol"):       # 時段累計內盤／外盤口數（0 是合法值）
            if quote.get(k) is not None:
                self._ex[k] = int(quote[k])
        self._ex["code"], self._ex["ts"], self._ex["hhmm"] = code, ts, hhmm
        if session_of(hhmm) == "day":                                # 交易所的日盤高低（夜盤事件的高低是夜盤的，不能混進來）
            eh, el = quote.get("high") or 0, quote.get("low") or 0
            if eh > 0 and el > 0 and eh >= el:
                self._ex_hi, self._ex_lo = float(eh), float(el)
                self._check_exchange_range(code, ts)

    def _check_exchange_range(self, code: str, ts: float) -> None:
        """自己從成交價累計的日盤高低，應該和交易所給的一樣（同一批成交）；不一樣就記下來——
        可能是交易所的高低混進了試算價、或我們漏收了行情。只有「從開盤就在跑」才比（重啟後自己累計的本來就不完整）。"""
        if self._hi is None or self._ex_hi is None or self._since_hhmm > LATE_START_HHMM:
            return
        if abs(self._ex_hi - self._hi) > 0.5 or abs(self._ex_lo - self._lo) > 0.5:
            self._ex_mismatch += 1
            if self._ex_mismatch_logged < EX_MISMATCH_LOG_MAX:
                self._ex_mismatch_logged += 1
                logger.warning("日盤高低與交易所不一致（%s %s）：自己 %s/%s、交易所 %s/%s（今天第 %d 次）",
                               code, time.strftime("%H:%M:%S", time.localtime(ts)), self._hi, self._lo,
                               self._ex_hi, self._ex_lo, self._ex_mismatch)

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
        hhmm_now = lt.tm_hour * 100 + lt.tm_min
        hi, lo = self._hi, self._lo
        own_partial = bool(self._since) and self._since_hhmm > LATE_START_HHMM
        range_source = "own"
        if own_partial and hi is not None and self._ex_hi is not None and self._ex_hi >= hi and self._ex_lo <= lo:
            hi, lo, range_source = self._ex_hi, self._ex_lo, "exchange"     # 重啟後自己累計的不完整 → 改用交易所給的整個日盤高低
        return {
            "prefix": self.prefix, "last": self.last_price,
            "last_age": round(now - self.last_ts, 1) if self.last_ts else None,
            "flow": flow,
            "high": hi, "low": lo, "range": round(hi - lo, 1) if hi is not None and lo is not None else None,
            "range_source": range_source,                                  # own＝自己從成交價累計／exchange＝交易所的值（重啟後補完整）
            "session_day": self._day,
            "since": time.strftime("%H:%M:%S", time.localtime(self._since)) if self._since else None,
            "partial": own_partial and range_source == "own",
            "in_session": SESSION_START_HHMM <= hhmm_now <= SESSION_END_HHMM,
            "session": session_of(hhmm_now),
            "book": self._book_view(now),
            "session_flow": self._session_flow(),
            "vwap": self._vwap(),
            "basis": self._basis(),
            "ex_range": ({"high": self._ex_hi, "low": self._ex_lo, "mismatch": self._ex_mismatch}
                         if self._ex_hi is not None else None),
            "cp": self._checkpoints(own_partial),
        }

    # ── snapshot 的附加區塊 ───────────────────────────────────────
    def _book_view(self, now: float) -> dict[str, Any] | None:
        b = self._book
        if not b:
            return None
        recent = [(sp, im) for t, sp, im in self._book_hist if t >= now - BOOK_WINDOW_SEC]
        sps = [x for x, _ in recent if x is not None]
        ims = [x for _, x in recent if x is not None]
        return {**b, "age": round(now - self._book_ts, 1), "n": len(recent), "window_sec": BOOK_WINDOW_SEC,
                "spread_avg": round(sum(sps) / len(sps), 2) if sps else None,
                "imb5_avg": round(sum(ims) / len(ims), 3) if ims else None}

    def _session_flow(self) -> dict[str, Any] | None:
        """交易所的時段累計內盤／外盤（重啟後第一筆就有整個時段的量，不像上面的逐筆視窗要重新累計）。"""
        ex = self._ex
        if "bid_side_total_vol" not in ex or "ask_side_total_vol" not in ex:
            return None
        inside, outside = ex["bid_side_total_vol"], ex["ask_side_total_vol"]
        return {"inside": inside, "outside": outside, "session": session_of(ex["hhmm"]),
                "share": round(outside / (inside + outside), 3) if inside + outside > 0 else None}

    def _vwap(self) -> dict[str, Any] | None:
        ex = self._ex
        if "avg_price" not in ex or self.last_price is None:
            return None
        return {"price": ex["avg_price"], "diff": round(self.last_price - ex["avg_price"], 1), "session": session_of(ex["hhmm"])}

    def _basis(self) -> dict[str, Any] | None:
        """基差＝期貨最新價 − 現貨指數。現貨只在 09:00~13:30 更新，其餘時間（含夜盤）指數是舊的，valid=False 前端不顯示數字。"""
        ex = self._ex
        if "underlying_price" not in ex or self.last_price is None:
            return None
        return {"spot": ex["underlying_price"], "value": round(self.last_price - ex["underlying_price"], 1),
                "valid": BASIS_START_HHMM <= ex["hhmm"] < BASIS_END_HHMM}

    def _checkpoints(self, own_partial: bool) -> dict[str, Any] | None:
        """各檢查點的早盤振幅與之後已走的振幅。不是從開盤就在跑（重啟）→ None（早盤那段不完整，不能拿來預測）。"""
        if own_partial or not self._cp:
            return None
        rng = lambda hl: round(hl[0] - hl[1], 1) if hl[0] is not None else None     # noqa: E731
        return {str(c): {"early": rng(v["early"]), "post": rng(v["post"])} for c, v in self._cp.items()}


live_state = LiveState()
