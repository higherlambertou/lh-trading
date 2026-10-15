"""啟動前的位址檢查（core/bind_check.py，update.md 發現 36）：Tailscale 位址不在本機時立刻結束，不要先開 worker、登入券商。"""
from __future__ import annotations

import errno
import socket

import pytest

import core.bind_check as bc


def test_a_local_address_is_fine():
    assert bc.check_bind("127.0.0.1", 0) is None                                   # port 0＝隨便一個空的
    assert bc.check_bind("0.0.0.0", 0) is None


def test_an_address_that_is_not_on_this_machine_is_reported():
    msg = bc.check_bind("203.0.113.77", 0)                                         # TEST-NET-3：不可能是本機的位址
    assert msg is not None and "203.0.113.77" in msg and "Tailscale" in msg


def test_a_port_already_in_use_is_not_an_address_problem():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        # SO_REUSEADDR 在 macOS 上對「正在 listen」的 port 仍然會 EADDRINUSE；不管哪個結果，都不能當成「位址不在」
        assert bc.check_bind("127.0.0.1", port) is None
    finally:
        s.close()


def test_other_errors_are_left_to_uvicorn(monkeypatch):
    class Boom:
        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise OSError(errno.EACCES, "Permission denied")

        def close(self):
            pass

    monkeypatch.setattr(bc.socket, "socket", lambda *a, **k: Boom())
    assert bc.check_bind("127.0.0.1", 80) is None


def test_preflight_exits_with_3_before_anything_starts(capsys):
    with pytest.raises(SystemExit) as e:
        bc.preflight("203.0.113.77", 0)
    assert e.value.code == bc.EXIT_NO_ADDRESS == 3
    assert "無法啟動" in capsys.readouterr().err
    bc.preflight("127.0.0.1", 0)                                                   # 位址在：什麼都不發生


def test_the_socket_is_always_closed(monkeypatch):
    closed = []

    class S:
        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(bc.socket, "socket", lambda *a, **k: S())
    assert bc.check_bind("1.2.3.4", 1) is not None and closed == [True]


# ── 模擬盤不再綁 0.0.0.0（update.md 發現 41）──────────────────────

def test_sim_bind_host_precedence_and_default():
    from core.bind_check import sim_bind_host
    assert sim_bind_host({}) == "127.0.0.1"                                            # 什麼都沒設：只有本機
    assert sim_bind_host({"BIND_HOST": "100.127.125.13"}) == "100.127.125.13"          # 跟正式盤同一個 Tailscale 位址
    assert sim_bind_host({"BIND_HOST": "100.1.1.1", "SIM_BIND_HOST": "100.2.2.2"}) == "100.2.2.2"
    assert sim_bind_host({"SIM_BIND_HOST": "  ", "BIND_HOST": " 100.1.1.1 "}) == "100.1.1.1"   # 空白當沒設、前後空白去掉


def test_main_sim_never_hardcodes_a_wildcard_host():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main_sim.py").read_text(encoding="utf-8")
    assert not re.search(r'host\s*=\s*["\']0\.0\.0\.0["\']', src)
    assert "sim_bind_host()" in src and "preflight(" in src                             # 位址不在本機就先結束，不先登入券商


def _health_url_from_run_sim(tmp_path, env_text):
    """執行 run_sim.sh 裡算健康檢查位址的那幾行（PORT=8003 到 HEALTH_URL=…），回傳結果。"""
    import subprocess
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "run_sim.sh").read_text(encoding="utf-8")
    i, j = text.index("PORT=8003"), text.index('HEALTH_URL="http://${HEALTH_HOST}')
    j = text.index("\n", j)
    (tmp_path / ".env").write_text(env_text)
    r = subprocess.run(["bash", "-c", "set -u\n" + text[i:j] + '\necho "$HEALTH_URL"'], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("env_text,expect", [
    ("BIND_HOST=100.127.125.13\n", "http://100.127.125.13:8003/api/health"),
    ("BIND_HOST=100.127.125.13\nSIM_BIND_HOST=100.9.9.9\n", "http://100.9.9.9:8003/api/health"),
    ('BIND_HOST="100.127.125.13"\n', "http://100.127.125.13:8003/api/health"),             # 引號會去掉
    ("SOMETHING=else\n", "http://127.0.0.1:8003/api/health"),
    ("", "http://127.0.0.1:8003/api/health"),
])
def test_run_sim_health_check_follows_the_same_host_as_main_sim(tmp_path, env_text, expect):
    assert _health_url_from_run_sim(tmp_path, env_text) == expect
