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


@pytest.mark.parametrize("script", SCRIPTS + [ROOT / "run_frontend.sh", ROOT / "scripts" / "launchd.sh"], ids=lambda p: p.name)
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
    assert t.index('if [ "$state" = "noiface" ]') < t.index('# 異常（down=凍結 / nobroker=沒連上券商 / stale=連線假死）') < t.index('fails=$((fails + 1))')
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


# ── 換月重啟（update.md 發現 38）：後端在 health 帶 restart_wanted=rollover，watchdog 正常關閉並重啟 ──────────

@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("body,expect", [
    ('{"status":"ok","broker_connected":"True"}', "healthy"),
    ('{"status":"ok","broker_connected":"True","restart_wanted":"rollover"}', "rollover"),
    ('{"status":"ok","broker_connected":"False","restart_wanted":"rollover"}', "nobroker"),        # 沒連上券商：先當斷線處理，不是換月
    ('{"status":"ok","broker_connected":"True","restart_wanted":"something_else"}', "healthy"),
    ('{"status":"ok","broker_connected":"False","session":"stale","stale_reason":"日盤中 200 秒沒有收到任何報價"}', "stale"),   # 連線假死（發現 65）
    ('{"status":"ok","broker_connected":"False"}', "nobroker"),
])
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_health_state_understands_the_rollover_flag(tmp_path, script, body, expect):
    bin_dir = stub_bin(tmp_path, curl=f"printf '%s\\n200' '{body}'", ifconfig=IFCONFIG_CMD)
    fn = function_source("health_state", script)
    r = run_bash(f'HEALTH_URL=http://x\nBIND_HOST=100.127.125.13\n{function_source("iface_up")}\n{fn}\nhealth_state', bin_dir)
    assert r.stdout.strip() == expect, (r.stdout, r.stderr)


IFCONFIG_CMD = "printf '\\tinet 100.127.125.13 --> 100.127.125.13 netmask 0xffffffff\\n'"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_rollover_restart_is_planned_not_a_failure(script):
    text = script.read_text(encoding="utf-8")
    i = text.index('if [ "$state" = "rollover" ]; then')
    assert i < text.index('if [ "$state" = "healthy" ]; then')                    # 先處理換月旗標，再走一般健康分支
    branch = text[i:text.index('if [ "$state" = "healthy" ]; then')]
    assert 'ROLLOVER_MIN_UPTIME' in branch and 'ROLLOVER_MIN_GAP' in branch and 'planned=1' in branch
    assert 'kill "$CHILD_PID"' in branch and 'kill -9 "$CHILD_PID"' in branch      # 先正常關閉（worker 登出），逾時才 -9
    assert re.search(r'if \[ "\$planned" -eq 1 \]; then\s+consec_short=0', text)    # 有計畫的重啟不算短命、不退避


