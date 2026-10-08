"""core/shioaji_worker.py：父進程消失時 worker 要自己登出並結束，不能變成帶著券商連線的孤兒。

背景（2026-10-08 實測）：run_live.sh 的 cleanup() 只給 main.py 2 秒正常關閉就 kill -9，worker 來不及被叫去登出，
又不會自己發現「爸爸死了」→ 變成孤兒（PPID=1），帶著券商連線活了 3 小時多、不會逾時；最近 4 次停機有 2 次漏，
把券商連線數撐到 3 條（上限 5 條，滿了新的 worker 就登不進去）。

整合測試用「假的 shioaji 模組＋真的子進程」：父進程用 os._exit() 離開——對 worker 來說跟 kill -9 一樣
（不跑 atexit、不會幫忙結束 daemon 子進程），而且測試本身不需要對任何進程送訊號。
假的 shioaji 帶一個保險：25 秒後自己結束，就算修正失效也不會留下永遠活著的進程。"""
from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.shioaji_worker as sw

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="用 ps 檢查進程是否存在；Windows 的 os.kill 會直接殺掉進程")

FAKE_SHIOAJI = '''
import os, threading, time
from types import SimpleNamespace


class _Quote:
    def set_on_quote_fop_v1_callback(self, cb):
        pass


class Shioaji:
    def __init__(self, simulation=True):
        self.quote = _Quote()
        threading.Thread(target=self._deadman, daemon=True).start()

    def _deadman(self):                      # 保險：修正失效時，worker 也不會永遠活著
        time.sleep(float(os.environ.get("FAKE_SJ_DEADMAN", "25")))
        os._exit(99)

    def login(self, **kw):
        pass

    def activate_ca(self, **kw):
        pass

    def set_order_callback(self, cb):
        pass

    def usage(self):
        return SimpleNamespace(bytes=1, limit_bytes=10, remaining_bytes=9, connections=1)

    def logout(self):
        with open(os.environ["FAKE_SJ_MARK"], "a") as f:
            f.write("logout\\n")


constant = SimpleNamespace()
'''

FAKE_DOTENV = "def load_dotenv(*a, **k):\n    return False\n"       # 不讀真實的 .env

PARENT = textwrap.dedent('''
    import multiprocessing, os, sys, time
    from core.shioaji_worker import run_worker

    if __name__ == "__main__":
        mode = sys.argv[1]
        ctx = multiprocessing.get_context("spawn")
        cmd_q, event_q = ctx.Queue(), ctx.Queue()
        p = ctx.Process(target=run_worker, args=(cmd_q, event_q), daemon=True)
        p.start()
        ev = event_q.get(timeout=30)
        assert ev["type"] == "connected", ev
        print("pid", p.pid, flush=True)
        if mode == "die":
            os._exit(0)                       # 像 kill -9：不跑 atexit，不會幫忙結束 daemon 子進程
        if mode == "idle_then_work":
            time.sleep(4)                     # 父進程活著、worker 閒置好幾個檢查週期：不能被誤判成孤兒
            print("alive_after_idle", p.is_alive(), flush=True)
            cmd_q.put({"method": "usage", "req_id": "r1"})
            r = event_q.get(timeout=10)
            print("response", r.get("type"), r.get("req_id"), flush=True)
        cmd_q.put({"type": "shutdown"})
        p.join(15)
        print("exitcode", p.exitcode, flush=True)
''')


def running(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


@pytest.fixture
def rig(tmp_path):
    (tmp_path / "shioaji.py").write_text(FAKE_SHIOAJI, encoding="utf-8")
    (tmp_path / "dotenv.py").write_text(FAKE_DOTENV, encoding="utf-8")
    (tmp_path / "parent.py").write_text(PARENT, encoding="utf-8")
    mark = tmp_path / "mark.txt"
    # 子進程只拿最小必要的環境變數。不能原樣轉交 os.environ：其他測試 import main 時 load_dotenv() 會把真實 .env
    # （券商金鑰、憑證路徑與密碼、身分證字號）載進測試進程，轉交給子進程既不乾淨，也會讓 worker 走到不同的程式路徑。
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP")}
    env.update({"PYTHONPATH": os.pathsep.join([str(tmp_path), str(ROOT)]), "FAKE_SJ_MARK": str(mark), "SIMULATION": "true"})

    def launch(mode: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(tmp_path / "parent.py"), mode], cwd=tmp_path, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    return launch, mark


def read_pid(proc: subprocess.Popen) -> int:
    line = proc.stdout.readline().split()
    assert line and line[0] == "pid", proc.stderr.read()
    return int(line[1])


def test_worker_logs_out_and_exits_when_its_parent_disappears(rig):
    launch, mark = rig
    proc = launch("die")
    pid = read_pid(proc)
    proc.wait(timeout=20)                                               # 父進程用 os._exit 離開（等同被 kill -9）
    deadline = time.time() + 12
    while running(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not running(pid), "父進程沒了，worker 還活著：會變成佔住券商連線的孤兒"
    assert mark.read_text().count("logout") == 1                         # 結束前先登出（連線才會還給券商）


def test_an_idle_worker_with_a_live_parent_is_not_mistaken_for_an_orphan(rig):
    launch, mark = rig
    proc = launch("idle_then_work")
    out, err = proc.communicate(timeout=40)
    lines = dict(l.split(maxsplit=1) for l in out.splitlines() if " " in l)
    assert lines["alive_after_idle"] == "True", err                      # 閒置 4 秒（4 個檢查週期）仍活著
    assert lines["response"] == "response r1", err                       # 之後指令照常處理
    assert lines["exitcode"].strip() == "0" and mark.read_text().count("logout") == 1


def test_normal_shutdown_path_is_unchanged(rig):
    launch, mark = rig
    out, err = launch("shutdown").communicate(timeout=40)
    assert "exitcode 0" in out, err
    assert mark.read_text().count("logout") == 1                         # 只登出一次（沒有因為多了孤兒偵測而重複）


# ── 單元 ─────────────────────────────────────────────────────────

def test_parent_alive_is_true_outside_a_multiprocessing_child():
    assert multiprocessing.parent_process() is None and sw.parent_alive() is True


def test_parent_alive_follows_the_parent_process_handle(monkeypatch):
    for alive in (True, False):
        monkeypatch.setattr(multiprocessing, "parent_process", lambda a=alive: SimpleNamespace(is_alive=lambda: a))
        assert sw.parent_alive() is alive


def test_the_child_never_receives_the_real_credentials_even_if_the_test_process_has_them(rig, monkeypatch):
    for k in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY", "CA_PATH", "CA_PASSWORD", "PERSON_ID"):
        monkeypatch.setenv(k, "should-never-reach-the-child")
    launch, mark = rig
    proc = launch("shutdown")
    out, err = proc.communicate(timeout=40)
    assert "exitcode 0" in out, err                                      # 連線成功＝沒有走到需要憑證的路徑
