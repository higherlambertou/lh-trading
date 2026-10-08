"""破產機率驗證（賭徒破產問題）：本金撐不撐得住「期望值為正」的那一天。

對應《破產機率驗證_需求文件.md》。期望值回答「平均而言划不划算」，破產機率回答
「我有沒有足夠的本金，撐到那個平均數字真正顯現的那一天」。

三種估法（互相對照）：
  1. 公式：賺賠金額對稱時 P(破產) = (q/p)^i，i = 本金 ÷ 單筆虧損（文件的公式；無限期）
  2. Lundberg 上界：賺賠不對稱時，無限期破產機率 ≤ exp(-R·可虧本金)，R 解 p·e^{-R·a} + q·e^{R·b} = 1
     （a=單筆獲利、b=單筆虧損；期望值 ≤ 0 時沒有解＝遲早破產，回傳 1）
  3. 蒙地卡羅：用勝率＋獲利/虧損金額（或歷史每筆損益 bootstrap）模擬 n 筆交易，統計「途中本金 ≤ 破產線」的比例
     ——有限筆數內的破產機率，最貼近實況（文件也建議以模擬取代公式）

純 numpy、無 I/O。單位一律是「元」（呼叫端自行把點數 × 點值 × 口數換成元）。
CLI：python -m core.ruin --capital 51482 --tp 20 --sl 60 --win 0.65 --cost-pts 2
"""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

DEFAULT_WIN_RATES = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85)
CAPITAL_MULTIPLIERS = (0.5, 1.0, 2.0)          # 文件：目前本金、減半、加倍


# ── 公式 ──────────────────────────────────────────────────────────

def symmetric_ruin(p: float, units: float) -> float:
    """賺賠金額相等時的無限期破產機率：(q/p)^i。p<=0.5 → 1（遲早破產）。units = 本金 ÷ 單筆虧損。"""
    if p >= 1.0:
        return 0.0
    if p <= 0.5:
        return 1.0
    return ((1.0 - p) / p) ** units


def breakeven_win_rate(win: float, loss: float, cost: float = 0.0) -> float:
    """期望值 = 0 的勝率：p·win − (1−p)·loss − cost = 0 → p = (loss + cost) / (win + loss)。"""
    return (loss + cost) / (win + loss)


def expectancy(p: float, win: float, loss: float, cost: float = 0.0) -> float:
    return p * win - (1.0 - p) * loss - cost


def lundberg_bound(p: float, win: float, loss: float, capital_at_risk: float) -> float:
    """無限期破產機率的上界 exp(-R·capital_at_risk)。期望值 <= 0 → 1.0（遲早破產）。"""
    q = 1.0 - p
    if capital_at_risk <= 0 or p * win - q * loss <= 0:
        return 1.0
    if q <= 0:
        return 0.0

    def f(r: float) -> float:
        return p * math.exp(-r * win) + q * math.exp(r * loss) - 1.0

    hi = 1.0 / max(win, loss)
    cap = 700.0 / loss                                  # 避免 exp 溢位
    while f(hi) < 0 and hi < cap:
        hi *= 2.0
    if f(hi) < 0:
        return 0.0
    lo = 1e-12
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return math.exp(-0.5 * (lo + hi) * capital_at_risk)


# ── 蒙地卡羅 ──────────────────────────────────────────────────────

