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

import uvicorn
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
