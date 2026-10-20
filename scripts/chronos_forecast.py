#!/usr/bin/env python
"""用 Chronos（零樣本）預測每個決策時點之後 30 分鐘（update.md 發現 56 的實驗 1）。**在裝了 chronos-forecasting 與 torch 的環境跑**（不是交易系統的環境；torch 很大，別裝進交易用的 Python）。

輸入：data/foundation_vol_input.npz（`python -m core.foundation_vol_study export` 產生）；輸出：data/foundation_vol_forecast.npz（同一批決策位置，各 30 個預測值的總和）。
登記的設定（不可改）：模型 amazon/chronos-bolt-base、零樣本、脈絡 512、預測 30、取中位數（0.5 分位）。可中斷續跑（每批存檔）。
用法：<chronos 環境的 python> scripts/chronos_forecast.py [輸入] [輸出]"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

MODEL = "amazon/chronos-bolt-base"
BATCH = 64


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "data" / "foundation_vol_input.npz"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "data" / "foundation_vol_forecast.npz"
    import torch
    from chronos import BaseChronosPipeline
    z = np.load(src)
    a, v, pos = z["a"].astype(np.float32), z["v"].astype(np.float32), z["p"]
    ctx_len, horizon = int(z["context"]), int(z["horizon"])
    done_rv = np.full(len(pos), np.nan)
    done_vol = np.full(len(pos), np.nan)
    if dst.exists():                                                   # 續跑
        old = np.load(dst)
        if old["p"].shape == pos.shape and (old["p"] == pos).all():
            done_rv, done_vol = old["rv"].copy(), old["vol"].copy()
    pipe = BaseChronosPipeline.from_pretrained(MODEL, device_map="cpu", torch_dtype=torch.float32)
    t0 = time.time()
    todo = np.flatnonzero(~(np.isfinite(done_rv) & np.isfinite(done_vol)))
    for start in range(0, len(todo), BATCH):
        sel = todo[start:start + BATCH]
        for series, target in ((a, done_rv), (v, done_vol)):
            ctx = [torch.tensor(series[p - ctx_len:p]) for p in pos[sel]]
            q, _ = pipe.predict_quantiles(ctx, prediction_length=horizon, quantile_levels=[0.5])
            target[sel] = q[:, :, 0].sum(dim=1).numpy()
        if (start // BATCH) % 20 == 0:
            np.savez_compressed(dst, p=pos, rv=done_rv, vol=done_vol, dates=z["dates"], context=ctx_len, horizon=horizon)
            print(f"{start + len(sel)}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    np.savez_compressed(dst, p=pos, rv=done_rv, vol=done_vol, dates=z["dates"], context=ctx_len, horizon=horizon)
    print("完成", dst, f"{time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
