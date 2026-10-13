"""選擇權買方：進場訊號與平虧損腳規則的回測（update.md 發現 30 B；判定標準在跑之前就寫在 update.md，每項只跑一次）。

資料：core/option_history 整理好的近價鏈（月選擇權、一般時段，每天收盤一筆），所以只能回測「波段」（以日為單位的進出）。
每個月選擇權到期月一個週期（互不重疊，每個檢定每個月只有一個樣本）：進場日＝上一個月選擇權到期日之後的第一個交易日（距到期約 28~35 天），買 ATM 跨式。

誠實的限制：
- 每個檢定最多約 190 個週期，統計力有限；每個檢定都附「這份資料看得出來的最小效果（MDE，80% 力道）」。沒過關不等於證明沒有。
- 成交價是收盤那一刻的最後最佳買賣價（可能陳舊或很寬；不合理就退回結算價加減 2%），實際盤中進出的價差可能更好或更差。
- 到期價值用「價內選擇權最後成交價反推的最終結算價」，和真正的最終結算價差幾點，相對跨式幾百點的權利金可以忽略。
- 一天只有一個決策點（收盤）：判斷用當天收盤的結算價、要賣就在下一個交易日收盤，盤中的來回完全沒有模擬。
- 資料缺漏的處理（不改設計，但會計數並印出）：要賣的那天某一腳沒有報價就順延到下一個有報價的交易日、都沒有就持有到期；到期日沒有最終結算價的週期整個丟棄。

用法：
    python -m core.option_backtest sanity              # 只看資料處理有沒有對（週期數、進場距到期、報價可用比例、IV 與 vrp_study 對帳），不看報酬
    python -m core.option_backtest run [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from statistics import NormalDist, mean, median, stdev
from typing import Any, Iterable, Iterator, NamedTuple, Optional, Sequence

from core.iv_monitor import IV_SANE_RANGE, atm_iv_from_quotes, iv_percentile
from core.option_history import DEFAULT_DIR, iter_rows
from core.options_calc import greeks

FEE_PTS = 20.0 / 50.0                  # 手續費每口每邊 20 元 ＝ 0.4 點（TXO 每點 50 元）
TAX = 0.001                            # 期交稅：權利金的 0.1%（到期現金結算也收，以結算價值計）
ASK_OK, BID_OK = (0.9, 1.25), (0.75, 1.1)          # 最後最佳賣／買價相對結算價的合理範圍，超出就當陳舊
HAIRCUT = 0.02                         # 沒有合理報價時：買進＝結算價 × 1.02、賣出＝結算價 × 0.98
TP_MULT = 1.5                          # X2 停利：價值 ≥ 進場賣價總和的 1.5 倍
LEGOUT_RATIO = 0.5                     # X3 平虧損腳：虧損腳價值 ÷ 進場賣價 ≤ 0.5
TIME_STOP_DAYS = 7                     # X1：距到期 ≤ 7 個日曆天的第一個交易日收盤賣出
IV_MIN_DAYS = 7.0                      # IV 的定義（同 vrp_study／iv_monitor）：最近且距到期 ≥ 7 天的月選擇權
PCT_WINDOW = 250
RV_DAYS = 20
SPIKE_DAYS = 5
SPIKE = 0.025                          # S3：前 5 個交易日內單日漲跌幅最大值 ≥ 2.5%
IV_LOW_PCT = 20.0                      # S1：IV 百分位 ≤ 20%
ENTRY_MIN_DTE = 10                     # 主要規則：進場日距到期至少幾個日曆天（正常 27~34，春節順延的月份短到 16；只是防第一天資料缺漏時拖到到期週才進）
STRANGLE_DELTA = 0.25
SENS_HALF_SPREAD = 0.015               # 敏感度分析（描述）：不用收盤報價，買賣一律照結算價 ± 1.5%
BOOT = 5000                            # bootstrap 次數（固定種子）
N_TESTS = 7
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.69：Bonferroni 校正後雙尾 5%
Z_HINT = 2.0
Z_POWER = NormalDist().inv_cdf(0.8)                          # 80% 力道
RULES = ("X0", "X1", "X2", "X3")
RULE_TEXT = {"X0": "持有到期", "X1": "距到期 7 天賣出", "X2": "停利 50%", "X3": "平虧損腳（≤50%）"}

Key = tuple[float, str]                # (履約價, 'C' / 'P')


class Q(NamedTuple):
    """某一天某一檔的收盤資料（點）。"""
    settle: Optional[float]
    close: Optional[float]
    bid: Optional[float]
    ask: Optional[float]


@dataclass
class Exp:
    """某一天、某個月選擇權到期月的資料：到期日、遠期價 F、各 (履約價, 'C'/'P') 的收盤資料。"""
    expiry: str
    expiry_date: date
    F: float
    rows: dict[Key, Q] = field(default_factory=dict)


def iter_days(rows: Iterable[dict[str, Any]], fix: Optional[dict[str, date]] = None,
              changed: Optional[dict[str, tuple[date, date]]] = None) -> Iterator[tuple[date, dict[str, Exp]]]:
    """core.option_history.iter_rows 的列（依日期排序）→ 依序每個交易日 (日期, {到期月: Exp})；只要月選擇權的一般時段。
    fix：{到期月: 真正的到期日}（見 expiry_fixes），蓋掉檔案裡推算的到期日；changed 會被填入 {到期月: (原本, 更正後)}。"""
    cur: Optional[str] = None
    exps: dict[str, Exp] = {}
    for r in rows:
        if r["kind"] != "M" or r["session"] != "G":
            continue
        if r["date"] != cur:
            if cur is not None:
                if r["date"] < cur:
                    raise ValueError(f"資料沒有依日期排序：{r['date']} 出現在 {cur} 之後")
                yield date.fromisoformat(cur), exps
            cur, exps = r["date"], {}
        e = exps.get(r["expiry"])
        if e is None:
            ed = date.fromisoformat(r["expiry_date"])
            if fix and r["expiry"] in fix and fix[r["expiry"]] != ed:
                if changed is not None:
                    changed[r["expiry"]] = (ed, fix[r["expiry"]])
                ed = fix[r["expiry"]]
            e = exps[r["expiry"]] = Exp(r["expiry"], ed, float(r["F"]))
        e.rows[(float(r["strike"]), r["right"])] = Q(r["settle"], r["close"], r["bid"], r["ask"])
    if cur is not None:
        yield date.fromisoformat(cur), exps


# ── 價格的假設 ────────────────────────────────────────────────────

def mark(q: Optional[Q]) -> Optional[float]:
    """目前價值＝結算價；沒有就用收盤價；都沒有回 None。"""
    if q is None:
        return None
    if q.settle is not None and q.settle > 0:
        return q.settle
    if q.close is not None and q.close > 0:
        return q.close
    return None


def ask_reasonable(q: Q) -> bool:
    """最後最佳賣價可用：> 0、在結算價的 0.9~1.25 倍、不低於買價（有買價的話）。"""
    m = mark(q)
    if m is None or q.ask is None or q.ask <= 0:
        return False
    return ASK_OK[0] * m <= q.ask <= ASK_OK[1] * m and (q.bid is None or q.bid <= 0 or q.ask >= q.bid)


def bid_reasonable(q: Q) -> bool:
    """最後最佳買價可用：> 0、在結算價的 0.75~1.1 倍。"""
    m = mark(q)
    return m is not None and q.bid is not None and q.bid > 0 and BID_OK[0] * m <= q.bid <= BID_OK[1] * m


def ask_px(q: Q) -> Optional[float]:
    """買進價：最後最佳賣價（合理才用），否則結算價 × 1.02；連結算價都沒有回 None。"""
    m = mark(q)
    if m is None:
        return None
    return q.ask if ask_reasonable(q) else m * (1 + HAIRCUT)


def bid_px(q: Q) -> Optional[float]:
    """賣出價：最後最佳買價（合理才用），否則結算價 × 0.98；連結算價都沒有回 None。"""
    m = mark(q)
    if m is None:
        return None
    return q.bid if bid_reasonable(q) else m * (1 - HAIRCUT)


def buy_cost(price: float) -> float:
    return price + FEE_PTS + TAX * price


def sell_proceeds(price: float) -> float:
    return price - FEE_PTS - TAX * price


def settle_proceeds(value: float) -> float:
    """到期現金結算：沒有手續費、只收稅（以結算價值計）。"""
    return value - TAX * value


def intrinsic(key: Key, F: float) -> float:
    k, right = key
    return max(F - k, 0.0) if right == "C" else max(k - F, 0.0)


# ── IV、報酬、訊號（只用到某一天為止的資料）───────────────────────

def years_to(day: date, expiry_date: date) -> float:
    """從 day 13:45 到到期日 13:30，單位年（和 vrp_study 同定義）。"""
    return (datetime.combine(expiry_date, time(13, 30)) - datetime.combine(day, time(13, 45))).total_seconds() / (365 * 86400)


def atm_iv(day: date, e: Exp) -> Optional[tuple[float, float]]:
    """某一天某個到期月的 (ATM IV %, 遠期價)：用結算價、買賣權平價取遠期、Black-76、r＝0（和 vrp_study 同定義）。"""
    pairs: dict[float, dict[str, float]] = {}
    for (k, right), q in e.rows.items():
        if q.settle is not None and q.settle > 0:
            pairs.setdefault(k, {})[right] = q.settle
    pairs = {k: v for k, v in pairs.items() if "C" in v and "P" in v}
    T = years_to(day, e.expiry_date)
    if len(pairs) < 3 or T <= 0:
        return None
    k0 = min(pairs, key=lambda k: abs(pairs[k]["C"] - pairs[k]["P"]))
    f0 = k0 + pairs[k0]["C"] - pairs[k0]["P"]
    k_atm = min(pairs, key=lambda k: abs(k - f0))
    res = atm_iv_from_quotes(pairs[k_atm]["C"], pairs[k_atm]["P"], k_atm, T, 0.0)
    if res is None or not (IV_SANE_RANGE[0] <= res[0] <= IV_SANE_RANGE[1]):
        return None
    return res


def daily_iv(day: date, exps: dict[str, Exp]) -> Optional[float]:
    """當天的 IV 序列值：最近且距到期 ≥ 7 天的月選擇權的 ATM IV。"""
    use = [e for e in exps.values() if years_to(day, e.expiry_date) * 365 >= IV_MIN_DAYS]
    if not use:
        return None
    res = atm_iv(day, min(use, key=lambda e: e.expiry_date))
    return res[0] if res else None


def day_return(prev: dict[str, Exp], today: dict[str, Exp], day: date) -> Optional[float]:
    """當天的報酬：同一個到期月的遠期價 F 取對數報酬（避開換月跳空）；兩天都有的到期月裡取最近的。"""
    common = [e for e in prev.values() if e.expiry in today and e.expiry_date >= day]
    if not common:
        return None
    e = min(common, key=lambda x: x.expiry_date)
    f0, f1 = e.F, today[e.expiry].F
    return math.log(f1 / f0) if f0 > 0 and f1 > 0 else None


def entry_signals(iv_hist: Sequence[float], rets: Sequence[float]) -> dict[str, Optional[float]]:
    """進場訊號：只用傳進來的歷史（呼叫端保證只到進場日的前一個交易日）。
    iv_pct：最新一筆 IV 在它之前 250 筆裡的排名（嚴格小於，同 iv_monitor）；ivrv：最新 IV ÷ 近 20 日報酬標準差年化；spike：近 5 日單日報酬絕對值最大。"""
    iv = iv_hist[-1] if iv_hist else None
    pct = iv_percentile(iv, list(reversed(iv_hist[-PCT_WINDOW - 1:-1])), PCT_WINDOW) if len(iv_hist) > PCT_WINDOW else None
    rv = stdev(rets[-RV_DAYS:]) * math.sqrt(252) * 100.0 if len(rets) >= RV_DAYS else None
    return {"iv": iv, "iv_pct": pct, "rv": rv, "ivrv": (iv / rv) if iv is not None and rv else None,
            "spike": max(abs(x) for x in rets[-SPIKE_DAYS:]) if len(rets) >= SPIKE_DAYS else None}


# ── 週期 ──────────────────────────────────────────────────────────

@dataclass
class Leg:
    entry: Q
    daily: list[tuple[date, Q]] = field(default_factory=list)       # 進場後每天（含到期日）的收盤資料


@dataclass
class Cycle:
    rule: str
    expiry: str
    expiry_date: date
    entry_day: date
    F_entry: float
    legs: dict[Key, Leg]
    straddle: tuple[Key, Key]
    strangle: Optional[tuple[Key, Key]] = None
    signals: dict[str, Optional[float]] = field(default_factory=dict)
    final_F: Optional[float] = None
    rescued: bool = False                                     # 到期日與最終結算價是用 TX 最後成交價補的（見 rescue）


def pick_straddle(e: Exp) -> Optional[tuple[Key, Key]]:
    """最靠近遠期價、買賣權都能算出買進價的履約價（平手取較低的）。"""
    ks = {k for k, _ in e.rows if (k, "C") in e.rows and (k, "P") in e.rows
          and ask_px(e.rows[(k, "C")]) is not None and ask_px(e.rows[(k, "P")]) is not None}
    if not ks:
        return None
    k = min(ks, key=lambda x: (abs(x - e.F), x))
    return (k, "C"), (k, "P")


def pick_strangle(day: date, e: Exp, delta: float = STRANGLE_DELTA) -> Optional[tuple[Key, Key]]:
    """兩翼的 |Delta| 最接近 delta 的價外 Call 與 Put（用該到期月的 ATM IV、Black-76、r＝0 算 Delta）。"""
    ai = atm_iv(day, e)
    if ai is None:
        return None
    iv, T = ai[0] / 100.0, years_to(day, e.expiry_date)
    best: dict[str, tuple[float, float]] = {}
    for (k, right), q in e.rows.items():
        if ask_px(q) is None or (right == "C" and k <= e.F) or (right == "P" and k >= e.F):
            continue
        gap = abs(abs(greeks(e.F, k, T, iv, 0.0, right == "C")["delta"]) - delta)
        if right not in best or (gap, k) < best[right]:
            best[right] = (gap, k)
    if "C" not in best or "P" not in best:
        return None
    return (best["C"][1], "C"), (best["P"][1], "P")


class Walker:
    """逐日餵資料、切出週期、算進場訊號。rules：'main'＝上一個到期日之後第一個交易日進場；'dte10'＝距到期 ≤ 10 個日曆天的第一個交易日進場。"""

    def __init__(self, rules: Sequence[str] = ("main", "dte10")) -> None:
        self.rules = tuple(rules)
        self.iv_hist: list[float] = []
        self.iv_by_date: dict[str, float] = {}
        self.rets: list[float] = []
        self.prev: Optional[dict[str, Exp]] = None
        self.started: dict[str, set[str]] = {r: set() for r in self.rules}
        self.known: set[date] = set()                         # 看過的所有到期日
        self.fixed: dict[str, tuple[date, date]] = {}         # 到期日被 TX 更正的契約：{到期月: (檔案裡的, 更正後的)}
        self.open: list[Cycle] = []
        self.done: list[Cycle] = []
        self.dropped: list[Cycle] = []

    def feed(self, day: date, exps: dict[str, Exp]) -> None:
        self.known.update(e.expiry_date for e in exps.values())
        self._advance(day, exps)
        for rule in self.rules:
            self._maybe_enter(rule, day, exps)
        iv = daily_iv(day, exps)                              # 今天的資料明天才會被訊號用到：放在進場判斷之後
        if iv is not None:
            self.iv_hist.append(iv)
            self.iv_by_date[day.isoformat()] = iv
        if self.prev is not None:
            r = day_return(self.prev, exps, day)
            if r is not None:
                self.rets.append(r)
        self.prev = exps

    def _advance(self, day: date, exps: dict[str, Exp]) -> None:
        for cyc in list(self.open):
            e = exps.get(cyc.expiry)
            if day > cyc.expiry_date or (day == cyc.expiry_date and e is None):
                self.open.remove(cyc)                         # 到期日沒有最終結算價（或資料跳過了到期日）：整個週期丟棄
                self.dropped.append(cyc)
                continue
            if e is None:
                continue                                      # 這天沒有這個到期月（資料缺漏）
            for key, leg in cyc.legs.items():
                q = e.rows.get(key)
                if q is not None:
                    leg.daily.append((day, q))
            if day == cyc.expiry_date:
                cyc.final_F = e.F
                self.open.remove(cyc)
                self.done.append(cyc)

    def _maybe_enter(self, rule: str, day: date, exps: dict[str, Exp]) -> None:
        live = [e for e in exps.values() if e.expiry_date >= day]
        if not live:
            return
        e = min(live, key=lambda x: x.expiry_date)
        dte = (e.expiry_date - day).days
        if e.expiry in self.started[rule] or not (dte >= ENTRY_MIN_DTE if rule == "main" else 1 <= dte <= 10):
            return
        if rule == "main":
            before = [x for x in self.known if x < e.expiry_date]
            if not before or max(before) >= day:
                return                                        # 資料起點（沒看過前一個到期日）；或前一個到期日還沒過（含今天就是到期日、但它那天的資料缺了）
        sg = pick_straddle(e)
        if sg is None:
            return
        legs = {k: Leg(e.rows[k]) for k in sg}
        wings = pick_strangle(day, e)
        for k in wings or ():
            legs.setdefault(k, Leg(e.rows[k]))
        self.started[rule].add(e.expiry)
        self.open.append(Cycle(rule, e.expiry, e.expiry_date, day, e.F, legs, sg, wings, entry_signals(self.iv_hist, self.rets)))


def run_walker(directory: Path = DEFAULT_DIR, start: str = "", end: str = "9999-12-31", rules: Sequence[str] = ("main", "dte10"),
               fix: Optional[dict[str, date]] = None) -> Walker:
    w = Walker(rules)
    for day, exps in iter_days(iter_rows(directory, start, end), fix, w.fixed):
        w.feed(day, exps)
    return w


def tx_expiry_data(directory: Path) -> tuple[dict[str, str], dict[tuple[str, str], float]]:
    """期交所 TX 日行情（一般時段、有成交）→ ({到期月: 最後一個有成交的日子}, {(到期月, 日期): 收盤價})。
    TX 月契約和 TXO 月選擇權同一天到期，最後一天的收盤價就是最終結算價附近（和選擇權估計在 189 個週期上差中位數 1 點）。"""
    from core.trend_study import load_tx_contracts
    last: dict[str, str] = {}
    close: dict[tuple[str, str], float] = {}
    for d, month in load_tx_contracts(directory).items():
        for code, (_, c) in month.items():
            close[(code, d)] = c
            if code not in last or d > last[code]:
                last[code] = d
    return last, close


def expiry_fixes(last: dict[str, str]) -> dict[str, date]:
    """月契約真正的到期日＝它在 TX 最後有成交的日子（舊檔沒有契約到期日欄位，到期日是推算的第三個週三，遇假日順延的月份會錯；
    2026-02 的檔案有欄位但 vrp_study 沒解析）。只用已經結束的契約（TX 資料還延續到它之後）；還在交易的契約最後成交日是資料的最後一天，不是到期日。"""
    end = max(last.values(), default="")
    return {code: date.fromisoformat(d) for code, d in last.items() if d < end}


def rescue(w: Walker, last: dict[str, str], close: dict[tuple[str, str], float]) -> int:
    """被丟棄的週期（到期日沒有最終結算價）：用 TX 補。到期日改成該契約在 TX 最後有成交的日子（舊檔沒有契約到期日欄位，
    到期日是推算的第三個週三，遇假日順延的月份會錯），最終結算價用那天的 TX 收盤價。補得起來的搬到 done，回傳補了幾個。"""
    n = 0
    for cyc in list(w.dropped):
        d = last.get(cyc.expiry)
        px = close.get((cyc.expiry, d)) if d else None
        if px is None or date.fromisoformat(d) <= cyc.entry_day:
            continue
        cyc.expiry_date, cyc.final_F, cyc.rescued = date.fromisoformat(d), px, True
        w.dropped.remove(cyc)
        w.done.append(cyc)
        n += 1
    w.done.sort(key=lambda c: (c.expiry_date, c.rule))
    return n


# ── 一個週期在各出場規則下的結果 ───────────────────────────────────

def simulate(c: Cycle, keys: Optional[Sequence[Key]] = None, half_spread: Optional[float] = None) -> Optional[dict[str, Any]]:
    """對一個週期（預設是跨式的兩腳）算 X0~X3 各自的總損益（點）與 ROI；缺進場價、缺最終結算價回 None。
    half_spread：不用報價、買賣一律照結算價 ± 這個比例（敏感度分析用；預設 None＝照登記的規則用收盤的最後最佳買賣價）。"""
    keys = list(c.straddle if keys is None else keys)
    if c.final_F is None or not keys:
        return None

    def ask_of(q: Q) -> Optional[float]:
        m = mark(q)
        return ask_px(q) if half_spread is None else (None if m is None else m * (1 + half_spread))

    def bid_of(q: Q) -> Optional[float]:
        m = mark(q)
        return bid_px(q) if half_spread is None else (None if m is None else m * (1 - half_spread))

    ask: dict[Key, float] = {}
    for k in keys:
        a = ask_of(c.legs[k].entry)
        if a is None:
            return None
        ask[k] = a
    outlay = sum(buy_cost(a) for a in ask.values())
    prem = sum(ask.values())
    quotes = {k: dict(c.legs[k].daily) for k in keys}
    days = sorted({d for q in quotes.values() for d in q if d < c.expiry_date})        # 進場後、到期日以前的交易日
    held = {k: intrinsic(k, c.final_F) for k in keys}
    delayed = 0

    def hold(ks: Sequence[Key]) -> float:
        return sum(settle_proceeds(held[k]) for k in ks)

    def sell(i0: int, ks: Sequence[Key]) -> tuple[float, str]:
        """從第 i0 個交易日起，第一個每一腳都有報價的收盤把 ks 照買價賣出；i0 已是到期日（或之後都沒有報價）就以到期價值結算。"""
        nonlocal delayed
        for i in range(i0, len(days)):
            bids = [bid_of(quotes[k][days[i]]) if days[i] in quotes[k] else None for k in ks]
            if None not in bids:
                delayed += i > i0
                return sum(sell_proceeds(b) for b in bids), days[i].isoformat()       # type: ignore[arg-type]
        delayed += i0 < len(days)
        return hold(ks), "到期"

    res: dict[str, tuple[float, str]] = {"X0": (hold(keys) - outlay, "到期")}
    i1 = next((i for i, d in enumerate(days) if (c.expiry_date - d).days <= TIME_STOP_DAYS), None)       # X1：這一天收盤就賣
    if i1 is None:
        res["X1"] = res["X0"]
    else:
        p, when = sell(i1, keys)
        res["X1"] = (p - outlay, when)
    res["X2"] = res["X0"]                                      # X2：價值 ≥ 進場賣價總和 × 1.5 → 下一個交易日收盤賣出全部
    for i, d in enumerate(days):
        ms = [mark(quotes[k].get(d)) for k in keys]
        if None not in ms and sum(ms) >= TP_MULT * prem:                                # type: ignore[arg-type]
            p, when = sell(i + 1, keys)
            res["X2"] = (p - outlay, when)
            break
    res["X3"] = res["X0"]                                      # X3：價值 ÷ 進場賣價最低的那一腳 ≤ 0.5 → 下一個交易日收盤賣掉那一腳，另一腳持有到期
    if len(keys) == 2:
        for i, d in enumerate(days):
            ms = {k: mark(quotes[k].get(d)) for k in keys}
            if None in ms.values():
                continue
            loser = min(keys, key=lambda k: ms[k] / ask[k])                              # type: ignore[operator]
            if ms[loser] / ask[loser] <= LEGOUT_RATIO:                                   # type: ignore[operator]
                p, when = sell(i + 1, [loser])
                res["X3"] = (p + hold([k for k in keys if k != loser]) - outlay, when)
                break
    out: dict[str, Any] = {"outlay": outlay, "premium": prem, "delayed": delayed,
                           "free": sum(held.values()) - sum(mark(c.legs[k].entry) or 0.0 for k in keys)}      # 無摩擦：進場照結算價、到期不收稅
    for r in RULES:
        pnl, when = res[r]
        out[r] = {"pnl": pnl, "roi": pnl / outlay, "exit": when}
    return out


# ── 統計 ──────────────────────────────────────────────────────────

def one_sample_t(x: Sequence[float]) -> dict[str, Any]:
    n = len(x)
    if n < 3:
        return {"n": n, "mean": None, "se": None, "t": float("nan"), "mde": None}
    se = stdev(x) / math.sqrt(n)
    return {"n": n, "mean": mean(x), "se": se, "t": mean(x) / se if se > 0 else float("nan"), "mde": (Z_CLAIM + Z_POWER) * se}


def welch(a: Sequence[float], b: Sequence[float]) -> dict[str, Any]:
    """a − b 的 Welch t（大樣本用常態門檻，同專案其他檢定）。"""
    out: dict[str, Any] = {"n_a": len(a), "n_b": len(b), "mean_a": None, "mean_b": None, "diff": None, "se": None, "t": float("nan"), "mde": None}
    if len(a) < 3 or len(b) < 3:
        return out
    se = math.sqrt(stdev(a) ** 2 / len(a) + stdev(b) ** 2 / len(b))
    d = mean(a) - mean(b)
    out.update({"mean_a": mean(a), "mean_b": mean(b), "diff": d, "se": se, "t": d / se if se > 0 else float("nan"), "mde": (Z_CLAIM + Z_POWER) * se})
    return out


def verdict(t: float) -> str:
    if math.isnan(t):
        return "無法檢定"
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


def describe(x: Sequence[float], seed: int = 7, boot: Optional[int] = None) -> dict[str, Any]:
    """平均／中位數／勝率／分位／bootstrap 95% 區間（固定種子）。"""
    boot = BOOT if boot is None else boot
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "sd": None, "win": None, "p05": None, "p95": None, "boot_lo": None, "boot_hi": None}
    xs = sorted(x)
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(xs, k=n)) for _ in range(boot))
    return {"n": n, "mean": mean(xs), "median": median(xs), "sd": stdev(xs) if n > 1 else None, "win": sum(1 for v in xs if v > 0) / n,
            "p05": xs[min(int(0.05 * n), n - 1)], "p95": xs[min(int(0.95 * n), n - 1)],
            "boot_lo": means[int(0.025 * boot)], "boot_hi": means[int(0.975 * boot) - 1]}


def split_terciles(items: Sequence[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """[(排序用的值, ROI)] → (最低三分之一的 ROI, 最高三分之一的 ROI)；各取 n//3 個。"""
    s = sorted(items, key=lambda p: p[0])
    m = len(s) // 3
    if m == 0:
        return [], []
    return [v for _, v in s[:m]], [v for _, v in s[len(s) - m:]]