def _wait(cond, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.mark.skipif(not HAS_BASH or shutil.which("pgrep") is None, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_rollover_flag_restarts_the_backend_once_without_backoff(tmp_path, script):
    """整支腳本（常數縮短、路徑換成暫存、假的 curl／後端）：健康 → 後端要求換月 → watchdog 正常關閉並重啟一次；
    旗標一直留著也不會連續重啟（最少間隔），而且不算短命重啟（沒有退避）。"""
    work, sim = tmp_path / "work", tmp_path / "sim"
    (sim / "bin").mkdir(parents=True)
    work.mkdir()
    for name, body in {
        "ifconfig": IFCONFIG_CMD,
        "lsof": "exit 0",
        "curl": 'printf \'%s\\n200\' "$(cat "$SIM/health.body")"',
        "stub_py": 'if [ "$1" = "main.py" ] || [ "$1" = "main_sim.py" ]; then echo "$$" >> "$SIM/backend_pids"; exec sleep 600; fi\nexit 0',
    }.items():
        p = sim / "bin" / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    stub = str(sim / "bin" / "stub_py")
    text = script.read_text(encoding="utf-8")
    for pat, repl in [
        (r"(?m)^PY=.*$", f"PY={stub}"), (r"(?m)^LOCK_DIR=.*$", f'LOCK_DIR="{sim}/lock.d"'), (r"(?m)^APP_LOG=.*$", f'APP_LOG="{sim}/app.log"'),
        (r"(?m)^PORT=\d+$", "PORT=18999"), (r"(?m)^CHECK_INTERVAL=\d+", "CHECK_INTERVAL=1"), (r"(?m)^GRACE_PERIOD=\d+", "GRACE_PERIOD=3"),
        (r"(?m)^ROLLOVER_MIN_UPTIME=\d+", "ROLLOVER_MIN_UPTIME=2"), (r"(?m)^ROLLOVER_MIN_GAP=\d+", "ROLLOVER_MIN_GAP=60"),
        (r"/Users/lambert/anaconda3/bin/python3\.11", stub),
    ]:
        text = re.sub(pat, repl, text)
    (work / "wd.sh").write_text(text)
    (work / ".env").write_text("BIND_HOST=100.127.125.13\n")
    (work / "main.py").write_text("")
    (work / "main_sim.py").write_text("")
    (sim / "health.body").write_text('{"status":"ok","broker_connected":"True"}')
    env = {**os.environ, "SIM": str(sim), "PATH": f"{sim / 'bin'}:{os.environ['PATH']}", "NOTIFY_SKIP_DOTENV": "1"}
    log = open(sim / "wd.out", "w")
    p = subprocess.Popen(["bash", "wd.sh"], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pids = lambda: [int(x) for x in (sim / "backend_pids").read_text().split()] if (sim / "backend_pids").exists() else []  # noqa: E731
    try:
        assert _wait(lambda: len(pids()) == 1, 15), "後端沒啟動"
        first = pids()[0]
        assert _wait(lambda: "進入正常監看" in (sim / "wd.out").read_text(), 15), "沒進入正常監看"
        time.sleep(3)                                                        # 超過 ROLLOVER_MIN_UPTIME（2 秒）
        (sim / "health.body").write_text('{"status":"ok","broker_connected":"True","restart_wanted":"rollover"}')
        assert _wait(lambda: len(pids()) == 2, 20), "沒有因為換月旗標重啟"
        assert not _alive(first), "舊的後端沒被關掉"
        time.sleep(4)                                                        # 旗標還在、新後端也過了最少存活時間：最少間隔（60 秒）要擋住
        assert len(pids()) == 2, "旗標一直在就連續重啟了"
        out = (sim / "wd.out").read_text()
        assert out.count("換月：後端要求重啟") == 1
        assert "退避等待" not in out and "連續第" not in out, out
    finally:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        p.wait(timeout=15)
        log.close()
        for x in pids():
            if _alive(x):
                os.kill(x, signal.SIGKILL)


# ── 連線假死（update.md 發現 65）：後端 health 帶 session=stale，watchdog 正常關閉並重啟；算短命重啟（會退避）──────────

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_stale_branch_stops_gracefully(script):
    """（靜態檢查）stale 在故障門檻之後、kill -9 的一般分支之前處理：先 kill（正常關閉）、等最多 10 秒、才 -9；不是有計畫的重啟（不設 planned）。"""
    text = script.read_text(encoding="utf-8")
    i = text.index('if [ "$state" = "stale" ]; then')
    assert text.index('if [ "$fails" -ge "$FAIL_THRESHOLD" ]; then') < i < text.index('log "判定故障(${state}) → kill -9')
    branch = text[i:text.index('log "判定故障(${state}) → kill -9')]
    assert branch.index('kill "$CHILD_PID"') < branch.index('kill -9 "$CHILD_PID"') and "break" in branch and "planned=1" not in branch
    if script.name == "run_live.sh":
        assert "notify_trouble freeze stale" in branch and "notify_stale" in branch
        fn = function_source("notify_stale", script)
        assert "core.notify send" in fn and '--key "watchdog:stale:live"' in fn and "--cooldown 1800" in fn and "& )" in fn      # 背景送、不拖慢重啟


@pytest.mark.skipif(not HAS_BASH or shutil.which("pgrep") is None, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_stale_restarts_the_backend(tmp_path, script):
    """整支腳本（常數縮短、假的 curl／後端）：健康 → 後端說連線假死 → 連續 2 次後正常關閉並重啟；正式盤會推播（背景）；
    這次重啟算短命（之後會退避），保護登入次數。"""
    work, sim = tmp_path / "work", tmp_path / "sim"
    (sim / "bin").mkdir(parents=True)
    work.mkdir()
    for name, body in {
        "ifconfig": IFCONFIG_CMD,
        "lsof": "exit 0",
        "curl": 'printf \'%s\\n200\' "$(cat "$SIM/health.body")"',
        "stub_py": 'if [ "$1" = "main.py" ] || [ "$1" = "main_sim.py" ]; then echo "$$" >> "$SIM/backend_pids"; exec sleep 600; fi\necho "$@" >> "$SIM/py_calls"\nexit 0',
    }.items():
        p = sim / "bin" / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    stub = str(sim / "bin" / "stub_py")
    text = script.read_text(encoding="utf-8")
    for pat, repl in [
        (r"(?m)^PY=.*$", f"PY={stub}"), (r"(?m)^LOCK_DIR=.*$", f'LOCK_DIR="{sim}/lock.d"'), (r"(?m)^APP_LOG=.*$", f'APP_LOG="{sim}/app.log"'),
        (r"(?m)^PORT=\d+$", "PORT=18998"), (r"(?m)^CHECK_INTERVAL=\d+", "CHECK_INTERVAL=1"), (r"(?m)^GRACE_PERIOD=\d+", "GRACE_PERIOD=3"),
        (r"(?m)^BACKOFF_STEPS=.*$", "BACKOFF_STEPS=(0 1 1 1)"), (r"/Users/lambert/anaconda3/bin/python3\.11", stub),
    ]:
        text = re.sub(pat, repl, text)
    (work / "wd.sh").write_text(text)
    (work / ".env").write_text("BIND_HOST=100.127.125.13\n")
    (work / "main.py").write_text("")
    (work / "main_sim.py").write_text("")
    (sim / "health.body").write_text('{"status":"ok","broker_connected":"True"}')
    env = {**os.environ, "SIM": str(sim), "PATH": f"{sim / 'bin'}:{os.environ['PATH']}", "NOTIFY_SKIP_DOTENV": "1"}
    log = open(sim / "wd.out", "w")
    p = subprocess.Popen(["bash", "wd.sh"], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pids = lambda: [int(x) for x in (sim / "backend_pids").read_text().split()] if (sim / "backend_pids").exists() else []  # noqa: E731
    try:
        assert _wait(lambda: len(pids()) == 1, 15), "後端沒啟動"
        first = pids()[0]
        assert _wait(lambda: "進入正常監看" in (sim / "wd.out").read_text(), 15), "沒進入正常監看"
        (sim / "health.body").write_text('{"status":"ok","broker_connected":"False","session":"stale","stale_reason":"日盤中 200 秒沒有收到任何報價"}')
        assert _wait(lambda: len(pids()) == 2, 20), "連線假死沒有重啟"
        assert not _alive(first), "舊的後端沒被關掉"
        (sim / "health.body").write_text('{"status":"ok","broker_connected":"True"}')
        out = (sim / "wd.out").read_text()
        assert "異常 state=stale（連續 1/2" in out and "判定券商連線假死(stale) → 正常關閉" in out and "判定故障(stale)" not in out
        assert "連續第 1 次短命重啟" in out, out                                     # 不是有計畫的重啟：會退避
        if script.name == "run_live.sh":
            assert _wait(lambda: (sim / "py_calls").exists() and "core.notify send" in (sim / "py_calls").read_text(), 5)
            calls = (sim / "py_calls").read_text()
            assert "watchdog --event freeze --detail stale --mode live" in calls and "watchdog:stale:live" in calls
        else:
            assert not (sim / "py_calls").exists() or "core.notify" not in (sim / "py_calls").read_text()      # 模擬盤不推播
    finally:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        p.wait(timeout=15)
        log.close()
        for x in pids():
            if _alive(x):
                os.kill(x, signal.SIGKILL)


# ── 新版 SDK 的獨立環境與 log 改名（update.md 發現 68）──────────────────────────

@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_python_is_the_venv_when_present(tmp_path, script):
    text = script.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.startswith("PY=") or l.startswith('[ -x "$PWD/.venv/bin/python" ]')]
    assert len(lines) == 2, lines
    snippet = "\n".join(lines) + '\necho "$PY"'
    r = subprocess.run(["bash", "-c", "set -u\n" + snippet], capture_output=True, text=True, cwd=tmp_path)
    assert r.stdout.strip() == "/Users/lambert/anaconda3/bin/python3.11"                # 沒有 .venv：用 anaconda（舊版）
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    py = tmp_path / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    r = subprocess.run(["bash", "-c", "set -u\n" + snippet], capture_output=True, text=True, cwd=tmp_path)
    assert r.stdout.strip() == str(py.resolve()) or r.stdout.strip().endswith("/.venv/bin/python")
    assert '"$PY" main' in text                                                          # 啟動後端用的是 $PY，不是寫死的路徑


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_rotate_app_log(tmp_path, script):
    log_fn = 'log() { echo "LOG: $*"; }'
    fn = function_source("rotate_app_log", script)
    app = tmp_path / "app.log"
    app.write_bytes(b"x" * (2 * 1024 * 1024))
    r = run_bash(f'{log_fn}\n{fn}\nAPP_LOG="{app}"\nLOG_MAX_MB=1\nrotate_app_log', stub_bin(tmp_path, ifconfig="exit 0"))
    assert not app.exists() and (tmp_path / "app.log.1").stat().st_size == 2 * 1024 * 1024 and "改名成" in r.stdout
    app.write_bytes(b"y" * 1000)
    r = run_bash(f'{log_fn}\n{fn}\nAPP_LOG="{app}"\nLOG_MAX_MB=1\nrotate_app_log', tmp_path / "bin")
    assert app.exists() and r.stdout == ""                                               # 沒超過：不動
    text = script.read_text(encoding="utf-8")
    i = text.index("    rotate_app_log\n")
    assert text.index("    free_port\n") < i < text.index('"$PY" main')                 # 舊的停掉、清完 port 之後，啟動新的之前


# ── 登入自動啟動（launchd，update.md 發現 69）──────────────────────────────

@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_second_watchdog_exits_cleanly(tmp_path, script):
    """已經有一個在跑：要正常結束（exit 0），launchd（只在異常結束時重啟）才不會一直重試。"""
    lock = tmp_path / "lock.d"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))                                      # 一個活著的行程佔著鎖
    text = re.sub(r'(?m)^LOCK_DIR=.*$', f'LOCK_DIR="{lock}"', script.read_text(encoding="utf-8"))
    (tmp_path / "wd.sh").write_text(text)
    r = subprocess.run(["bash", str(tmp_path / "wd.sh")], capture_output=True, text=True, cwd=tmp_path, timeout=20)
    assert r.returncode == 0 and "本次拒絕啟動" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert (lock / "pid").read_text() == str(os.getpid())                              # 不會搶走別人的鎖


def test_launchd_plists():
    import plistlib
    live = plistlib.loads((ROOT / "deploy/launchd/com.lh.trading.live.plist").read_bytes())
    launcher = ["/Users/lambert/anaconda3/bin/python", str(ROOT / "scripts" / "launchd_run.py")]           # macOS 不讓 launchd 直接用 bash 讀 ~/Documents
    assert live["Label"] == "com.lh.trading.live" and live["ProgramArguments"] == launcher + ["run_live.sh"]
    assert live["WorkingDirectory"] == str(ROOT) and live["RunAtLoad"] is True
    assert live["KeepAlive"] == {"SuccessfulExit": False}                               # 只有異常結束才重啟；kill 停掉（exit 0）不會被拉回來
    assert {"/usr/sbin", "/sbin", "/usr/bin"} <= set(live["EnvironmentVariables"]["PATH"].split(":"))     # lsof、ifconfig、curl
    assert live["StandardOutPath"] == live["StandardErrorPath"] == "/tmp/lh_live_watchdog.log"
    fe = plistlib.loads((ROOT / "deploy/launchd/com.lh.trading.frontend.plist").read_bytes())
    assert fe["Label"] == "com.lh.trading.frontend" and fe["ProgramArguments"] == launcher + ["run_frontend.sh"]
    assert fe["KeepAlive"] is False and fe["RunAtLoad"] is True                         # 前端只在登入時啟動一次，不跟手動 build 打架


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_run_frontend(tmp_path):
    work = tmp_path / "work"
    (work / "frontend").mkdir(parents=True)
    (work / "run_frontend.sh").write_text((ROOT / "run_frontend.sh").read_text(encoding="utf-8"))
    calls = tmp_path / "npm_calls"
    busy = stub_bin(tmp_path / "busy", lsof="exit 0", npm=f'echo "$@" >> {calls}') if (tmp_path / "busy").mkdir() is None else None
    r = subprocess.run(["bash", str(work / "run_frontend.sh")], capture_output=True, text=True, env={"PATH": f"{busy}:/usr/bin:/bin"}, timeout=20)
    assert r.returncode == 0 and "已經有前端在跑" in r.stdout and not calls.exists()   # 3002 有人在用：不重複啟動
    free = stub_bin(tmp_path / "free", lsof="exit 1", npm=f'echo "$@" >> {calls}') if (tmp_path / "free").mkdir() is None else None
    r = subprocess.run(["bash", str(work / "run_frontend.sh")], capture_output=True, text=True, env={"PATH": f"{free}:/usr/bin:/bin"}, timeout=20)
    assert r.returncode == 0 and calls.read_text().split("\n")[:2] == ["run build", "run start"]     # 還沒 build 過：先 build 再啟動
    (work / "frontend" / ".next").mkdir()
    (work / "frontend" / ".next" / "BUILD_ID").write_text("x")
    calls.unlink()
    subprocess.run(["bash", str(work / "run_frontend.sh")], capture_output=True, text=True, env={"PATH": f"{free}:/usr/bin:/bin"}, timeout=20)
    assert calls.read_text().strip() == "run start"                                    # build 過：直接啟動


@pytest.mark.skipif(not HAS_BASH, reason="需要 bash")
def test_launchd_script_parses():
    r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / "launchd.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (ROOT / "scripts" / "launchd.sh").read_text(encoding="utf-8")
    assert "plutil -lint" in text and 'launchctl bootstrap "$DOMAIN"' in text and 'launchctl bootout "$DOMAIN/$l"' in text



def test_launchd_run_forwards_exit_codes_and_signals(tmp_path):
    """啟動器用 bash 的結束碼結束（launchd 靠這個分正常／異常），收到的 SIGTERM 轉給 bash（bash 的 trap 才會收拾子行程）。"""
    import sys
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "launchd_run.py").write_text((ROOT / "scripts" / "launchd_run.py").read_text(encoding="utf-8"))
    (root / "ok.sh").write_text("exit 0\n")
    (root / "bad.sh").write_text("exit 3\n")
    (root / "trap.sh").write_text('trap \'echo got-term > "$PWD/term"; exit 0\' TERM\nwhile true; do sleep 0.1; done\n')
    run = lambda name: subprocess.run([sys.executable, str(root / "scripts" / "launchd_run.py"), name], capture_output=True, text=True, timeout=20)  # noqa: E731
    assert run("ok.sh").returncode == 0 and run("bad.sh").returncode == 3
    assert run("../evil.sh").returncode == 2 and run("x.py").returncode == 2                 # 只能跑 repo 根目錄的 .sh
    p = subprocess.Popen([sys.executable, str(root / "scripts" / "launchd_run.py"), "trap.sh"])
    time.sleep(1.0)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0 and (root / "term").read_text().strip() == "got-term"      # 轉給 bash、bash 正常收尾
