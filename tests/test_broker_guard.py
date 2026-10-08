"""core/broker_guard.py：登入「之前」先看正式盤後端目前有幾條券商連線。

背景（2026-10-08）：唯讀歷史資料工具每次另外登入一條，券商回收連線有延遲，連跑幾次就疊起來（3 → 5）；
上限 5 條，高峰時正式盤剛好重啟就會登不進去。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import core.broker_guard as bg
import core.hurst_study as hs
import core.indicator_history as ih
from core.market_store import MarketStore


class Handler(BaseHTTPRequestHandler):
    body, status = {"connections": 3}, 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.body).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/position/usage", Handler
    srv.shutdown()
    Handler.body, Handler.status = {"connections": 3}, 200


def test_usage_url_follows_the_backend_settings():
    assert bg.usage_url({"BIND_HOST": "100.1.2.3", "PORT": "8002"}) == "http://100.1.2.3:8002/api/position/usage"
    assert bg.usage_url({"BIND_HOST": "0.0.0.0"}) == "http://localhost:8002/api/position/usage"      # 0.0.0.0 只能監聽，不能連
    assert bg.usage_url({}) == "http://localhost:8002/api/position/usage"
    assert bg.usage_url({"USAGE_URL": "http://x/y"}) == "http://x/y"


def test_peek_reads_the_connection_count(server):
    url, h = server
    assert bg.peek_connections(url) == 3
    h.body = {"connections": 5}
    assert bg.peek_connections(url) == 5


def test_peek_distinguishes_backend_down_from_backend_not_ready(server):
    url, h = server
    h.status, h.body = 503, {"detail": "流量資料尚未就緒"}
    with pytest.raises(bg.BackendNotReady):
        bg.peek_connections(url)                                  # 有開但剛啟動：不知道幾條
    assert bg.peek_connections("http://127.0.0.1:9/x", timeout=1) is None      # 連不上＝後端沒開
    h.status, h.body = 200, {"oops": 1}
    assert bg.peek_connections(url) is None                       # 格式不對當作查不到


@pytest.mark.parametrize("n,ok", [(0, True), (2, True), (3, True), (4, False), (5, False)])
def test_login_is_allowed_only_while_there_is_a_spare_slot(n, ok):
    allowed, why = bg.room_for_login(peek=lambda: n)
    assert allowed is ok and str(n) in why
    if not ok:
        assert "保留 1 條給正式盤重啟" in why


def test_backend_down_is_allowed_but_not_ready_is_refused():
    assert bg.room_for_login(peek=lambda: None)[0] is True       # 後端沒開，沒有別的連線在搶

    def booting():
        raise bg.BackendNotReady("HTTP 503")

    allowed, why = bg.room_for_login(peek=booting)
    assert allowed is False and "剛啟動" in why                   # 剛重啟時連線最多，不要在這時候多登入一條


def never_login():
    raise AssertionError("不該登入")


def test_flow_fetch_does_not_log_in_when_the_guard_says_no(tmp_path, monkeypatch):
    monkeypatch.setattr(ih, "open_readonly_api", never_login)
    res = ih.fetch_flow_history(3, store=MarketStore(tmp_path / "m.db"), sleep=lambda s: None, log=lambda *a: None,
                                precheck=lambda: (False, "目前 4 條連線"))
    assert res["stopped"] == "目前 4 條連線" and res["fetched"] == []


def test_flow_fetch_checks_before_logging_in_and_not_when_given_an_api(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ih, "open_readonly_api", lambda: (_ for _ in ()).throw(RuntimeError("login")))
    with pytest.raises(RuntimeError):
        ih.fetch_flow_history(3, store=MarketStore(tmp_path / "m.db"), log=lambda *a: None,
                              precheck=lambda: seen.append("checked") or (True, "ok"))
    assert seen == ["checked"]                                    # 先檢查、才登入


def test_kbars_fetch_refuses_before_importing_the_broker(monkeypatch):
    monkeypatch.setattr(bg, "room_for_login", lambda *a, **k: (False, "目前 4 條連線"))
    with pytest.raises(RuntimeError, match="4 條連線"):
        hs.fetch_history(30)
