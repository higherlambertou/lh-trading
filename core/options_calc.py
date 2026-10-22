"""選擇權買方試算（買跨式／買勒式／買單向 Call 或 Put）與平倉試算：純函式，不碰券商、不下單。

用途（需求文件《選擇權波段策略》）：
- 情境一（波段跨式）、情境二（夜盤單向）：先把「成本、損益兩平、最大虧損、Greeks、價格×時間／IV 的損益表」算清楚；
- 跨式買進之後「平掉虧損那一腳」這個最關鍵的判斷點：算出已實現虧損、剩下那腳要漲到多少才回本、還要再走多少。
只做買方（最大虧損＝權利金）。履約價用 Delta 或「預期波動點數」選。

定價與 iv_monitor 同一套：Black-76（歐式選擇權、以遠期價定價），T＝日曆天／365，無風險利率預設 1.5%（IV_RATE）。
遠期價 F 與 ATM IV 由鏈上買賣平價反推（和 iv_monitor.atm_iv_from_quotes 同一條公式），不需要期貨報價。
單位：價格與損益都是「點」（臺指選擇權每點 50 元，MULT）；delta＝每點、vega＝每 1 個波動點（1%）、theta＝每日曆天。

誠實的限制（給使用者看的，不是程式註解）：
- 全部是模型與當下報價的推算，不是預測；情境表用各履約價自己的 IV（有偏斜），IV 隨價格怎麼變是假設（維持不變＋指定的平移）。
- 歷史機率只回答「過去這麼久的時間，漲跌超過損益兩平的比例」，窗口重疊、波動度會換 regime，只是參考。
- 到期損益以「理論結算＝到期日標的價」計，不含手續費與交易稅。
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from core.iv_monitor import black76, implied_vol

MULT = 50                       # 臺指選擇權每點 50 元
RATE = 0.015                    # 無風險利率（同 iv_monitor 的 IV_RATE 預設）
YEAR_DAYS = 365.0               # T＝日曆天／365（同 iv_monitor）
MAX_SPREAD_RATIO = 0.3          # 買賣價差超過中價的 30%：中價不可信（同 iv_monitor），改用最近成交價
WARN_SPREAD = 0.10              # 價差佔中價超過 10% 就提醒（進出一趟的成本）
WARN_DAYS = 3.0                 # 距到期不到這麼多天：Gamma 大、時間價值流失快
IV_SANE = (0.03, 1.5)           # 反推出來的 IV 超出這個範圍當作沒有（同 iv_monitor 的 3%~150%）
SCENARIO_MULTS = (-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0)    # 價格情境：參考波動的倍數
IV_SHOCKS = (-5.0, -2.5, 0.0, 2.5, 5.0)                               # IV 情境：平移幾個波動點
TMF_MULT = 10                   # 微台每點 10 元
HEDGE_COST_PTS = 3.0            # 每口每次對沖的成本（指數點，假設）：買賣價差約 1 點 + 手續費與期交稅約 2 點
KINDS = ("straddle", "strangle", "call", "put")
ACTION_HOLD, ACTION_ALL = "hold", "all"


# ── 基本數學 ──────────────────────────────────────────────────────

def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def greeks(F: float, K: float, T: float, sigma: float, r: float, is_call: bool) -> dict[str, float]:
    """Black-76 的 delta／gamma／vega／theta。
    delta＝F 每動 1 點（含折現）；gamma＝delta 每點的變化；vega＝IV 每變 1.00（呼叫端除以 100 得每 1 個波動點）；
    theta＝每日曆年的價值變化（負＝時間價值流失；呼叫端除以 365 得每天）。到期或波動率為 0 時只剩 delta（價內 1、價外 0）。"""
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        itm = F > K if is_call else F < K
        return {"delta": (df if itm else 0.0) * (1.0 if is_call else -1.0), "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    price = black76(F, K, T, sigma, r, is_call)
    return {
        "delta": df * (_n(d1) if is_call else -_n(-d1)),
        "gamma": df * _phi(d1) / (F * v),
        "vega": df * F * _phi(d1) * math.sqrt(T),
        "theta": r * price - df * F * _phi(d1) * sigma / (2.0 * math.sqrt(T)),
    }


def expected_move(F: float, sigma: float, T: float) -> float:
    """市場隱含的 1 個標準差波動（點）：F·σ·√T。ATM 跨式的價格約為它的 0.8 倍（隱含的平均絕對移動）。"""
    return F * sigma * math.sqrt(T) if T > 0 and sigma > 0 else 0.0


def mid_price(bid: float, ask: float, last: float = 0.0) -> float:
    """買賣中價優先（最近成交價可能是很久以前的陳舊價）；價差過寬或單邊沒報價才退回最近成交價，都沒有回傳 0。"""
    if bid > 0 and ask >= bid:
        m = (bid + ask) / 2
        if ask - bid <= MAX_SPREAD_RATIO * m:
            return m
    return float(last or 0.0)


def _r1(x: float | None, nd: int = 1) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


# ── 選擇權鏈 ──────────────────────────────────────────────────────

def _side(q: dict[str, Any] | None) -> dict[str, float] | None:
    if not q:
        return None
    bid, ask, last = float(q.get("bid") or 0), float(q.get("ask") or 0), float(q.get("close") or q.get("last") or 0)
    return {"bid": bid, "ask": ask, "last": last, "mid": mid_price(bid, ask, last), "volume": float(q.get("total_volume") or q.get("volume") or 0)}


def parity_forward(rows: dict[float, dict[str, dict]], T: float, r: float, hint: float | None = None) -> float | None:
    """由買賣平價反推遠期價：C − P ＝ df·(F − K)。取「買賣兩邊都有報價、|C−P| 最小」（最靠近平值）的最多 3 檔平均；
    有 hint（例如期貨現價）時只在 hint 附近 5 檔裡挑。沒有任何一檔兩邊都有報價回傳 None。"""
    df = math.exp(-r * T)
    cands = []
    for k, d in rows.items():
        c, p = d.get("C"), d.get("P")
        if c and p and c["mid"] > 0 and p["mid"] > 0:
            cands.append((k, c["mid"] - p["mid"]))
    if not cands:
        return None
    if hint:
        cands = sorted(cands, key=lambda x: abs(x[0] - hint))[:5]
    best = sorted(cands, key=lambda x: abs(x[1]))[:3]
    fs = [k + diff / df for k, diff in best]
    return sum(fs) / len(fs)


def build_chain(quotes: Sequence[dict[str, Any]], T: float, r: float = RATE, F: float | None = None, hint: float | None = None) -> dict[str, Any]:
    """把一筆一筆的報價 {strike, right('C'|'P'), bid, ask, close?, total_volume?} 整理成選擇權鏈：
    每個履約價的買賣權各有 bid／ask／mid／IV／delta、價差；加上遠期價 F 與 ATM IV。
    F 沒給就用買賣平價反推（hint＝期貨現價，可省略）。IV 反推不出來或超出 3%~150% 的記 None。"""
    if T <= 0:
        raise ValueError("已到期（T<=0），無法試算")
    by: dict[float, dict[str, dict]] = {}
    for q in quotes:
        right = str(q.get("right", "")).upper()[:1]
        if right not in ("C", "P"):
            continue
        s = _side(q)
        if s:
            by.setdefault(float(q["strike"]), {})[right] = s
    if not by:
        raise ValueError("鏈上沒有任何報價")
    if F is None:
        F = parity_forward(by, T, r, hint)
        if F is None:
            if hint:
                F = float(hint)
            else:
                raise ValueError("鏈上沒有買賣兩邊都有報價的履約價，反推不出遠期價（請改用手動輸入）")
    F = float(F)
    rows = []
    for k in sorted(by):
        row: dict[str, Any] = {"strike": k, "call": None, "put": None}
        for right, key in (("C", "call"), ("P", "put")):
            s = by[k].get(right)
            if not s:
                continue
            iv = implied_vol(s["mid"], F, k, T, r, right == "C") if s["mid"] > 0 else None
            if iv is not None and not (IV_SANE[0] <= iv <= IV_SANE[1]):
                iv = None
            delta = greeks(F, k, T, iv, r, right == "C")["delta"] if iv else None
            spread = s["ask"] - s["bid"] if s["bid"] > 0 and s["ask"] >= s["bid"] else None
            row[key] = {**s, "iv": iv, "delta": delta, "spread": spread,
                        "spread_pct": (spread / s["mid"]) if spread is not None and s["mid"] > 0 else None}
        rows.append(row)
    return {"F": F, "T": T, "r": r, "atm_iv": _atm_iv(rows, F), "rows": rows}


def _atm_iv(rows: list[dict], F: float) -> float | None:
    """最靠近 F 的履約價（買賣權 IV 都有）的平均；找不到就取最近 6 檔所有可用 IV 的平均。"""
    for row in sorted(rows, key=lambda x: abs(x["strike"] - F)):
        c, p = row["call"], row["put"]
        if c and p and c["iv"] and p["iv"]:
            return (c["iv"] + p["iv"]) / 2
    ivs = [s["iv"] for row in sorted(rows, key=lambda x: abs(x["strike"] - F))[:6] for s in (row["call"], row["put"]) if s and s["iv"]]
    return sum(ivs) / len(ivs) if ivs else None


def model_quotes(F: float, iv_pct: float, days: float, r: float = RATE, step: float = 50.0, span: float = 0.15,
                 spread_pct: float = 0.02, min_spread: float = 1.0) -> list[dict[str, Any]]:
    """手動（離線）模式：用 Black-76 與單一 IV 造出一條模擬的選擇權鏈（沒有偏斜；買賣價差＝中價的 spread_pct，至少 min_spread 點）。"""
    T, sigma = days / YEAR_DAYS, iv_pct / 100.0
    k0 = round(F / step) * step
    out = []
    n = int(F * span / step)
    for i in range(-n, n + 1):
        k = k0 + i * step
        if k <= 0:
            continue
        for right in ("C", "P"):
            mid = black76(F, k, T, sigma, r, right == "C")
            half = max(min_spread / 2, spread_pct * mid / 2)
            out.append({"strike": k, "right": right, "bid": round(max(mid - half, 0.0), 1), "ask": round(mid + half, 1), "close": round(mid, 1)})
    return out


# ── 選履約價、組部位 ───────────────────────────────────────────────

def _cands(chain: dict, right: str) -> list[dict]:
    """某一邊（'C' 或 'P'）能買的檔（有賣價）：[{strike, side(報價), ...}]。"""
    key = "call" if right == "C" else "put"
    return [{"strike": row["strike"], **row[key]} for row in chain["rows"] if row[key] and row[key]["ask"] > 0]


def _leg(c: dict, right: str, qty: int = 1, fill: str = "ask") -> dict[str, Any]:
    entry = c["ask"] if fill == "ask" else (c["mid"] or c["ask"])
    return {"right": right, "strike": c["strike"], "qty": qty, "bid": c["bid"], "ask": c["ask"], "mid": c["mid"], "iv": c["iv"],
            "delta": c["delta"], "spread": c["spread"], "spread_pct": c["spread_pct"], "entry": entry}


def pick_atm(chain: dict) -> float | None:
    """買賣權都能買的檔裡，最靠近 F 的履約價（跨式用）。"""
    both = {c["strike"] for c in _cands(chain, "C")} & {c["strike"] for c in _cands(chain, "P")}
    return min(both, key=lambda k: abs(k - chain["F"])) if both else None


def pick_by_delta(chain: dict, right: str, target: float, otm_only: bool = True) -> dict | None:
    """|delta| 最接近 target 的檔。otm_only＝只在價外那一側挑（勒式的兩翼；單向買 Call／Put 可以含價內）。"""
    F = chain["F"]
    c = [x for x in _cands(chain, right) if x["delta"] is not None and (not otm_only or (x["strike"] > F if right == "C" else x["strike"] < F))]
    return min(c, key=lambda x: (abs(abs(x["delta"]) - target), abs(x["strike"] - F))) if c else None


def pick_by_move(chain: dict, right: str, dist: float) -> dict | None:
    """履約價最靠近 F ± dist 的檔（Call 往上、Put 往下；dist 為負＝價內）。"""
    F = chain["F"]
    want = F + dist if right == "C" else F - dist
    c = _cands(chain, right)
    return min(c, key=lambda x: (abs(x["strike"] - want), abs(x["strike"] - F))) if c else None


def select_legs(chain: dict, kind: str, select: str = "delta", delta: float = 0.25, k: float = 0.5,
                ref_move: float | None = None, fill: str = "ask") -> list[dict[str, Any]]:
    """依結構與選法挑出各腳（都是買進、1 口）。
    straddle：最靠近 F 的同一履約價；strangle：兩翼各一（delta＝兩翼的 |delta|；move＝距 F ± k×預期波動）；
    call／put：單向（delta＝目標 |delta|，可含價內；move＝價外 k×預期波動，k<0 為價內）。
    找不到能買的檔丟 ValueError（說明哪一腳沒有）。"""
    if kind not in KINDS:
        raise ValueError(f"結構必須是 {KINDS}")
    if select not in ("delta", "move"):
        raise ValueError("履約價選法必須是 delta 或 move")
    em = ref_move if ref_move and ref_move > 0 else expected_move(chain["F"], chain["atm_iv"] or 0.0, chain["T"])
    if kind == "straddle":
        k0 = pick_atm(chain)
        if k0 is None:
            raise ValueError("找不到買權與賣權同一履約價都有賣價的檔，無法組跨式")
        legs = []
        for right in ("C", "P"):
            c = next(x for x in _cands(chain, right) if x["strike"] == k0)
            legs.append(_leg(c, right, fill=fill))
        return legs
    rights = ("C", "P") if kind == "strangle" else (("C",) if kind == "call" else ("P",))
    legs = []
    for right in rights:
        if select == "delta":
            c = pick_by_delta(chain, right, delta, otm_only=(kind == "strangle"))
        else:
            if em <= 0:
                raise ValueError("沒有預期波動可用（ATM IV 未知），請自己輸入預期波動點數")
            c = pick_by_move(chain, right, k * em)
        if c is None:
            raise ValueError(f"找不到{'買權' if right == 'C' else '賣權'}能買的檔（沒有賣價或 IV 反推不出來）")
        legs.append(_leg(c, right, fill=fill))
    return legs


# ── 到期損益、損益兩平 ─────────────────────────────────────────────

def payoff(legs: Sequence[dict], S: float) -> float:
    """到期時各腳的價值總和（都是買進：Call＝max(S−K,0)、Put＝max(K−S,0)，乘口數）。"""
    return sum(l["qty"] * max((S - l["strike"]) if l["right"] == "C" else (l["strike"] - S), 0.0) for l in legs)


def breakevens(legs: Sequence[dict], basis: float) -> list[float]:
    """到期損益＝payoff(S) − basis ＝ 0 的所有 S（由小到大）。basis＝總成本（平倉試算時為「總成本 − 已收回的錢」）。
    部位是分段線性：在各履約價處轉折，逐段找零點；左右兩個尾巴用斜率延伸。S<=0 的零點不算。"""
    if not legs or basis <= 0:
        return []
    ks = sorted({l["strike"] for l in legs})
    f = lambda s: payoff(legs, s) - basis      # noqa: E731
    pts = [0.0] + ks
    out: list[float] = []
    for a, b in zip(pts, pts[1:]):
        fa, fb = f(a), f(b)
        if fa == 0 and a > 0:
            out.append(a)
        if fa * fb < 0:
            out.append(a + (b - a) * (-fa) / (fb - fa))
    if f(ks[-1]) == 0:
        out.append(ks[-1])
    slope_up = sum(l["qty"] for l in legs if l["right"] == "C")
    if slope_up > 0 and f(ks[-1]) < 0:
        out.append(ks[-1] - f(ks[-1]) / slope_up)
    slope_dn = sum(l["qty"] for l in legs if l["right"] == "P")
    if slope_dn > 0 and f(ks[0]) < 0:
        s0 = ks[0] + f(ks[0]) / slope_dn
        if s0 > 0:
            out.append(s0)
    return sorted(set(round(x, 6) for x in out))


# ── 重新估價與情境 ─────────────────────────────────────────────────

def position_value(legs: Sequence[dict], S: float, T_rem: float, r: float, iv_shift: float = 0.0, default_iv: float = 0.2) -> float:
    """標的（遠期價）變成 S、剩下 T_rem 年、各腳 IV 平移 iv_shift（小數，0.025＝2.5 個波動點）時，各腳的理論價值總和。"""
    tot = 0.0
    for l in legs:
        sigma = max((l.get("iv") or default_iv) + iv_shift, 1e-4)
        tot += l["qty"] * black76(S, l["strike"], T_rem, sigma, r, l["right"] == "C")
    return tot


def _move_rows(F: float, ref: float, mults: Sequence[float] = SCENARIO_MULTS) -> list[dict[str, float]]:
    return [{"mult": m, "pts": round(m * ref, 1), "pct": round(m * ref / F, 5), "S": max(F + m * ref, 1.0)} for m in mults]


def _time_cols(T: float, hold_hours: float) -> list[dict[str, Any]]:
    """情境表的時間欄：現在、持有 hold_hours 小時後、1 天後、到期（去掉重複與超過到期的）。"""
    cands = [("現在", 0.0)]
    if hold_hours and hold_hours > 0:
        cands.append((f"{hold_hours:g} 小時後", hold_hours / 24.0))
    cands += [("1 天後", 1.0), ("到期", T * YEAR_DAYS)]
    out, seen = [], set()
    for label, d in sorted(cands, key=lambda x: x[1]):
        d = min(d, T * YEAR_DAYS)
        key = round(d, 4)
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": "到期" if key == round(T * YEAR_DAYS, 4) else label, "days": d})
    return out


def scenario_tables(legs: Sequence[dict], F: float, T: float, r: float, basis: float, ref: float, hold_hours: float,
                    default_iv: float, realized: float = 0.0, mult: float = MULT, lots: int = 1) -> dict[str, Any]:
    """價格×時間（IV 不變）與價格×IV 平移（在持有 hold_hours 後）的損益表（點與元）。
    損益＝realized ＋ 理論價值 − basis；其中 basis 是各腳買進成本加總（平倉試算時是留下那幾腳的成本）。"""
    moves = _move_rows(F, ref)
    cols = _time_cols(T, hold_hours)

    def pnl(S: float, days: float, shift: float) -> float:
        return realized + position_value(legs, S, max(T - days / YEAR_DAYS, 0.0), r, shift, default_iv) - basis

    grid = [[pnl(m["S"], c["days"], 0.0) for c in cols] for m in moves]
    d_hold = min(max(hold_hours, 0.0) / 24.0 if hold_hours else 1.0, T * YEAR_DAYS)
    iv_grid = [[pnl(m["S"], d_hold, s / 100.0) for s in IV_SHOCKS] for m in moves]
    return {"moves": [{k: v for k, v in m.items() if k != "S"} for m in moves], "time_cols": cols,
            "pnl_pts": [[_r1(x) for x in row] for row in grid], "pnl_twd": [[round(x * mult * lots) for x in row] for row in grid],
            "iv": {"after_days": round(d_hold, 4), "shocks": list(IV_SHOCKS), "pnl_pts": [[_r1(x) for x in row] for row in iv_grid],
                   "pnl_twd": [[round(x * mult * lots) for x in row] for row in iv_grid]}}


# ── 機率 ──────────────────────────────────────────────────────────

def prob_lognormal(F: float, sigma: float, T: float, bes: Sequence[float], below: Sequence[bool]) -> float | None:
    """風險中性（遠期價為平均）、對數常態：到期時越過損益兩平的機率。bes[i] 是損益兩平價，below[i]＝True 表示「低於它才獲利」（下方那個）。
    同時有上下兩個時回傳兩邊機率相加（兩者互斥）。IV 不明回傳 None。"""
    if not sigma or sigma <= 0 or T <= 0 or not bes:
        return None
    v = sigma * math.sqrt(T)
    p = 0.0
    for b, lo in zip(bes, below):
        d2 = (math.log(F / b) - 0.5 * v * v) / v
        p += _n(-d2) if lo else _n(d2)
    return min(max(p, 0.0), 1.0)


def hist_stats(closes: Sequence[float], h: int, F: float, bes: Sequence[float], below: Sequence[bool]) -> dict[str, Any] | None:
    """歷史上任意 h 個交易日（窗口重疊）的漲跌：越過損益兩平的比例，以及漲跌幅絕對值（換算成現在的 F 的點數）的分位數。
    closes＝每日收盤（由舊到新）。樣本太少（< 30 個窗口）回傳 None。"""
    c = [x for x in closes if x and x > 0]
    if h < 1 or len(c) <= h + 30:
        return None
    rets = [math.log(c[i + h] / c[i]) for i in range(len(c) - h)]
    n = len(rets)
    p = 0.0
    for b, lo in zip(bes, below):
        t = math.log(b / F)
        p += sum(1 for x in rets if (x < t if lo else x > t)) / n
    absm = sorted(abs(F * (math.exp(x) - 1.0)) for x in rets)
    q = lambda f: absm[min(int(f * n), n - 1)]      # noqa: E731
    return {"p": min(p, 1.0), "n": n, "h_days": h, "abs_move_q": {"50": round(q(0.5), 1), "75": round(q(0.75), 1), "90": round(q(0.9), 1)}}


def trading_days(T: float) -> int:
    """日曆天 → 約略的交易日數（週休二日；至少 1）。"""
    return max(1, round(T * YEAR_DAYS * 5.0 / 7.0))


# ── 用微台對沖 delta ──────────────────────────────────────────────

def hedge_view(legs: Sequence[dict], F: float, T: float, r: float, default_iv: float, *, mult: float = MULT, lots: int = 1,
               closes: Sequence[float] | None = None, tmf_mult: float = TMF_MULT, cost_pts: float = HEDGE_COST_PTS) -> dict[str, Any]:
    """買了選擇權、想用微台把 delta 中和（gamma scalping）：現在要買賣幾口微台、對沖之後「每天要動多少才打平時間價值」、最近實際動多少。
    1 個選擇權單位的 delta d ＝ 標的每動 1 點、部位賺賠 d × 50 元（臺指選擇權每點 50）；微台每點 10 元 → 中和要的微台口數 ＝ −d × 50 / 10（正＝買進、負＝賣出）。
    對沖後的每天損益 ≈ ½·Γ·(ΔS)² − |Θ|，所以每個交易日的均方根波動要超過 sqrt(2·|Θ|/Γ) 才賺（Θ 是每日曆天、一週有 5 個交易日，所以乘 7/5）。
    這只是試算：不下單；對沖本身的期望值多半是負的（實際波動通常不比隱含波動大，update.md 發現 30）。"""
    d = g = th = 0.0
    for l in legs:
        x = greeks(F, l["strike"], T, l.get("iv") or default_iv, r, l["right"] == "C")
        d += l["qty"] * x["delta"]
        g += l["qty"] * x["gamma"]
        th += l["qty"] * x["theta"] / YEAR_DAYS
    net = d * lots
    exact = -net * mult / tmf_mult
    n = round(exact)
    residual = net + n * tmf_mult / mult                       # 對沖後剩下的 delta（選擇權單位；1 口微台 ＝ tmf_mult/mult 個）
    be = math.sqrt(2.0 * abs(th) * 1.4 / g) if g > 1e-12 and th < 0 else None
    real = None
    if closes:
        c = [x for x in closes[-21:] if x and x > 0]
        if len(c) >= 11:
            diffs = [b - a for a, b in zip(c, c[1:])]
            real = math.sqrt(sum(x * x for x in diffs) / len(diffs))
    sig = default_iv
    return {"net_delta": _r1(net, 3), "delta_twd_per_pt": round(net * mult), "tmf_lots": n, "tmf_lots_exact": _r1(exact, 2),
            "residual_delta": _r1(residual, 3), "residual_twd_per_pt": round(residual * mult),
            "gamma": _r1(g * lots, 5), "theta_day_twd": round(th * lots * mult),
            "be_rms_pts": _r1(be, 0) if be else None,
            "implied_rms_pts": _r1(F * sig * math.sqrt(1.4 / YEAR_DAYS), 0) if sig else None,
            "realized_rms_pts": _r1(real, 0) if real else None,
            "ratio": round(real / be, 2) if real and be else None,
            "cost_per_hedge_twd": round(abs(n) * cost_pts * tmf_mult), "cost_pts": cost_pts}


# ── 試算（A）─────────────────────────────────────────────────────

def _greek_sum(legs: Sequence[dict], F: float, T: float, r: float, default_iv: float, mult: float, lots: int) -> dict[str, float]:
    d = g = th = v = 0.0
    for l in legs:
        x = greeks(F, l["strike"], T, l.get("iv") or default_iv, r, l["right"] == "C")
        d += l["qty"] * x["delta"]
        g += l["qty"] * x["gamma"]
        th += l["qty"] * x["theta"] / YEAR_DAYS
        v += l["qty"] * x["vega"] / 100.0
    return {"delta": _r1(d, 3), "gamma": _r1(g, 5), "theta_day_pts": _r1(th, 2), "vega_pts": _r1(v, 2),
            "delta_twd": round(d * mult * lots), "theta_day_twd": round(th * mult * lots), "vega_twd": round(v * mult * lots)}


def _be_view(levels: Sequence[float], F: float, em: float) -> list[dict[str, Any]]:
    return [{"level": _r1(b), "dist_pts": _r1(b - F), "dist_pct": round((b - F) / F, 5), "dist_em": round((b - F) / em, 3) if em > 0 else None}
            for b in levels]


def analyze(chain: dict, legs: Sequence[dict], *, kind: str = "", lots: int = 1, budget_twd: float | None = None,
            ref_move: float | None = None, hold_hours: float = 6.0, closes: Sequence[float] | None = None,
            mult: float = MULT) -> dict[str, Any]:
    """對挑好的各腳做完整試算：成本、損益兩平、預期波動、機率、Greeks、情境表、提醒。"""
    F, T, r = chain["F"], chain["T"], chain["r"]
    iv0 = chain["atm_iv"] or 0.2
    em1 = expected_move(F, chain["atm_iv"] or 0.0, T)
    em = ref_move if ref_move and ref_move > 0 else em1
    cost = sum(l["qty"] * l["entry"] for l in legs)
    cost_mid = sum(l["qty"] * (l["mid"] or l["entry"]) for l in legs)
    exit_spread = sum(l["qty"] * (l["ask"] - l["bid"]) for l in legs if l["bid"] > 0)
    levels = breakevens(legs, cost)
    below = [b < F for b in levels]
    be = _be_view(levels, F, em1)                     # 損益兩平離現價幾個「市場隱含 1σ」（不受你自己填的預期波動影響）
    p_ln = prob_lognormal(F, chain["atm_iv"] or 0.0, T, levels, below) if levels else None
    hist = hist_stats(closes or [], trading_days(T), F, levels, below) if closes and levels else None
    per_lot_twd = cost * mult
    warnings: list[str] = []
    for l in legs:
        name = f"{'買權' if l['right'] == 'C' else '賣權'} {l['strike']:g}"
        if l["bid"] <= 0:
            warnings.append(f"{name} 沒有買價：只能照賣價進場，出場可能很難成交（或要大幅讓價）")
        elif l["spread_pct"] is not None and l["spread_pct"] > WARN_SPREAD:
            warnings.append(f"{name} 買賣價差佔中價 {l['spread_pct'] * 100:.0f}%（{l['spread']:.1f} 點）：進出一趟的成本偏高")
        if l["iv"] is None:
            warnings.append(f"{name} 的 IV 反推不出來，Greeks 與情境表改用 ATM IV 估")
    if T * YEAR_DAYS < WARN_DAYS:
        warnings.append(f"距到期只剩 {T * YEAR_DAYS:.1f} 天：Gamma 很大、時間價值流失很快，情境表的誤差也會變大")
    if chain["atm_iv"] is None:
        warnings.append("ATM IV 不明：預期波動、機率與情境表都是用 20% 估的，請當作示意")
    out = {
        "kind": kind, "F": _r1(F), "days": round(T * YEAR_DAYS, 2), "atm_iv": _r1((chain["atm_iv"] or 0) * 100, 2) if chain["atm_iv"] else None,
        "legs": [{"right": l["right"], "strike": l["strike"], "qty": l["qty"], "bid": _r1(l["bid"]), "ask": _r1(l["ask"]), "mid": _r1(l["mid"]),
                  "entry": _r1(l["entry"]), "iv": _r1(l["iv"], 4), "delta": _r1(l["delta"], 4), "spread": _r1(l["spread"]),
                  "spread_pct": _r1(l["spread_pct"], 4)} for l in legs],
        "cost": {"pts": _r1(cost), "mid_pts": _r1(cost_mid), "slippage_pts": _r1(cost - cost_mid), "exit_spread_pts": _r1(exit_spread),
                 "per_lot_twd": round(per_lot_twd), "lots": lots, "total_twd": round(per_lot_twd * lots),
                 "budget_twd": budget_twd, "max_lots": int(budget_twd // per_lot_twd) if budget_twd and per_lot_twd > 0 else None},
        "max_loss": {"pts": _r1(cost), "twd": round(per_lot_twd * lots)},
        "expected_move": {"sigma_pts": _r1(em1), "straddle_pts": _r1(em1 * math.sqrt(2.0 / math.pi)), "ref_pts": _r1(em),
                          "ref_source": "user" if ref_move and ref_move > 0 else "implied"},
        "breakeven": be,
        "prob": {"lognormal": _r1(p_ln, 4) if p_ln is not None else None, "hist": hist},
        "greeks": _greek_sum(legs, F, T, r, iv0, mult, lots),
        "hedge": hedge_view(legs, F, T, r, iv0, mult=mult, lots=lots, closes=closes),
        "scenarios": scenario_tables(legs, F, T, r, cost, em, hold_hours, iv0, mult=mult, lots=lots),
        "warnings": warnings,
    }
    return out


# ── 平倉試算（B）──────────────────────────────────────────────────

def _marks(legs: Sequence[dict], F: float, T: float, r: float, iv: float | None, marks: Sequence[dict] | None) -> list[dict[str, Any]]:
    """每一腳目前的賣出價（bid）、中價與 IV。有報價就用報價；沒有就用 Black-76 與 iv 估（沒有價差）。"""
    out = []
    for i, l in enumerate(legs):
        m = (marks[i] if marks and i < len(marks) else None) or {}
        bid, ask = float(m.get("bid") or 0), float(m.get("ask") or 0)
        mid = mid_price(bid, ask, float(m.get("close") or 0))
        iv_i = m.get("iv")
        if mid > 0 and not iv_i:
            iv_i = implied_vol(mid, F, l["strike"], T, r, l["right"] == "C")
        if mid <= 0:
            sig = iv_i or iv
            if not sig:
                raise ValueError("沒有報價也沒有 IV，無法估目前價格")
            mid = black76(F, l["strike"], T, sig, r, l["right"] == "C")
            bid = ask = mid
            iv_i = sig
        out.append({"bid": bid if bid > 0 else 0.0, "ask": ask, "mid": mid, "iv": iv_i or iv})
    return out


def close_analysis(legs: Sequence[dict], *, F: float, T: float, r: float = RATE, iv: float | None = None,
                   marks: Sequence[dict] | None = None, ref_move: float | None = None, hold_hours: float = 24.0,
                   mult: float = MULT, closes: Sequence[float] | None = None) -> dict[str, Any]:
    """已持有（買進）的各腳：legs＝[{right, strike, qty, entry}]（entry＝買進價，點）。
    對「全部持有」「只平掉某一腳」「全部平掉」各算：已實現損益（以買價 bid 賣出）、留下來的損益兩平（扣掉已收回的錢）、
    還要再走多少、最大總虧損、情境表（到期與持有 hold_hours 後）與 Greeks。
    損益兩平的算法：總損益＝已收回的錢 − 總成本 ＋ 留下各腳到期價值，令它為 0；等於「留下的腳到期要值多少才回本」＝淨成本。"""
    if T <= 0:
        raise ValueError("已到期，沒有可平倉的部位")
    if not legs:
        raise ValueError("沒有任何一腳")
    ls = [{"right": str(l["right"]).upper()[:1], "strike": float(l["strike"]), "qty": 1 if l.get("qty") is None else int(l["qty"]),
           "entry": float(l["entry"])} for l in legs]                     # 沒給口數＝1 口；明講 0 口要擋下來（不能默默變成 1 口）
    if any(l["right"] not in ("C", "P") or l["qty"] <= 0 or l["entry"] < 0 or l["strike"] <= 0 for l in ls):
        raise ValueError("每一腳要有買賣權（C／P）、履約價、口數（正整數）與買進價")
    mk = _marks(ls, F, T, r, iv, marks)
    for l, m in zip(ls, mk):
        l["iv"] = m["iv"]
    sig = [m["iv"] for m in mk if m["iv"]]
    iv_ref = sum(sig) / len(sig) if sig else (iv or 0.2)
    em_rem = expected_move(F, iv_ref, T)
    ref = ref_move if ref_move and ref_move > 0 else em_rem
    E = sum(l["qty"] * l["entry"] for l in ls)
    pnl_leg = [l["qty"] * (m["mid"] - l["entry"]) for l, m in zip(ls, mk)]
    order = sorted(range(len(ls)), key=lambda i: pnl_leg[i])
    role = {}
    if len(ls) >= 2:
        role[order[0]], role[order[-1]] = "loser", "winner"
    legs_out = [{**l, "bid": _r1(m["bid"]), "ask": _r1(m["ask"]), "mid": _r1(m["mid"]), "iv": _r1(m["iv"], 4), "pnl_mid_pts": _r1(p),
                 "pnl_bid_pts": _r1(l["qty"] * (m["bid"] - l["entry"])), "role": role.get(i)} for i, (l, m, p) in enumerate(zip(ls, mk, pnl_leg))]

    def action(aid: str, label: str, closed: set[int]) -> dict[str, Any]:
        keep = [l for i, l in enumerate(ls) if i not in closed]
        proceeds = sum(ls[i]["qty"] * mk[i]["bid"] for i in closed)
        realized = sum(ls[i]["qty"] * (mk[i]["bid"] - ls[i]["entry"]) for i in closed)
        basis = E - proceeds                       # 淨成本：留下的腳到期要值這麼多才回本
        levels = breakevens(keep, basis) if keep else []
        now_val = sum(l["qty"] * mk[i]["mid"] for i, l in enumerate(ls) if i not in closed)
        # 總損益 ＝ 已收回的錢 ＋ 留下各腳的價值 − 全部的買進成本 E（所以 basis 傳 E、realized 傳已收回的錢）
        sc = scenario_tables(keep, F, T, r, E, ref, hold_hours, iv_ref, realized=proceeds, mult=mult) if keep else None
        return {"id": aid, "label": label, "closed": sorted(closed),
                "realized_pts": _r1(realized), "realized_twd": round(realized * mult),
                "proceeds_pts": _r1(proceeds), "net_basis_pts": _r1(basis),
                "max_loss_pts": _r1(-basis) if keep else _r1(realized), "max_loss_twd": round((-basis if keep else realized) * mult),
                "remaining_now_pts": _r1(now_val), "need_ratio": round(basis / now_val, 3) if keep and now_val > 0 and basis > 0 else None,
                "breakeven": _be_view(levels, F, em_rem), "greeks": _greek_sum(keep, F, T, r, iv_ref, mult, 1) if keep else None,
                "hedge": hedge_view(keep, F, T, r, iv_ref, mult=mult, lots=1, closes=closes) if keep else None,
                "scenarios": sc}

    actions = [action(ACTION_HOLD, "全部持有", set())]
    for i, l in enumerate(ls):
        actions.append(action(f"close_{i}", f"只平掉 {'買權' if l['right'] == 'C' else '賣權'} {l['strike']:g}", {i}))
    if len(ls) > 1:
        actions.append(action(ACTION_ALL, "全部平掉", set(range(len(ls)))))
    warnings = []
    if any(m["bid"] <= 0 for m in mk):
        warnings.append("有些腳沒有買價（bid）：平倉試算以 0 計，實際可能要大幅讓價才賣得掉")
    if marks is None:
        warnings.append("沒有現價報價：目前價格是用 Black-76 與你給的 IV 估的（沒有買賣價差），平倉的已實現損益會比實際好看")
    return {"F": _r1(F), "days": round(T * YEAR_DAYS, 2), "iv": _r1(iv_ref * 100, 2), "em_sigma_pts": _r1(em_rem), "ref_pts": _r1(ref),
            "entry_total_pts": _r1(E), "legs": legs_out, "actions": actions, "warnings": warnings}


# ── 跨式 vs 勒式：各指數點位的損益（〈選擇權試算〉的比較表與曲線）────────────

COMPARE_STEPS = (50, 100, 200, 250, 500, 1000, 2000, 5000)
COMPARE_MAX_ROWS = 25
COMPARE_CURVE_POINTS = 121


def _nice_step(span: float, max_rows: int = COMPARE_MAX_ROWS) -> float:
    """讓 ±span 之間的列數不超過 max_rows 的最小整齊間距。"""
    for s in COMPARE_STEPS:
        if 2 * span / s <= max_rows - 1:
            return float(s)
    return float(COMPARE_STEPS[-1])


def _value_at(legs: Sequence[dict], S: float, T_rem: float, r: float, iv0: float) -> float:
    return payoff(legs, S) if T_rem <= 1e-9 else position_value(legs, S, T_rem, r, 0.0, iv0)


def compare_structures(chain: dict, *, select: str = "delta", delta: float = 0.25, k: float = 0.5, ref_move: float | None = None,
                       fill: str = "ask", lots: int = 1, hold_days: float = 1.0, mult: float = MULT) -> dict[str, Any]:
    """同一條鏈上的買跨式與買勒式，在各個標的點位（遠期價）的損益（元）：到期時，以及持有 hold_days 天後（IV 不變、用 Black-76 重估）。
    點位以現價為中心、整齊間距；範圍涵蓋 ±1.5 個預期波動與兩者所有的損益兩平點（再多 20%）。曲線另外給 121 個點畫圖。只試算、不下單。"""
    F, T, r = chain["F"], chain["T"], chain["r"]
    iv0 = chain["atm_iv"] or 0.2
    em = ref_move if ref_move and ref_move > 0 else expected_move(F, chain["atm_iv"] or 0.0, T)
    T_hold = max(T - max(hold_days, 0.0) / YEAR_DAYS, 0.0)
    out: dict[str, Any] = {"F": round(F, 1), "days": round(T * YEAR_DAYS, 2), "hold_days": hold_days, "hold_is_expiry": T_hold <= 1e-9,
                           "atm_iv": chain["atm_iv"], "em_pts": round(em, 1), "lots": lots, "structures": {}, "warnings": []}
    legs_by: dict[str, list[dict]] = {}
    far = 0.0
    for kind in ("straddle", "strangle"):
        legs = select_legs(chain, kind, select=select, delta=delta, k=k, ref_move=ref_move, fill=fill)
        cost = sum(l["qty"] * l["entry"] for l in legs)
        bes = breakevens(legs, cost)
        far = max([far] + [abs(b - F) for b in bes])
        legs_by[kind] = legs
        out["structures"][kind] = {"legs": [{"right": l["right"], "strike": l["strike"], "entry": l["entry"], "bid": l["bid"], "ask": l["ask"]} for l in legs],
                                   "cost_pts": round(cost, 2), "cost_twd": round(cost * mult * lots), "breakevens": [round(b, 1) for b in bes],
                                   "breakeven_moves": [round(b - F, 1) for b in bes], "max_loss_twd": round(-cost * mult * lots)}
        for l in legs:
            if l["bid"] <= 0:
                out["warnings"].append(f"{'買權' if l['right'] == 'C' else '賣權'} {l['strike']:g} 沒有買價：出場可能很難成交")
    span = max(1.5 * em, far * 1.2, F * 0.02)
    step = _nice_step(span)
    lo = math.floor((F - span) / step) * step
    hi = math.ceil((F + span) / step) * step
    levels: list[float] = []
    x = lo
    while x <= hi + 1e-9:
        if x > 0:
            levels.append(float(x))
        x += step

    def row(S: float) -> dict[str, Any]:
        d: dict[str, Any] = {"S": round(S, 1), "chg": round(S - F, 1), "chg_pct": round((S - F) / F, 5)}
        for kind, legs in legs_by.items():
            cost = out["structures"][kind]["cost_pts"]
            d[kind] = {"expiry": round((payoff(legs, S) - cost) * mult * lots), "hold": round((_value_at(legs, S, T_hold, r, iv0) - cost) * mult * lots)}
        return d

    out["step"] = step
    out["rows"] = [row(S) for S in levels]
    c_lo, c_hi = levels[0], levels[-1]
    out["curve"] = [row(c_lo + (c_hi - c_lo) * i / (COMPARE_CURVE_POINTS - 1)) for i in range(COMPARE_CURVE_POINTS)]
    return out
