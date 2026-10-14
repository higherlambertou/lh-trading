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
