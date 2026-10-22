"""夜盤預期振幅（update.md 發現 67）：只顯示，策略、風控、下單都不用它。

模型＝發現 67 兩項都過關的那個：ln(夜盤高低差) ~ 1 ＋ ln(前 20 個夜盤平均高低差) ＋ x_iv(前一個交易日) ＋ ln(前一個日盤高低差)，
全期擬合、68% 區間用殘差的 16／84% 分位數（同發現 55 的做法）。資料的定義直接用研究的 load_tx／build（只有一份）。
擬合在**獨立子行程**做（python -m core.night_range fit；core/taifex_update 每天期交所資料更新後執行），結果存 data/night_model.json；
後端只讀這個檔＋即時的輸入：前一個交易日收盤的臺指選擇權波動率指數（lh-intel，約 16:15 抓到）× 0.93、前一個日盤的高低差與收盤（bars_1m）。
哪個夜盤：13:45 以後＝今晚（前一個交易日＝今天）；05:00 以前＝正在進行的那個（前一個交易日＝昨天）；週末沒有。輸入不齊就回 {"note": 原因}，不湊數字。"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

from core.daytrade import TAIPEI
from core.iv_day_range import RATIO               # 官方臺指選擇權波動率指數 → ATM IV（發現 54；日盤與夜盤用同一個換算）

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "data" / "night_model.json"
TX_DIR = ROOT / "data" / "taifex" / "tx"
IV_DIR = ROOT / "data" / "taifex" / "vrp"
N_REF = 20
KEEP_RECENT = 40


def fit_model(tx_dir: Path = TX_DIR, iv_dir: Path = IV_DIR) -> dict[str, Any]:
    """（CPU 工作，在子行程裡跑）全期擬合，回傳要存進 data/night_model.json 的 dict。"""
    from core.night_range_study import build, load_tx
    from core.vrp_study import load_iv
    day, night = load_tx(tx_dir)
    b = build(day, night, load_iv(iv_dir))
    ok = np.isfinite(b["y"]) & np.isfinite(b["lnR"]) & np.isfinite(b["x_iv"]) & np.isfinite(b["ln_day"])
    X = np.column_stack([np.ones(int(ok.sum())), b["lnR"][ok], b["x_iv"][ok], b["ln_day"][ok]])
    y = b["y"][ok]
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    res = y - X @ beta
    labels = sorted(night)
    return {"fitted_at": time.time(), "terms": ["const", "lnR", "x_iv", "ln_day"], "beta": [float(x) for x in beta],
            "q16": float(np.quantile(res, 0.16)), "q50": float(np.quantile(res, 0.50)), "q84": float(np.quantile(res, 0.84)), "n": int(len(y)),
            "data_from": str(b["dates"][ok][0]), "data_to": str(b["dates"][ok][-1]),
            "recent_nights": [[d, round(night[d][0] - night[d][1], 1)] for d in labels[-KEEP_RECENT:]]}


_cache: dict[str, Any] = {"mtime": None, "model": None}


def load_model(path: Path = MODEL_PATH) -> Optional[dict[str, Any]]:
    """讀 data/night_model.json（檔案沒變就用記憶體裡的）；沒有或壞掉回 None。"""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] != mt:
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            ok = isinstance(m, dict) and len(m.get("beta") or []) == 4 and all(k in m for k in ("q16", "q50", "q84", "recent_nights"))
            _cache.update(mtime=mt, model=m if ok else None)
        except (OSError, ValueError):
            _cache.update(mtime=mt, model=None)
    return _cache["model"]


def prev_trading_day(now: datetime) -> Optional[str]:
    """要估的夜盤之前那個交易日：13:45 以後＝今天、05:00 以前＝昨天、其他時段（日盤中、05:00～13:45）＝None；週末沒有夜盤（None）。
    不看休市日曆：休市日沒有日盤資料，for_tonight 會回「還沒有那天的日盤」。"""
    now = now.astimezone(TAIPEI)
    m = now.hour * 60 + now.minute
    if m >= 13 * 60 + 45:
        d = now.date()
    elif m < 5 * 60:
        d = now.date() - timedelta(days=1)
    else:
        return None
    return d.isoformat() if d.weekday() < 5 else None


def for_tonight(model: Optional[dict[str, Any]], prev_day: str, day_range: Optional[float], close: Optional[float],
                vix: Optional[tuple[str, float]]) -> dict[str, Any]:
    """畫面用：{"expect": {lo, mid, hi}, "r20n", "vix", "vix_date", "day_range", "prev_day", "n"} 或 {"note": 原因}。"""
    if model is None:
        return {"note": "夜盤模型還沒算好（期交所資料每天 15:10 後更新，之後自動擬合）"}
    if not day_range or day_range <= 0 or not close or close <= 0:
        return {"note": f"還沒有 {prev_day} 的日盤資料"}
    if vix is None or vix[0] != prev_day or not vix[1] or vix[1] <= 0:
        return {"note": f"等 {prev_day[5:]} 收盤的臺指選擇權波動率指數（lh-intel 約 16:15 抓到）"}
    recent = [r for d, r in model["recent_nights"] if d <= prev_day][-N_REF:]
    if len(recent) < N_REF or not all(r > 0 for r in recent):
        return {"note": "最近 20 個夜盤的資料不完整"}
    r20 = float(np.mean(recent))
    b0, b1, b2, b3 = model["beta"]
    mu = b0 + b1 * math.log(r20) + b2 * math.log(vix[1] * RATIO / 100.0 * close) + b3 * math.log(day_range)
    expect = {k: round(math.exp(mu + model[q]), 1) for k, q in (("lo", "q16"), ("mid", "q50"), ("hi", "q84"))}
    return {"expect": expect, "r20n": round(r20, 1), "vix": round(vix[1], 2), "vix_date": vix[0], "day_range": round(day_range, 1),
            "prev_day": prev_day, "n": int(model.get("n") or 0)}


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] != "fit":
        print(__doc__)
        return 2
    m = fit_model()
    tmp = MODEL_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MODEL_PATH)
    print(f"夜盤模型已更新：n={m['n']}（{m['data_from']}～{m['data_to']}），係數 {', '.join(f'{x:.3f}' for x in m['beta'])}，"
          f"殘差 16／84% {m['q16']:.3f}／{m['q84']:.3f}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
