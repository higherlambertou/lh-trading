"""watchdog 腳本（run_live.sh、run_sim.sh）：語法、已知的 bash 地雷、BIND_HOST 位址檢查（update.md 發現 36）。

2026-10-14：Tailscale 位址不見，watchdog 把「其實沒壞」的後端當成凍結殺掉，重啟又綁不上位址，一路退避到 10 分鐘。
現在位址不在本機＝不判定故障、不殺後端、起不來就等位址回來。這裡測的是位址檢查的函式；整支腳本的行為用假的 ifconfig／curl／後端跑過（見 update.md 發現 36）。"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [ROOT / "run_live.sh", ROOT / "run_sim.sh"]
HAS_BASH = shutil.which("bash") is not None


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_variable_is_followed_directly_by_a_multibyte_character(script):
    """`$_old_pid）` 這種寫法：在某些 locale 下 bash 會把全形括號的位元組當成變數名的一部分，set -u 下直接 unbound variable，
    訊息印不出來（2026-10-14 在 /tmp/lh_live_watchdog.log 看到）。變數後面緊接非 ASCII 字元一律要寫成 ${var}。"""
    bad = [(i + 1, line.strip()[:100]) for i, line in enumerate(script.read_text(encoding="utf-8").splitlines()) if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line)]
    assert bad == [], bad


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def function_source(name: str, script: Path = ROOT / "run_live.sh") -> str:
    text = script.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"找不到函式 {name}"
    return m.group(0)


def stub_bin(tmp_path: Path, **tools: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    for name, body in tools.items():
        p = d / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def run_bash(snippet: str, bin_dir: Path, **env: str) -> subprocess.CompletedProcess:
    full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(bin_dir)}
    full_env.update(env)
    return subprocess.run(["bash", "-c", "set -u\n" + snippet], capture_output=True, text=True, env=full_env, timeout=20)


IFCONFIG = "\tinet 127.0.0.1 netmask 0xff000000\n\tinet 100.127.125.13 --> 100.127.125.13 netmask 0xffffffff\n"


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_iface_up_finds_the_address(tmp_path):
    b = stub_bin(tmp_path, ifconfig=f"printf '{IFCONFIG}'")
    r = run_bash(function_source("iface_up") + '\niface_up && echo UP || echo DOWN', b, BIND_HOST="100.127.125.13")
    assert r.stdout.strip() == "UP", r.stderr


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("host", ["100.127.125.1", "100.127.125.130", "100.127.12.13", "100x127x125x13", "10.127.125.13"])
def test_iface_up_does_not_confuse_similar_addresses(tmp_path, host):
    """位址檢查是整個位址比對：不是前綴（.13 vs .130）、點也不是萬用字元。"""
    b = stub_bin(tmp_path, ifconfig=f"printf '\\tinet {host} --> {host} netmask 0xffffffff\\n'")
    r = run_bash(function_source("iface_up") + '\niface_up && echo UP || echo DOWN', b, BIND_HOST="100.127.125.13")
    assert r.stdout.strip() == "DOWN", (host, r.stderr)


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_iface_up_is_down_when_the_address_is_gone(tmp_path):
    b = stub_bin(tmp_path, ifconfig="printf '\\tinet 127.0.0.1 netmask 0xff000000\\n'")
    r = run_bash(function_source("iface_up") + '\niface_up && echo UP || echo DOWN', b, BIND_HOST="100.127.125.13")
    assert r.stdout.strip() == "DOWN"


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "0.0.0.0"])
def test_iface_up_is_always_true_for_wildcard_and_loopback_hosts(tmp_path, host):
    b = stub_bin(tmp_path, ifconfig="exit 1")
    r = run_bash(function_source("iface_up") + '\niface_up && echo UP || echo DOWN', b, BIND_HOST=host)
    assert r.stdout.strip() == "UP", host


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_iface_up_falls_back_to_ip_addr_on_linux(tmp_path):
    b = stub_bin(tmp_path, ip="printf '2: tailscale0    inet 100.127.125.13/32 scope global tailscale0\\n'")           # 沒有 ifconfig，只有 ip addr
    r = run_bash(function_source("iface_up") + '\niface_up && echo UP || echo DOWN', b, BIND_HOST="100.127.125.13")
    assert r.stdout.strip() == "UP", r.stderr


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_health_state_says_noiface_without_asking_the_backend(tmp_path):
    calls = tmp_path / "curl_calls"
    b = stub_bin(tmp_path, ifconfig="printf '\\tinet 127.0.0.1 netmask 0xff000000\\n'", curl=f"echo called >> {calls}; printf '{{\"broker_connected\":\"True\"}}\\n200'")
    snippet = function_source("iface_up") + "\n" + function_source("health_state") + '\nHEALTH_URL=http://x/\nhealth_state'
    r = run_bash(snippet, b, BIND_HOST="100.127.125.13")
    assert r.stdout.strip() == "noiface" and not calls.exists()                          # 位址不在：連 curl 都不用打
    b2 = stub_bin(tmp_path / "two", ifconfig=f"printf '{IFCONFIG}'", curl="printf '{\"status\":\"ok\",\"broker_connected\":\"True\"}\\n200'") if (tmp_path / "two").mkdir() is None else None
    r2 = run_bash(snippet, b2, BIND_HOST="100.127.125.13")
    assert r2.stdout.strip() == "healthy"                                                  # 位址在：照舊看 health


def test_the_loop_treats_noiface_as_not_a_failure():
    """（靜態檢查）迴圈裡 noiface 要在『異常』判斷之前就 continue，而且啟動前要先 wait_iface；這兩段被改掉了會在這裡被抓到。"""
    t = (ROOT / "run_live.sh").read_text(encoding="utf-8")
    assert t.index('if [ "$state" = "noiface" ]') < t.index('# 異常（down=凍結 / nobroker=沒連上券商）') < t.index('fails=$((fails + 1))')
    assert t.index("    wait_iface\n    free_port") > 0 and "continue" in t[t.index('if [ "$state" = "noiface" ]'):t.index('# 異常（down=凍結')]


# ── 結束訊號要能打斷等待（2026-10-14：退避的 sleep 600 讓 pkill 最長 10 分鐘後才生效）──────────────────────────

def _bare_sleeps(script: Path) -> list[str]:
    """迴圈裡直接寫的 sleep：只允許很短的固定秒數（≤3，例如殺掉舊進程後等一下）；不固定或較長的等待要走 isleep。"""
    text = re.sub(r"^isleep\(\) \{.*?^\}", "", script.read_text(encoding="utf-8"), flags=re.S | re.M)
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*sleep\s+(\S+)", line)
        if m and not (m.group(1).isdigit() and int(m.group(1)) <= 3):
            out.append(line.strip())
    return out


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_long_or_variable_waits_go_through_isleep(script):
    """直接 `sleep "$wait_s"`（最長 600 秒）時，bash 要等前景指令跑完才執行 trap：pkill／kill 要最長 10 分鐘後才生效，
    這段時間 watchdog 還活著、單例鎖還在，重開會被擋下來。"""
    assert _bare_sleeps(script) == []
    text = script.read_text(encoding="utf-8")
    assert re.search(r"^isleep\(\) \{", text, re.M)
    assert 'kill "$SLEEP_PID"' in function_source("cleanup", script), "cleanup 要把正在睡的 sleep 一起收掉，否則會留下孤兒 sleep"
    assert re.search(r'^\s+isleep "\$wait_s"', text, re.M) and re.search(r'^\s+isleep "\$CHECK_INTERVAL"', text, re.M)


def _child_pids(pid: int) -> list[int]:
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(not HAS_BASH or shutil.which("pgrep") is None, reason="需要 bash、pgrep")
def test_premise_bash_defers_a_trap_while_a_foreground_command_runs(tmp_path):
    """整個修正的前提：前景 sleep 睡著時收到 TERM，trap 要等 sleep 跑完才執行。
    若哪天 bash 的行為變了，這個測試會失敗，提醒 isleep 可能不再需要（也讓上面的測試不是在測一個不存在的問題）。"""
    h = tmp_path / "bare.sh"
    h.write_text("trap 'echo trapped; exit 0' TERM\necho ready\nsleep 3\n")
    p = subprocess.Popen(["bash", str(h)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    time.sleep(0.3)
    t0 = time.time()
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)
    assert time.time() - t0 >= 2.0


@pytest.mark.skipif(not HAS_BASH or shutil.which("pgrep") is None, reason="需要 bash、pgrep")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_term_during_isleep_exits_promptly_and_leaves_no_sleep_behind(tmp_path, script):
    """用腳本裡真正的 isleep／cleanup（LOCK_DIR 指到暫存目錄）睡 30 秒，睡到一半送 TERM：
    要在 cleanup 自己的 2 秒內結束（沒修要等 30 秒），而且那個 sleep 子行程不能留下來。"""
    h = tmp_path / f"h_{script.name}"
    h.write_text(
        'set -u\nSLEEP_PID=""\nCHILD_PID=""\n' f'LOCK_DIR="{tmp_path}/lock.d"; mkdir -p "$LOCK_DIR"\n'
        'log() { echo "log: $*"; }\n'
        + function_source("isleep", script) + "\n" + function_source("cleanup", script) + "\n"
        'trap cleanup INT TERM\necho ready\nisleep 30\necho finished\n')
    p = subprocess.Popen(["bash", str(h)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    sleepers: list[int] = []
    try:
        assert p.stdout.readline().strip() == "ready"
        for _ in range(20):                       # 等它真的進到 sleep
            sleepers = _child_pids(p.pid)
            if sleepers:
                break
            time.sleep(0.1)
        assert sleepers, "isleep 沒有起 sleep 子行程"
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("送 TERM 後 8 秒還沒結束：isleep 沒有被打斷")
        assert time.time() - t0 < 6
        assert p.returncode == 0
        for _ in range(20):                       # 被收掉的 sleep 變成殭屍到被 init 接手回收，要一下子
            left = [x for x in sleepers if _alive(x)]
            if not left:
                break
            time.sleep(0.1)
        for x in left:                            # 孤兒會握著 stdout 管道，先收掉才不會讓下面的 read() 卡到它自己睡完
            os.kill(x, signal.SIGKILL)
        assert left == [], "cleanup 沒把 sleep 子行程收掉"
        assert "finished" not in p.stdout.read()
        assert not (tmp_path / "lock.d").exists(), "cleanup 要釋放單例鎖"
    finally:
        if p.poll() is None:
            p.kill()
        for x in sleepers:
            if _alive(x):
                os.kill(x, signal.SIGKILL)