def run_tests(cycles: Sequence[Cycle]) -> dict[str, Any]:
    """事先登記的 7 個檢定與描述。cycles 是主要規則（上一個到期日之後第一個交易日進場）的週期。"""
    sims = [(c, s) for c in cycles if (s := simulate(c)) is not None]
    roi = {r: [s[r]["roi"] for _, s in sims] for r in RULES}
    t: dict[str, Any] = {"T1": one_sample_t(roi["X0"])}
    for name, r in (("T2", "X1"), ("T3", "X2"), ("T4", "X3")):
        t[name] = one_sample_t([b - a for a, b in zip(roi["X0"], roi[r])])
    x0 = [(c, s["X0"]["roi"]) for c, s in sims]
    t["T5"] = welch([v for c, v in x0 if c.signals.get("iv_pct") is not None and c.signals["iv_pct"] <= IV_LOW_PCT],
                    [v for c, v in x0 if c.signals.get("iv_pct") is not None and c.signals["iv_pct"] > IV_LOW_PCT])
    low, high = split_terciles([(c.signals["ivrv"], v) for c, v in x0 if c.signals.get("ivrv") is not None])
    t["T6"] = welch(low, high)
    t["T7"] = welch([v for c, v in x0 if c.signals.get("spike") is not None and c.signals["spike"] >= SPIKE],
                    [v for c, v in x0 if c.signals.get("spike") is not None and c.signals["spike"] < SPIKE])
    for v in t.values():
        v["verdict"] = verdict(v["t"])
    by_year: dict[str, list[float]] = {}
    for c, s in sims:
        by_year.setdefault(str(c.entry_day.year), []).append(s["X0"]["roi"])
    since16 = [s for c, s in sims if c.entry_day.year >= 2016]
    wing = [s for s in (simulate(c, c.strangle) for c in cycles if c.strangle) if s is not None]
    dte = sorted((c.expiry_date - c.entry_day).days for c, _ in sims)
    desc = {"cycles": len(sims), "first": min(c.entry_day for c, _ in sims).isoformat() if sims else None,
            "last": max(c.entry_day for c, _ in sims).isoformat() if sims else None,
            "entry_dte": {"min": dte[0], "median": dte[len(dte) // 2], "max": dte[-1]} if dte else None,
            "roi": {r: describe(roi[r]) for r in RULES}, "pts": {r: describe([s[r]["pnl"] for _, s in sims]) for r in RULES},
            "premium_pct_of_F": describe([s["premium"] / c.F_entry for c, s in sims]),
            "by_year_x0": {y: {"n": len(v), "mean": mean(v)} for y, v in sorted(by_year.items())},
            "since2016": {"n": len(since16), "x0_pts_mean": mean(s["X0"]["pnl"] for s in since16) if since16 else None,
                          "x0_win": sum(1 for s in since16 if s["X0"]["pnl"] > 0) / len(since16) if since16 else None,
                          "free_pts_mean": mean(s["free"] for s in since16) if since16 else None,
                          "free_win": sum(1 for s in since16 if s["free"] > 0) / len(since16) if since16 else None},
            "strangle_x0": describe([s["X0"]["roi"] for s in wing]),
            "early_exits": {r: sum(1 for _, s in sims if s[r]["exit"] != "到期") for r in ("X1", "X2", "X3")},
            "delayed_exits": sum(1 for _, s in sims if s["delayed"]), "rescued": sum(1 for c, _ in sims if c.rescued),
            "sens": sensitivity(cycles)}
    return {"tests": t, "describe": desc}


def sensitivity(cycles: Sequence[Cycle], half_spread: float = SENS_HALF_SPREAD) -> dict[str, Any]:
    """敏感度分析（描述、不檢定）：近年收盤的最後最佳買賣價很寬、多半是收盤報價失真，而出場用買價、到期結算沒有價差成本，
    會讓提早出場在近年看起來不利。這裡把成交價改成結算價 ± half_spread，看 T1~T4 的方向會不會變。"""
    sims = [s for s in (simulate(c, half_spread=half_spread) for c in cycles) if s is not None]
    roi = {r: [s[r]["roi"] for s in sims] for r in RULES}
    out: dict[str, Any] = {"half_spread": half_spread, "n": len(sims), "T1": one_sample_t(roi["X0"])}
    for name, r in (("T2", "X1"), ("T3", "X2"), ("T4", "X3")):
        out[name] = one_sample_t([b - a for a, b in zip(roi["X0"], roi[r])])
    return out


def describe_rule(cycles: Sequence[Cycle]) -> dict[str, Any]:
    """次要規則（描述）：只看持有到期。"""
    sims = [s for s in (simulate(c) for c in cycles) if s is not None]
    return {"n": len(sims), "roi": describe([s["X0"]["roi"] for s in sims]), "pts": describe([s["X0"]["pnl"] for s in sims])}


def _pc(x: Optional[float], d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def format_report(res: dict[str, Any], secondary: Optional[dict[str, Any]] = None, dropped: int = 0) -> str:
    d, t = res["describe"], res["tests"]
    if not d["cycles"]:
        return f"沒有可用的週期（資料不足或全部被丟棄；丟棄 {dropped} 個）"
    lines = [f"主要規則：月選擇權到期後第一個交易日進場買 ATM 跨式；週期 {d['cycles']} 個（{d['first']} ~ {d['last']}；"
             f"其中 {d['rescued']} 個的到期日與最終結算價用 TX 最後成交價補，丟棄 {dropped} 個）",
             f"進場距到期（日曆天）最小／中位數／最大 {d['entry_dte']['min']}／{d['entry_dte']['median']}／{d['entry_dte']['max']}；跨式權利金平均佔標的 {d['premium_pct_of_F']['mean'] * 100:.2f}%", "",
             "出場規則             平均ROI  中位數  勝率  5%分位  95%分位  bootstrap 95% 區間      平均損益(點)"]
    for r in RULES:
        x, p = d["roi"][r], d["pts"][r]
        lines.append(f"{r} {RULE_TEXT[r]:<12}{_pc(x['mean']):>9}{_pc(x['median']):>8}{x['win'] * 100:>5.0f}%{_pc(x['p05']):>8}{_pc(x['p95']):>9}   "
                     f"[{_pc(x['boot_lo'])}, {_pc(x['boot_hi'])}]{p['mean']:>10.1f}")
    lines += ["", f"7 個事先登記的檢定（雙尾；|t| ≥ {Z_CLAIM:.2f} 過關、≥ {Z_HINT:.1f} 線索；MDE＝80% 力道看得出來的最小效果，單位是 ROI）"]
    names = {"T1": "X0 平均 ROI ≠ 0", "T2": "X1(距到期7天賣) − X0", "T3": "X2(停利50%) − X0", "T4": "X3(平虧損腳) − X0",
             "T5": "S1 IV百分位≤20% vs 其餘", "T6": "S2 IV/RV 最低 vs 最高三分位", "T7": "S3 前5日大波動 vs 沒有"}
    for k in ("T1", "T2", "T3", "T4"):
        v = t[k]
        lines.append(f"  {k} {names[k]:<24} n={v['n']:>3}  平均 {_pc(v['mean']):>8}  t={v['t']:+.2f}  MDE ±{(v['mde'] or 0) * 100:.0f}%  → {v['verdict']}")
    for k in ("T5", "T6", "T7"):
        v = t[k]
        lines.append(f"  {k} {names[k]:<24} n={v['n_a']}/{v['n_b']}  差 {_pc(v['diff']):>8}（{_pc(v['mean_a'])} vs {_pc(v['mean_b'])}）  t={v['t']:+.2f}  MDE ±{(v['mde'] or 0) * 100:.0f}%  → {v['verdict']}")
    lines += ["", "各年 X0 平均 ROI：" + "  ".join(f"{y} {_pc(v['mean'], 0)}(n={v['n']})" for y, v in d["by_year_x0"].items())]
    s = d["since2016"]
    if s["n"]:
        lines.append(f"對照先前示意（2016 起 {s['n']} 個週期；先前是 −13 點／勝率 43%、不含成本）：含成本 X0 平均 {s['x0_pts_mean']:+.1f} 點、勝率 {s['x0_win'] * 100:.0f}%；"
                     f"無摩擦（結算價進場）平均 {s['free_pts_mean']:+.1f} 點、勝率 {s['free_win'] * 100:.0f}%")
    w = d["strangle_x0"]
    if w["n"]:
        lines.append(f"勒式（兩翼 |Delta| 0.25）持有到期（描述）：n={w['n']} 平均 {_pc(w['mean'])} 中位數 {_pc(w['median'])} 勝率 {w['win'] * 100:.0f}%")
    sv = d["sens"]
    if sv["n"] >= 3 and sv["T1"]["mean"] is not None:
        lines.append(f"敏感度（描述，不檢定；成交價改為結算價 ±{sv['half_spread'] * 100:.1f}%，不用收盤報價）：X0 平均 ROI {_pc(sv['T1']['mean'])}（t={sv['T1']['t']:+.2f}）；"
                     + "；".join(f"{k} {_pc(sv[k]['mean'])}（t={sv[k]['t']:+.2f}）" for k in ("T2", "T3", "T4")))
    e = d["early_exits"]
    lines.append(f"X1／X2／X3 實際提早出場的週期數：{e['X1']}／{e['X2']}／{e['X3']}（其餘持有到期）；賣出順延（某一腳那天沒報價）的週期：{d['delayed_exits']}")
    if secondary and secondary["n"]:
        r = secondary["roi"]
        lines.append(f"次要（描述）：距到期 ≤10 個日曆天的第一個交易日進場、持有到期：n={secondary['n']} 平均 ROI {_pc(r['mean'])} 中位數 {_pc(r['median'])} 勝率 {r['win'] * 100:.0f}% "
                     f"平均損益 {secondary['pts']['mean']:+.1f} 點")
    return "\n".join(lines)


def _clean(o: Any) -> Any:
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


# ── 資料處理的核對（不看報酬）─────────────────────────────────────

def sanity(w: Walker, vrp_dir: Optional[Path] = None, tx: Optional[tuple[dict[str, str], dict[tuple[str, str], float]]] = None) -> str:
    """週期數、進場距到期、進場報價可用比例與價差、IV 與 vrp_study 的對帳、最終結算價與 TX 的對帳。不算任何報酬。"""
    lines = []
    for rule in w.rules:
        cs = [c for c in w.done if c.rule == rule]
        if not cs:
            lines.append(f"[{rule}] 沒有週期")
            continue
        dte = sorted((c.expiry_date - c.entry_day).days for c in cs)
        ok_ask = sum(ask_reasonable(c.legs[k].entry) for c in cs for k in c.straddle)
        n_leg = 2 * len(cs)
        lines.append(f"[{rule}] 完成 {len(cs)} 個、丟棄 {sum(1 for c in w.dropped if c.rule == rule)} 個、尚未到期 {sum(1 for c in w.open if c.rule == rule)} 個；"
                     f"進場距到期 {dte[0]}／{dte[len(dte) // 2]}／{dte[-1]} 天（最小／中位數／最大）；"
                     f"進場賣價可用 {ok_ask}/{n_leg}（{ok_ask / n_leg * 100:.0f}%）；有勒式資料 {sum(1 for c in cs if c.strangle)} 個")
    main_cs = [c for c in w.done if c.rule == "main"]
    spreads: dict[int, list[float]] = {}
    for c in main_cs:
        for k in c.straddle:
            q = c.legs[k].entry
            if q.bid and q.ask and q.bid > 0 and q.ask >= q.bid:
                spreads.setdefault(c.entry_day.year, []).append((q.ask - q.bid) / ((q.ask + q.bid) / 2))
    if spreads:
        lines.append("進場腿收盤最後最佳買賣價的價差（占中價，各年中位數）：" + "  ".join(f"{y} {median(v) * 100:.1f}%" for y, v in sorted(spreads.items())))
    if tx is not None:
        last, close = tx
        own = [c for c in main_cs if not c.rescued]
        diffs = sorted(abs(c.final_F - close[(c.expiry, c.expiry_date.isoformat())]) for c in own if (c.expiry, c.expiry_date.isoformat()) in close)
        if diffs:
            lines.append(f"最終結算價（選擇權最後成交價反推）對 TX 到期日收盤：{len(diffs)} 個；差的中位數 {median(diffs):.1f}、p90 {diffs[int(0.9 * len(diffs))]:.1f}、最大 {diffs[-1]:.1f} 點")
        if w.fixed:
            lines.append(f"到期日用 TX 最後成交日更正的契約 {len(w.fixed)} 個：" + "、".join(f"{k} {a}→{b}" for k, (a, b) in sorted(w.fixed.items())))
        lines.append(f"到期日沒有最終結算價、TX 補得起來的：{sum(1 for c in w.dropped if c.rule == 'main' and (c.expiry, last.get(c.expiry, '')) in close)} 個"
                     f"（已補 {sum(1 for c in main_cs if c.rescued)} 個）")
    if vrp_dir is not None and any(vrp_dir.glob("iv_*.csv")):
        from core.vrp_study import load_iv
        ref = load_iv(vrp_dir)
        both = sorted(set(ref) & set(w.iv_by_date))
        diffs = [abs(ref[d]["iv"] - w.iv_by_date[d]) for d in both]
        lines.append(f"IV 對帳（與 vrp_study）：共同 {len(both)} 天；只在 vrp {len(set(ref) - set(w.iv_by_date))} 天、只在這裡 {len(set(w.iv_by_date) - set(ref))} 天；"
                     f"差的最大 {max(diffs) if diffs else float('nan'):.3f}、平均 {mean(diffs) if diffs else float('nan'):.4f}（IV 百分點）")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="python -m core.option_backtest", description="選擇權買方回測（進場訊號、平虧損腳）")
    ap.add_argument("what", choices=("sanity", "run"))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    a = ap.parse_args(argv)
    tx = tx_expiry_data(Path(a.dir) / "tx")
    w = run_walker(Path(a.dir), fix=expiry_fixes(tx[0]))
    if a.what == "sanity":
        print(sanity(w, Path(a.dir) / "vrp", tx))
        rescue(w, *tx)
        print(f"補完後：主要規則完成 {sum(1 for c in w.done if c.rule == 'main')} 個、仍丟棄 {sum(1 for c in w.dropped if c.rule == 'main')} 個")
        return 0
    rescue(w, *tx)
    res = run_tests([c for c in w.done if c.rule == "main"])
    sec = describe_rule([c for c in w.done if c.rule == "dte10"])
    dropped = sum(1 for c in w.dropped if c.rule == "main")
    print(format_report(res, sec, dropped))
    if a.json:
        Path(a.json).write_text(json.dumps(_clean({"main": res, "secondary": sec, "dropped": dropped}), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
