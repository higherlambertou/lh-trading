"""慢速順勢（趨勢跟隨）在臺股期貨（TX）日線上的檢驗（update.md 發現 27 ⑤）。

資料：期交所〈期貨每日交易行情〉TX（2003 年起；`python -m core.oi_study fetch --only tx --start-year 2003` 下載到 data/taifex/tx），
只用一般（日盤）交易時段的開盤與收盤。不連券商。

怎麼建構（每一步都在避免偷看與展期的假報酬）：
  - 連續報酬：展期會讓價格跳空。每一期（第 k 日開盤 → 第 k+1 日開盤）都用「這兩天都有成交的最近月合約」算報酬 r_k；
    訊號用的價格指數 C 同理，用收盤價以「兩天都有成交的最近月合約」逐日串接。
  - 持倉：第 j 日收盤後，只用 ≤ j 日的資料決定 p ∈ {−1, +1}（一直在場內，多空都做），第 j+1 日開盤進場，賺 [j+1 開盤, j+2 開盤] 的報酬。
  - 成本：單邊 SIDE_COST_PTS 點（來回 4 點，和其他檢驗一致）。換邊＝平＋開（2 個單邊）、展期＝平舊＋開新（2 個單邊），買進持有也付展期成本。
3 個事先固定的訊號（不調參數）：
  TSMOM120：過去 120 個交易日的報酬為正 → 多、否則空。
  MA200   ：收盤在 200 日均線之上 → 多、之下 → 空。
  Donchian55：收盤突破前 55 日的收盤高點 → 多、跌破前 55 日的收盤低點 → 空，沒有突破就維持原本方向。
檢定：策略淨報酬對買進持有淨報酬做迴歸 R = α + β·R_mkt + ε，檢定 α（Newey-West，落後 HAC_LAG 期）。
  為什麼看 α 不看平均報酬：指數這 23 年從約 4,400 漲到 4 萬多，長期偏多的訊號光靠大盤漲勢就會賺錢，α 才扣掉了這個。
  3 個檢定，Bonferroni |t| ≥ 2.39 才宣稱有 α，|t| ≥ 2 只算線索。
描述（不檢定）：夏普比率與買進持有比較、每年換邊次數、前半／後半期的 α 是否同號、依 60 日波動度縮放部位的夏普比率。

用法：
    python -m core.trend_study [--dir data/taifex] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Optional

import numpy as np

from core.oi_study import DEFAULT_DIR, TX_HEADER, _iso, _num, _rows

SIDE_COST_PTS = 2.0
HAC_LAG = 10
TS_LOOKBACK, MA_WINDOW, DONCHIAN = 120, 200, 55
VOL_TARGET, VOL_WINDOW, VOL_CAP = 0.15, 60, 2.0
MAX_GAP_DAYS = 10                    # 相鄰兩個交易日相隔超過這麼多天（資料缺一段）就不接起來
N_TESTS = 3
Z_CLAIM = NormalDist().inv_cdf(1 - 0.05 / N_TESTS / 2)       # ≈ 2.39
Z_HINT = 2.0
TRADING_DAYS = 252


def verdict(t: float) -> str:
    return "過關" if abs(t) >= Z_CLAIM else ("線索" if abs(t) >= Z_HINT else "沒有")


# ── 資料 ──────────────────────────────────────────────────────────

def load_tx_contracts(directory: Path) -> dict[str, dict[str, tuple[float, float]]]:
    """日期 → {到期月份: (開盤, 收盤)}：一般交易時段、有成交、價差單不算。"""
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for f in sorted(directory.glob("*.csv")):
        for r in _rows(f, TX_HEADER):
            exp = r.get("到期月份(週別)", "").strip()
            if r.get("交易時段", "一般").strip() not in ("一般", "") or not (exp.isdigit() and len(exp) == 6):
                continue
            o, c, v = _num(r.get("開盤價", "")), _num(r.get("收盤價", "")), _num(r.get("成交量", ""))
            if not o or not c or not v or v <= 0:
                continue
            out.setdefault(_iso(r["交易日期"]), {})[exp] = (o, c)
    return dict(sorted(out.items()))


def build_series(contracts: dict[str, dict[str, tuple[float, float]]]) -> dict[str, Any]:
    """逐期（第 k 日開盤 → 第 k+1 日開盤）的連續報酬、串接的收盤指數與展期標記。
    回傳的陣列都以「期」為單位（長度 n−1），close_index 以「日」為單位（長度 n）。"""
    dates = list(contracts)
    keep, r_open, r_close, open_px, hold = [], [], [], [], []
    for i in range(len(dates) - 1):
        a, b = contracts[dates[i]], contracts[dates[i + 1]]
        common = sorted(set(a) & set(b))
        if not common or (date.fromisoformat(dates[i + 1]) - date.fromisoformat(dates[i])).days > MAX_GAP_DAYS:
            keep.append(False)
            r_open.append(0.0)
            r_close.append(0.0)
            open_px.append(a[min(a)][0] if a else 1.0)
            hold.append("")
            continue
        h = common[0]
        keep.append(True)
        r_open.append(b[h][0] / a[h][0] - 1)
        r_close.append(b[h][1] / a[h][1] - 1)
        open_px.append(a[h][0])
        hold.append(h)
    close_index = np.concatenate([[1.0], np.cumprod(1 + np.array(r_close))]) if r_close else np.array([1.0])
    roll = np.array([k > 0 and keep[k] and keep[k - 1] and hold[k] != hold[k - 1] for k in range(len(hold))], bool)
    return {"dates": dates, "keep": np.array(keep, bool), "r_open": np.array(r_open), "r_close": np.array(r_close),
            "open_px": np.array(open_px), "roll": roll, "close_index": close_index}


# ── 訊號（只用 ≤ j 日的資料；NaN＝還不能判斷）────────────────────────

def signal_tsmom(c: np.ndarray, lookback: int = TS_LOOKBACK) -> np.ndarray:
    s = np.full(len(c), np.nan)
    for j in range(lookback, len(c)):
        s[j] = 1.0 if c[j] / c[j - lookback] - 1 > 0 else -1.0
    return s


def signal_ma(c: np.ndarray, window: int = MA_WINDOW) -> np.ndarray:
    s = np.full(len(c), np.nan)
    for j in range(window - 1, len(c)):
        s[j] = 1.0 if c[j] > c[j - window + 1:j + 1].mean() else -1.0
    return s


def signal_donchian(c: np.ndarray, n: int = DONCHIAN) -> np.ndarray:
    s = np.full(len(c), np.nan)
    state = np.nan
    for j in range(n, len(c)):
        prev = c[j - n:j]
        if c[j] > prev.max():
            state = 1.0
        elif c[j] < prev.min():
            state = -1.0
        s[j] = state
    return s


SIGNALS = {"TSMOM120": signal_tsmom, "MA200": signal_ma, "Donchian55": signal_donchian}


# ── 報酬與統計 ────────────────────────────────────────────────────

def strategy_returns(series: dict[str, Any], position: np.ndarray, side_cost: float = SIDE_COST_PTS) -> np.ndarray:
    """position[k]＝第 k 期持有的部位（決定於第 k−1 日收盤）。回傳每期的淨報酬（NaN＝沒有資料的期）。"""
    r, px, roll = series["r_open"], series["open_px"], series["roll"]
    prev = np.concatenate([[position[0]], position[:-1]])
    trades = np.where(roll, 2.0, np.abs(position - prev))
    out = position * r - side_cost * trades / px
    out[~series["keep"]] = np.nan
    return out


def positions_from_signal(sig: np.ndarray) -> np.ndarray:
    """第 j 日收盤後才知道的訊號，只能在第 j+1 日開盤進場、賺第 j+1 期（[j+1 日開盤, j+2 日開盤]）的報酬：
    所以期 k 的部位＝第 k−1 日收盤的訊號（期 0 還沒有）。期的個數比日少 1。"""
    return np.concatenate([[np.nan], sig[:-2]])


def newey_west_alpha(y: np.ndarray, x: np.ndarray, lag: int = HAC_LAG) -> dict[str, float]:
    """y = α + β x + ε 的 OLS，α 的標準誤用 Newey-West（Bartlett 權重）。"""
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    b = XtX_inv @ X.T @ y
    e = y - X @ b
    u = X * e[:, None]                                     # 每一期的「分數」
    S = u.T @ u
    for l in range(1, lag + 1):
        G = u[l:].T @ u[:-l]
        S += (1 - l / (lag + 1)) * (G + G.T)
    cov = XtX_inv @ S @ XtX_inv
    se = math.sqrt(max(cov[0, 0], 0.0))
    return {"alpha": float(b[0]), "beta": float(b[1]), "t": float(b[0] / se) if se > 0 else 0.0, "se": se}


def sharpe(x: np.ndarray) -> float:
    sd = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
    return float(np.mean(x) / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def vol_scaled(series: dict[str, Any], position: np.ndarray, net: np.ndarray) -> np.ndarray:
    """部位乘上 min(上限, 目標波動 ÷ 過去 60 期的年化波動)；波動只用決定當時已知的報酬（≤ 第 k−2 期）。描述用。"""
    rc = series["r_close"]
    out = np.full(len(net), np.nan)
    for k in range(VOL_WINDOW + 1, len(net)):
        sd = float(np.std(rc[k - 1 - VOL_WINDOW:k - 1], ddof=1)) * math.sqrt(TRADING_DAYS)
        scale = VOL_CAP if sd == 0 else min(VOL_CAP, VOL_TARGET / sd)
        out[k] = scale * net[k]
    return out


def study(contracts: dict[str, dict[str, tuple[float, float]]]) -> dict[str, Any]:
    series = build_series(contracts)
    dates = series["dates"]
    if len(dates) < MA_WINDOW + 400:
        raise ValueError(f"TX 日行情只有 {len(dates)} 天，太少（先執行 python -m core.oi_study fetch --only tx --start-year 2003）")
    c = series["close_index"]
    pos = {name: positions_from_signal(fn(c)) for name, fn in SIGNALS.items()}
    start = max(int(np.argmax(~np.isnan(p))) for p in pos.values())            # 三個訊號都有定義的第一期：同一段樣本才能比較
    ok = series["keep"].copy()
    ok[:start] = False
    flat = np.ones(len(series["roll"]))
    mkt = strategy_returns(series, flat)
    idx = np.flatnonzero(ok & ~np.isnan(mkt))
    mid = dates[idx[len(idx) // 2]]
    years = len(idx) / TRADING_DAYS
    tests: dict[str, Any] = {}
    for name, p in pos.items():
        net = strategy_returns(series, np.nan_to_num(p, nan=0.0))
        y, x = net[idx], mkt[idx]
        reg = newey_west_alpha(y, x)
        first, second = idx[idx < idx[len(idx) // 2]], idx[idx >= idx[len(idx) // 2]]
        a1 = newey_west_alpha(net[first], mkt[first])["alpha"]
        a2 = newey_west_alpha(net[second], mkt[second])["alpha"]
        flips = float(np.sum(np.abs(np.diff(p[idx])) > 0)) / years
        scaled = vol_scaled(series, np.nan_to_num(p, nan=0.0), net)
        sidx = idx[~np.isnan(scaled[idx])]
        tests[name] = {"alpha_pct_per_year": reg["alpha"] * TRADING_DAYS * 100, "beta": reg["beta"], "t": reg["t"], "verdict": verdict(reg["t"]),
                       "se_pct_per_year": reg["se"] * TRADING_DAYS * 100, "sharpe": sharpe(y), "buy_hold_sharpe": sharpe(x),
                       "flips_per_year": flips, "first_half_alpha_pct_per_year": a1 * TRADING_DAYS * 100,
                       "second_half_alpha_pct_per_year": a2 * TRADING_DAYS * 100, "same_sign_in_both_halves": bool(a1 * a2 > 0),
                       "vol_scaled_sharpe": sharpe(scaled[sidx]) if len(sidx) > 10 else 0.0}
    se_ann = [t["se_pct_per_year"] for t in tests.values()]
    return {"periods": int(len(idx)), "first": dates[int(idx[0])], "last": dates[int(idx[-1])], "midpoint": mid, "years": years,
            "mde_alpha_pct_per_year": 2.8 * float(np.mean(se_ann)), "tests": tests}


def format_study(s: dict[str, Any]) -> str:
    L = [f"══ 慢速順勢 vs 買進持有：{s['periods']} 期（{s['first']} → {s['last']}，約 {s['years']:.1f} 年），α 的 80% 偵測力約 ±{s['mde_alpha_pct_per_year']:.1f}%/年 ══"]
    for name, t in s["tests"].items():
        L.append(f"  {name:<11}α={t['alpha_pct_per_year']:+.1f}%/年（標準誤 {t['se_pct_per_year']:.1f}）  β={t['beta']:+.2f}  t={t['t']:+.2f}  [{t['verdict']}]   "
                 f"夏普 {t['sharpe']:+.2f}（買進持有 {t['buy_hold_sharpe']:+.2f}）  每年換邊 {t['flips_per_year']:.1f} 次   "
                 f"前半 α={t['first_half_alpha_pct_per_year']:+.1f}／後半 α={t['second_half_alpha_pct_per_year']:+.1f}（{'同號' if t['same_sign_in_both_halves'] else '不同號'}）   "
                 f"波動縮放後夏普 {t['vol_scaled_sharpe']:+.2f}")
    passed = [n for n, t in s["tests"].items() if t["verdict"] == "過關"]
    hints = [n for n, t in s["tests"].items() if t["verdict"] == "線索"]
    L.append(f"\n判定（{N_TESTS} 個檢定；過關 |t| ≥ {Z_CLAIM:.2f}、線索 |t| ≥ {Z_HINT:g}）：過關 {len(passed)} 個 {passed or ''}；線索 {len(hints)} 個 {hints or ''}。"
             + ("" if passed else "沒有任何檢定過關＝這份資料上看不出慢速順勢相對買進持有的 α（偵測力有限，見上）。"))
    return "\n".join(L)


# ── CLI ───────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.trend_study", description="慢速順勢在 TX 日線上的檢驗（期交所公開資料）")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="CSV 存放位置（底下有 tx/）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    args = ap.parse_args(argv)
    d = Path(args.dir) / "tx"
    try:
        if not d.is_dir():
            raise ValueError(f"找不到 {d}，請先執行 python -m core.oi_study fetch --only tx --start-year 2003")
        res = study(load_tx_contracts(d))
    except (ValueError, OSError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 2
    print(format_study(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整結果已寫入 {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
