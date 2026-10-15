"""啟動前先確認 BIND_HOST 綁得上（update.md 發現 36）。

正式盤的後端只綁 Tailscale 的位址（BIND_HOST；不能綁 0.0.0.0，因為有真錢的下單端點）。Tailscale 斷線時那個位址會從本機消失，
uvicorn 要先跑完整個 lifespan 啟動（開 shioaji worker、登入券商…）才會發現綁不上、再全部關掉：每次失敗都白登入一次券商（有每日 1000 次的上限）。
這裡在啟動最前面用一個普通的 socket 試綁：位址不在（EADDRNOTAVAIL）就立刻結束（結束碼 3），什麼都還沒開。
其他錯誤（port 被占用等）不在這裡擋——交給 uvicorn 回報，watchdog 啟動前本來就會先清掉占用 port 的殘留進程。"""
from __future__ import annotations

import errno
import socket
import sys
from typing import Optional

EXIT_NO_ADDRESS = 3


def check_bind(host: str, port: int) -> Optional[str]:
    """試綁 host:port。回傳 None＝可以（或不是位址的問題）；回傳字串＝位址不在本機（說明文字）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return None
    except OSError as e:
        if e.errno == errno.EADDRNOTAVAIL:
            return f"{host} 這個位址不在本機上（Tailscale 斷線了嗎？）：{e}"
        return None
    finally:
        s.close()


def sim_bind_host(env: Optional[dict] = None) -> str:
    """模擬盤綁哪個位址（update.md 發現 41）：SIM_BIND_HOST → BIND_HOST（和正式盤同一個 Tailscale 位址）→ 127.0.0.1。
    **不再預設 0.0.0.0**：同網路的任何人都連得到，而後端沒有身分驗證。要對外請明確設 SIM_BIND_HOST。"""
    import os
    e = os.environ if env is None else env
    return (e.get("SIM_BIND_HOST") or "").strip() or (e.get("BIND_HOST") or "").strip() or "127.0.0.1"


def preflight(host: str, port: int) -> None:
    """位址不在就印出原因並結束（結束碼 3）；在就什麼都不做。"""
    problem = check_bind(host, port)
    if problem:
        print(f"ERROR: 無法啟動：{problem}", file=sys.stderr, flush=True)
        sys.exit(EXIT_NO_ADDRESS)
