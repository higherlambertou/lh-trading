"""launchd 用的啟動器（update.md 發現 69）。用法：python scripts/launchd_run.py run_live.sh

macOS 的隱私保護不讓 launchd 直接用 /bin/bash 讀 ~/Documents（Operation not permitted），但 anaconda 的 Python 已經有這個權限
（lh-intel 的 launchd 服務就是用它）。所以由這支 Python 當「負責的行程」再開 bash 跑腳本——子行程沿用它的權限。
收到的結束訊號轉給 bash，並用 bash 的結束碼結束：launchd 才分得出正常結束（不重啟）和異常結束（重啟）。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or "/" in argv[1] or not argv[1].endswith(".sh"):
        print("用法：python scripts/launchd_run.py run_live.sh", file=sys.stderr)
        return 2
    p = subprocess.Popen(["/bin/bash", os.path.join(ROOT, argv[1])], cwd=ROOT)

    def forward(sig: int, _frame: object) -> None:
        try:
            p.send_signal(sig)
        except ProcessLookupError:
            pass
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, forward)
    rc = p.wait()
    return rc if rc >= 0 else 128 + (-rc)                  # 被訊號殺掉（例如 kill -9）＝異常結束


if __name__ == "__main__":
    sys.exit(main(sys.argv))
