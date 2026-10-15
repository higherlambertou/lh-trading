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
from main import app  # noqa: E402  (app 已在 main.py 定義好；它 import 時就載入了 .env)
from core.bind_check import preflight, sim_bind_host  # noqa: E402

if __name__ == "__main__":
    # 綁 SIM_BIND_HOST → BIND_HOST（Tailscale 位址）→ 127.0.0.1；不再是 0.0.0.0（後端沒有身分驗證，update.md 發現 41）
    _host = sim_bind_host()
    print("=" * 50)
    print(f"  ⚡ 模擬盤模式  (SIMULATION=true, {_host}:8003)")
    print("=" * 50)
    preflight(_host, 8003)         # 位址不在本機（Tailscale 斷線）就在這裡結束，不先開 worker 登入券商
    uvicorn.run(
        app,
        host=_host,
        port=8003,
        reload=False,
    )