def _run_paths(draw, capital: float, n_trades: int, n_paths: int, ruin_level: float) -> dict[str, Any]:
    """共用的路徑模擬：draw(m, n_trades) → (m, n_trades) 的每筆損益矩陣。分塊避免吃光記憶體。"""
    ruined = 0
    first_ruin: list[int] = []
    dd: list[np.ndarray] = []
    survivors_end: list[np.ndarray] = []
    chunk = max(1, 4_000_000 // max(n_trades, 1))
    for start in range(0, n_paths, chunk):
        m = min(chunk, n_paths - start)
        eq = capital + np.cumsum(draw(m, n_trades), axis=1)
        hit = eq <= ruin_level
        r = hit.any(axis=1)
        ruined += int(r.sum())
        first_ruin += list(hit.argmax(axis=1)[r] + 1)
        peak = np.maximum(np.maximum.accumulate(eq, axis=1), capital)
        dd.append((peak - eq).max(axis=1))
        survivors_end.append(eq[~r, -1])
    ends = np.concatenate(survivors_end) if survivors_end else np.array([])
    return {
        "ruin_prob": ruined / n_paths,
        "n_trades": n_trades,
        "n_paths": n_paths,
        "median_first_ruin_trade": int(np.median(first_ruin)) if first_ruin else None,
        "drawdown_p95": float(np.quantile(np.concatenate(dd), 0.95)),
        "survivor_median_end": float(np.median(ends)) if ends.size else None,
    }


def simulate_ruin(capital: float, win_prob: float, win: float, loss: float, *, n_trades: int = 1000,
                  n_paths: int = 4000, ruin_level: float = 0.0, cost: float = 0.0, seed: int = 7) -> dict[str, Any]:
    """參數式：每筆以 win_prob 賺 win、否則賠 loss（再扣 cost），模擬 n_trades 筆，途中本金 <= ruin_level 即破產。"""
    rng = np.random.default_rng(seed)

    def draw(m: int, n: int) -> np.ndarray:
        return np.where(rng.random((m, n)) < win_prob, win, -loss) - cost

    return _run_paths(draw, capital, n_trades, n_paths, ruin_level)


def bootstrap_ruin(capital: float, pnls: Sequence[float], *, n_trades: int = 1000, n_paths: int = 4000,
                   ruin_level: float = 0.0, seed: int = 7) -> dict[str, Any]:
    """歷史式：從實際每筆損益（元）有放回抽樣，保留真實的損益分布（不假設只有一種賺法、一種賠法）。"""
    arr = np.asarray(list(pnls), dtype=float)
    rng = np.random.default_rng(seed)

    def draw(m: int, n: int) -> np.ndarray:
        return rng.choice(arr, size=(m, n), replace=True)

    return _run_paths(draw, capital, n_trades, n_paths, ruin_level)


# ── 連續虧損 ──────────────────────────────────────────────────────

def losing_streak(pnls: Sequence[float]) -> tuple[int, float]:
    """歷史上最長的連續虧損：(筆數, 該段累計虧損金額（負數）)。"""
    best_n, best_sum, n, s = 0, 0.0, 0, 0.0
    for x in pnls:
        if x < 0:
            n, s = n + 1, s + x
            if n > best_n or (n == best_n and s < best_sum):
                best_n, best_sum = n, s
        else:
            n, s = 0, 0.0
    return best_n, best_sum


def expected_longest_losing_streak(win_rate: float, n_trades: int) -> float:
    """n 筆交易中預期的最長連續虧損筆數：ln(n·p)/ln(1/q) + γ/ln(1/q) − 1/2（q = 虧損機率，γ = 歐拉常數）。
    以模擬驗證（勝率 50~80%、500~2000 筆）誤差 < 0.1 筆。"""
    q = 1.0 - win_rate
    if q <= 0 or n_trades <= 0:
        return 0.0
    if q >= 1:
        return float(n_trades)
    ln_inv_q = math.log(1.0 / q)
    return max(1.0, math.log(max(n_trades * (1 - q), 1.0)) / ln_inv_q + 0.5772156649 / ln_inv_q - 0.5)


def history_stats(pnls: Sequence[float]) -> dict[str, Any] | None:
    """歷史每筆損益（元）→ 勝率、平均賺賠、賺賠比、最長連虧（文件第一、三步）。"""
    arr = np.asarray(list(pnls), dtype=float)
    if arr.size == 0:
        return None
    wins, losses = arr[arr > 0], arr[arr < 0]
    n_streak, streak_loss = losing_streak(arr)
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(-losses.mean()) if losses.size else 0.0
    return {
        "n": int(arr.size), "win_rate": round(float((arr > 0).mean()), 4),
        "avg_win": round(avg_win, 1), "avg_loss": round(avg_loss, 1),
        "payoff": round(avg_win / avg_loss, 2) if avg_loss else None,
        "expectancy": round(float(arr.mean()), 1), "total": round(float(arr.sum()), 1),
        "worst_trade": round(float(arr.min()), 1),
        "longest_losing_streak": n_streak, "longest_losing_streak_loss": round(streak_loss, 1),
    }


# ── 整合報告（API / CLI 共用）──────────────────────────────────────

def build_report(capital: float, win_prob: float, win: float, loss: float, *, cost: float = 0.0,
                 n_trades: int = 1000, n_paths: int = 4000, ruin_level: float = 0.0, seed: int = 7,
                 win_rates: Sequence[float] = DEFAULT_WIN_RATES, pnls: Sequence[float] | None = None) -> dict[str, Any]:
    """金額單位：元。pnls 給定（≥1 筆）時，主要三格改用 bootstrap（真實損益分布），敏感度表仍用參數式。"""
    at_risk = capital - ruin_level
    ruin: dict[str, Any] = {}
    for mult in CAPITAL_MULTIPLIERS:
        c = capital * mult
        if pnls:
            mc = bootstrap_ruin(c, pnls, n_trades=n_trades, n_paths=n_paths, ruin_level=ruin_level, seed=seed)
        else:
            mc = simulate_ruin(c, win_prob, win, loss, n_trades=n_trades, n_paths=n_paths,
                               ruin_level=ruin_level, cost=cost, seed=seed)
        eff_loss = loss + cost                                     # 公式/上界把每筆成本併入虧損與獲利
        eff_win = win - cost
        ruin[f"{mult:g}x"] = {
            "capital": round(c, 1), **mc,
            "formula": round(symmetric_ruin(win_prob, (c - ruin_level) / eff_loss), 4)
            if abs(win - loss) < 1e-9 and cost == 0 else None,
            "lundberg": round(lundberg_bound(win_prob, eff_win, eff_loss, c - ruin_level), 4) if eff_win > 0 else 1.0,
        }
    grid = []
    for wr in win_rates:
        row: dict[str, Any] = {"win_rate": wr, "expectancy": round(expectancy(wr, win, loss, cost), 1)}
        for mult in CAPITAL_MULTIPLIERS:
            row[f"{mult:g}x"] = simulate_ruin(
                capital * mult, wr, win, loss, n_trades=n_trades, n_paths=max(1000, n_paths // 2),
                ruin_level=ruin_level, cost=cost, seed=seed)["ruin_prob"]
        grid.append(row)
    return {
        "inputs": {"capital": capital, "win_rate": win_prob, "win": win, "loss": loss, "cost": cost,
                   "n_trades": n_trades, "n_paths": n_paths, "ruin_level": ruin_level,
                   "source": "history" if pnls else "parameters"},
        "per_trade": {
            "win": win, "loss": loss, "cost": cost,
            "payoff": round(win / loss, 3) if loss else None,
            "breakeven_win_rate": round(breakeven_win_rate(win, loss, cost), 4),
            "expectancy": round(expectancy(win_prob, win, loss, cost), 1),
            "expectancy_pct_of_capital": round(100 * expectancy(win_prob, win, loss, cost) / capital, 3) if capital else None,
        },
        "ruin": ruin,
        "capacity": {
            "affordable_losses": int(max(at_risk, 0) // (loss + cost)) if loss + cost > 0 else None,
            "expected_longest_losing_streak": round(expected_longest_losing_streak(win_prob, n_trades), 1),
        },
        "grid": grid,
    }


def _main() -> None:  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="破產機率驗證（賭徒破產問題）")
    ap.add_argument("--capital", type=float, required=True, help="本金（元）")
    ap.add_argument("--tp", type=float, default=20, help="停利點數")
    ap.add_argument("--sl", type=float, default=60, help="停損點數")
    ap.add_argument("--qty", type=int, default=1, help="口數")
    ap.add_argument("--point-value", type=float, default=10, help="每點元數（TMF=10, MXF=50, TXF=200）")
    ap.add_argument("--win", type=float, default=0.65, help="勝率 0~1")
    ap.add_argument("--cost-pts", type=float, default=0, help="每筆成本（手續費+稅+滑價，點）")
    ap.add_argument("--trades", type=int, default=1000)
    ap.add_argument("--paths", type=int, default=4000)
    ap.add_argument("--ruin-level", type=float, default=0, help="破產線（元），預設 0")
    a = ap.parse_args()
    k = a.point_value * a.qty
    r = build_report(a.capital, a.win, a.tp * k, a.sl * k, cost=a.cost_pts * k, n_trades=a.trades,
                     n_paths=a.paths, ruin_level=a.ruin_level)
    pt = r["per_trade"]
    print(f"每筆：賺 {pt['win']:.0f} 元／賠 {pt['loss']:.0f} 元（賺賠比 {pt['payoff']}），成本 {pt['cost']:.0f} 元")
    print(f"損益兩平勝率 {pt['breakeven_win_rate']:.1%}；勝率 {a.win:.0%} 時每筆期望 {pt['expectancy']:+.0f} 元")
    print(f"撐得住連續 {r['capacity']['affordable_losses']} 筆最大虧損；{a.trades} 筆內預期最長連虧 {r['capacity']['expected_longest_losing_streak']} 筆")
    print(f"\n{a.trades} 筆內破產機率（破產線 {a.ruin_level:.0f} 元）：")
    for k_, v in r["ruin"].items():
        print(f"  本金 ×{k_[:-1]:<4s}({v['capital']:>9.0f} 元)  模擬 {v['ruin_prob']:>6.1%}  無限期上界 {v['lundberg']:>6.1%}"
              + (f"  公式 {v['formula']:.1%}" if v["formula"] is not None else ""))
    print("\n勝率 × 本金 敏感度（破產機率）：")
    print("  勝率    期望/筆     ×0.5     ×1      ×2")
    for g in r["grid"]:
        print(f"  {g['win_rate']:.0%}   {g['expectancy']:>+8.0f}  {g['0.5x']:>6.1%} {g['1x']:>6.1%} {g['2x']:>6.1%}")


if __name__ == "__main__":  # pragma: no cover
    _main()
