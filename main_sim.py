"""
模擬盤啟動入口

用法：
    python main_sim.py

與正式盤的差異：
    - SIMULATION=true（不會真實下單）
    - 使用 port 8003（可與正式盤 8002 同時運行）
    - 強制 DEV=false（避免 reload 干擾）
"""

import os

# 必須在所有 import 之前設定，確保 broker 初始化時讀到正確值
os.environ["SIMULATION"] = "true"
os.environ["DEV"] = "false"

# ── 除錯：凍結時用 `kill -USR1 <pid>` 可把所有 thread 的 Python 堆疊印到 log ──
import faulthandler  # noqa: E402
import signal        # noqa: E402

faulthandler.enable()
if hasattr(signal, "SIGUSR1"):
    faulthandler.register(signal.SIGUSR1, all_threads=True)

import uvicorn  # noqa: E402
from main import app  # noqa: E402  (app 已在 main.py 定義好)

if __name__ == "__main__":
    print("=" * 50)
    print("  ⚡ 模擬盤模式  (SIMULATION=true, port=8003)")
    print("=" * 50)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8003,
        reload=False,
    )
