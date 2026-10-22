"""手機推播（core/notify.py）：通道設定、送出的內容、失敗不丟例外、秘密不進 log、冷卻（含跨進程）、CLI。
全部用假的 HTTP 函式，不碰真實網路。"""
from __future__ import annotations

import json
import logging
import urllib.error

import pytest

import core.notify as nt
from core.protection import write_position_state

TOKEN, CHAT, NTFY = "123456:SECRET-token-abc", "987654", "https://ntfy.example.org/very-long-secret-topic"


class FakeHttp:
    def __init__(self, fail=None, status=200):
        self.calls, self.fail, self.status = [], fail or {}, status

    def __call__(self, url, body, headers, timeout=8.0):
        self.calls.append((url, body, headers))
        for needle, exc in self.fail.items():
            if needle in url:
                raise exc
        return self.status


@pytest.fixture
def http(monkeypatch):
    h = FakeHttp()
    monkeypatch.setattr(nt, "_http_post", h)
    return h


def configure(monkeypatch, telegram=False, ntfy=False, webhook=False):
    if telegram:
        monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
        monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_ID", CHAT)
    if ntfy:
        monkeypatch.setenv("NOTIFY_NTFY_URL", NTFY)
    if webhook:
        monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.org/x/y")


def test_channels_follow_the_environment(monkeypatch):
    assert nt.channels() == []
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    assert nt.channels() == []                                              # 只有 token 沒有 chat id：不算
    configure(monkeypatch, telegram=True, ntfy=True, webhook=True)
    assert nt.channels() == ["telegram", "ntfy", "webhook"]


def test_no_channel_means_no_request_but_the_message_is_still_in_the_log(http, caplog):
    with caplog.at_level(logging.WARNING):
        assert nt.send_sync("標題", "內容\n第二行") == {}
    assert http.calls == [] and "NOTIFY 標題｜內容 ／ 第二行" in caplog.text and "沒有設定任何推播通道" in caplog.text


def test_telegram_request_shape(monkeypatch, http):
    configure(monkeypatch, telegram=True)
    assert nt.send_sync("持倉警示", "TMFJ6 多 1 口") == {"telegram": "ok"}
    (url, body, headers), = http.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body.decode() == f"chat_id={CHAT}&text=%E6%8C%81%E5%80%89%E8%AD%A6%E7%A4%BA%0ATMFJ6+%E5%A4%9A+1+%E5%8F%A3"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_ntfy_posts_json_to_the_server_root_with_the_topic_inside(monkeypatch, http):
    configure(monkeypatch, ntfy=True)
    assert nt.send_sync("持倉警示", "訊息", "urgent") == {"ntfy": "ok"}
    (url, body, headers), = http.calls
    assert url == "https://ntfy.example.org/"                                # 標題有中文，不放在標頭，改用 JSON 發佈
    assert json.loads(body) == {"topic": "very-long-secret-topic", "title": "持倉警示", "message": "訊息", "priority": 5, "tags": ["warning"]}
    assert headers["Content-Type"].startswith("application/json")


def test_webhook_and_a_bad_ntfy_url(monkeypatch, http):
    configure(monkeypatch, webhook=True)
    assert nt.send_sync("t", "m", "default") == {"webhook": "ok"}
    assert json.loads(http.calls[0][1]) == {"title": "t", "message": "m", "priority": "default"}
    monkeypatch.setenv("NOTIFY_NTFY_URL", "https://ntfy.example.org/")     # 沒有主題
    res = nt.send_sync("t", "m")
    assert res["ntfy"].startswith("ValueError") and res["webhook"] == "ok"      # 一個通道壞了不影響其他通道


def test_every_configured_channel_is_tried_and_failures_never_raise(monkeypatch, caplog):
    configure(monkeypatch, telegram=True, ntfy=True)
    h = FakeHttp(fail={"api.telegram.org": urllib.error.URLError("timed out"), "ntfy.example.org": TimeoutError("slow")})
    monkeypatch.setattr(nt, "_http_post", h)
    with caplog.at_level(logging.WARNING):
        res = nt.send_sync("t", "m")
    assert res == {"telegram": "URLError: <urlopen error timed out>", "ntfy": "TimeoutError: slow"} and len(h.calls) == 2
    assert "推播有通道失敗" in caplog.text


def test_non_2xx_is_reported_as_a_failure(monkeypatch):
    configure(monkeypatch, webhook=True)
    monkeypatch.setattr(nt, "_http_post", FakeHttp(status=500))
    assert nt.send_sync("t", "m") == {"webhook": "HTTP 500"}


def test_secrets_never_reach_the_log(monkeypatch, caplog):
    """錯誤訊息裡若帶到 token、chat id、ntfy 主題，一律遮掉。"""
    configure(monkeypatch, telegram=True, ntfy=True)
    leak = ValueError(f"bad url https://api.telegram.org/bot{TOKEN}/sendMessage chat={CHAT} topic=very-long-secret-topic host=ntfy.example.org")
    monkeypatch.setattr(nt, "_http_post", FakeHttp(fail={"api.telegram.org": leak, "ntfy.example.org": leak}))
    with caplog.at_level(logging.DEBUG):
        res = nt.send_sync("t", "m")
    blob = json.dumps(res) + caplog.text
    for secret in (TOKEN, CHAT, "very-long-secret-topic", "ntfy.example.org"):
        assert secret not in blob
    assert "***" in res["telegram"]


def test_a_long_message_is_cut(monkeypatch, http):
    configure(monkeypatch, webhook=True)
    nt.send_sync("t", "字" * (nt.MAX_LEN + 500))
    assert len(json.loads(http.calls[0][1])["message"]) == nt.MAX_LEN + 1


def test_http_post_only_accepts_http_and_https():
    with pytest.raises(ValueError, match="http"):
        nt.REAL_HTTP_POST("file:///etc/passwd", b"x", {})
    with pytest.raises(ValueError, match="http"):
        nt.REAL_HTTP_POST("ftp://example.org/", b"x", {})


# ── 冷卻 ─────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(nt, "_clock", c)
    return c


def test_cooldown_per_key(monkeypatch, http, clock):
    configure(monkeypatch, webhook=True)
    assert nt.notify("t", "m", key="a", cooldown=600, wait=True) is True and len(http.calls) == 1
    clock.t += 599
    assert nt.notify("t", "m", key="a", cooldown=600, wait=True) is False and len(http.calls) == 1        # 599 秒：還在冷卻
    assert nt.notify("t", "m", key="b", cooldown=600, wait=True) is True and len(http.calls) == 2         # 另一件事不受影響
    clock.t += 2
    assert nt.notify("t", "m", key="a", cooldown=600, wait=True) is True and len(http.calls) == 3         # 601 秒：可以再送
    assert nt.notify("t", "m", wait=True) and nt.notify("t", "m", wait=True) and len(http.calls) == 5    # 沒給 key 就不擋


def test_a_failed_push_gives_the_cooldown_back(monkeypatch, clock):
    configure(monkeypatch, webhook=True)
    h = FakeHttp(fail={"hooks.example.org": OSError("down")})
    monkeypatch.setattr(nt, "_http_post", h)
    n = 1 + len(nt.RETRY_DELAYS)                                                                            # 網路類錯誤：每則先送一次＋重試（發現 68）
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True and len(h.calls) == n
    clock.t += 5
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True and len(h.calls) == 2 * n       # 通道壞了不會讓我們沉默 10 分鐘
    h.fail.clear()
    clock.t += 5
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True and len(h.calls) == 2 * n + 1
    clock.t += 5
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is False                                  # 成功送出之後才開始冷卻


def test_no_channel_still_consumes_the_cooldown_so_the_log_is_not_flooded(http, clock):
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True
    clock.t += 15
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is False


def test_cooldown_survives_a_new_process(monkeypatch, http, clock, tmp_path):
    """watchdog 每次呼叫都是新進程：冷卻要存在檔案裡。"""
    configure(monkeypatch, webhook=True)
    monkeypatch.setenv("NOTIFY_COOLDOWN_FILE", str(tmp_path / "cool.json"))
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True
    nt._memory_last.clear()                                                      # 模擬新進程：記憶體是空的
    clock.t += 30
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is False and len(http.calls) == 1
    assert json.loads((tmp_path / "cool.json").read_text()) == {"k": 1000.0}
    nt._memory_last.clear()
    clock.t += 700
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True


def test_notify_in_the_background_does_not_block_and_never_raises(monkeypatch, http):
    configure(monkeypatch, webhook=True)
    import threading
    done = threading.Event()
    monkeypatch.setattr(nt, "send_sync", lambda *a, **k: (done.set(), {"webhook": "ok"})[1])
    assert nt.notify("t", "m") is True
    assert done.wait(2)
    monkeypatch.setattr(nt, "send_sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert nt.notify("t", "m", wait=True) is True                                # 內部例外被吞掉


# ── CLI ─────────────────────────────────────────────────────────

def test_cli_status_and_test_exit_codes(monkeypatch, http, capsys):
    assert nt.main(["status"]) == 2 and "沒有" in capsys.readouterr().out
    assert nt.main(["test"]) == 2                                                  # 沒設定通道
    configure(monkeypatch, telegram=True, webhook=True)
    assert nt.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "telegram、webhook" in out and TOKEN not in out and CHAT not in out
    assert nt.main(["test"]) == 0 and len(http.calls) == 2
    monkeypatch.setattr(nt, "_http_post", FakeHttp(status=403))
    assert nt.main(["test"]) == 1                                                  # 通道有設定但全部失敗


def write_state(path, positions, unprot=(), ts=None):
    assert write_position_state(positions, list(unprot), "live", path=path, now=ts)


def test_cli_watchdog_alerts_only_when_there_is_a_position(monkeypatch, http, tmp_path, clock, capsys):
    configure(monkeypatch, webhook=True)
    monkeypatch.setenv("POSITION_STATE_PATH", str(tmp_path / "state.json"))
    assert nt.main(["watchdog", "--event", "freeze", "--detail", "down"]) == 0
    assert "沒有持倉狀態檔" in capsys.readouterr().out and http.calls == []     # 不知道有沒有持倉：不打擾
    write_state(tmp_path / "state.json", [])
    assert nt.main(["watchdog", "--event", "freeze", "--detail", "down"]) == 0
    assert "沒有持倉" in capsys.readouterr().out and http.calls == []
    write_state(tmp_path / "state.json", [{"code": "TMFJ6", "direction": "Buy", "quantity": 1, "price": 49050.0}], ts=clock.t - 20)
    assert nt.main(["watchdog", "--event", "freeze", "--detail", "down"]) == 0
    sent = json.loads(http.calls[0][1])
    assert "凍結或斷線" in sent["title"] and "TMFJ6 多 1 口" in sent["message"] and "連 /health 都沒有回應" in sent["message"] and sent["priority"] == "urgent"
    assert nt.main(["watchdog", "--event", "freeze", "--detail", "down"]) == 0
    assert len(http.calls) == 1 and "冷卻中" in capsys.readouterr().out          # 10 分鐘內同一種事件不重複
    assert nt.main(["watchdog", "--event", "exit", "--detail", "gone"]) == 0 and len(http.calls) == 2   # 另一種事件照送


def test_cli_watchdog_exit_codes_reflect_delivery(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("POSITION_STATE_PATH", str(tmp_path / "state.json"))
    write_state(tmp_path / "state.json", [{"code": "TMFJ6", "direction": "Sell", "quantity": 2, "price": 1.0}])
    assert nt.main(["watchdog"]) == 2                                              # 有持倉但沒有任何通道
    configure(monkeypatch, webhook=True)
    monkeypatch.setattr(nt, "_http_post", FakeHttp(status=500))
    nt._memory_last.clear()
    assert nt.main(["watchdog"]) == 1                                              # 通道全失敗
    monkeypatch.setattr(nt, "_http_post", FakeHttp())
    assert nt.main(["watchdog"]) == 0                                              # 失敗那次把冷卻還回去了，這次送得出去


def test_one_working_channel_keeps_the_cooldown(monkeypatch, clock):
    """telegram 送到了、ntfy 失敗：事情已經通知到了，不要因為另一個通道壞掉就重複推。"""
    configure(monkeypatch, telegram=True, ntfy=True)
    h = FakeHttp(fail={"ntfy.example.org": OSError("down")})
    monkeypatch.setattr(nt, "_http_post", h)
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is True
    clock.t += 5
    assert nt.notify("t", "m", key="k", cooldown=600, wait=True) is False
    assert len([c for c in h.calls if "api.telegram.org" in c[0]]) == 1


# ── Telegram chat id 小工具（python -m core.notify chat-id）──────────

def updates(*chats, key="message"):
    return {"ok": True, "result": [{"update_id": i, key: {"message_id": 1, "chat": c, "text": "/start"}} for i, c in enumerate(chats)]}


def fake_get(monkeypatch, payload=None, exc=None):
    calls = []

    def get(url, timeout=8.0):
        calls.append(url)
        if exc:
            raise exc
        return 200, json.dumps(payload)

    monkeypatch.setattr(nt, "_http_get", get)
    return calls


def test_telegram_chats_dedupes_keeps_order_and_skips_bad_entries():
    me = {"id": 987654, "type": "private", "first_name": "小明", "last_name": "王"}
    grp = {"id": -100123, "type": "supergroup", "title": "交易群"}
    data = updates(me, grp, me)
    data["result"] += [{"update_id": 9, "channel_post": {"chat": {"id": -100999, "type": "channel", "username": "chan"}}},
                       {"update_id": 10, "my_chat_member": {"chat": {"id": 5, "type": "private"}}}, {"update_id": 11, "message": {"chat": {"id": "x"}}},
                       {"update_id": 12}, "壞掉", {"update_id": 13, "message": "不是字典"}]
    assert nt.telegram_chats(data) == [{"id": 987654, "type": "private", "name": "小明 王"},
                                       {"id": -100123, "type": "supergroup", "name": "交易群"}, {"id": -100999, "type": "channel", "name": "chan"},
                                       {"id": 5, "type": "private", "name": ""}]
    assert nt.telegram_chats({}) == [] and nt.telegram_chats(None) == [] and nt.telegram_chats({"result": "x"}) == []


def test_chat_id_without_a_token_explains_and_makes_no_request(monkeypatch, capsys):
    calls = fake_get(monkeypatch, updates())
    assert nt.main(["chat-id"]) == 2
    assert "NOTIFY_TELEGRAM_TOKEN" in capsys.readouterr().out and calls == []


def test_chat_id_prints_the_id_and_the_line_to_paste_but_never_the_token(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    calls = fake_get(monkeypatch, updates({"id": 987654, "type": "private", "first_name": "小明"}))
    assert nt.main(["chat-id"]) == 0
    out = capsys.readouterr().out
    assert calls == [f"https://api.telegram.org/bot{TOKEN}/getUpdates?limit=100"]
    assert "聊天室 id：987654　（private，小明）" in out and "NOTIFY_TELEGRAM_CHAT_ID=987654" in out and TOKEN not in out


def test_chat_id_with_several_chats_lists_them_all_and_suggests_the_first(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    fake_get(monkeypatch, updates({"id": 987654, "type": "private", "first_name": "小明"}, {"id": -100123, "type": "group", "title": "群"}))
    assert nt.main(["chat-id"]) == 0
    out = capsys.readouterr().out
    assert "987654" in out and "-100123" in out and "NOTIFY_TELEGRAM_CHAT_ID=987654" in out


def test_chat_id_when_nobody_has_talked_to_the_bot_yet(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    fake_get(monkeypatch, {"ok": True, "result": []})
    assert nt.main(["chat-id"]) == 1
    assert "先在 Telegram 對你的 bot 傳一則訊息" in capsys.readouterr().out


def test_chat_id_reports_telegram_errors_without_leaking_the_token(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    fake_get(monkeypatch, {"ok": False, "description": f"Unauthorized {TOKEN}"})
    assert nt.main(["chat-id"]) == 1
    out = capsys.readouterr().out
    assert "Telegram 回報失敗" in out and TOKEN not in out and "***" in out
    fake_get(monkeypatch, exc=urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
    assert nt.main(["chat-id"]) == 1
    out = capsys.readouterr().out
    assert "HTTP Error 401" in out and "token 寫錯" in out and TOKEN not in out
    fake_get(monkeypatch, exc=ValueError(f"bad url https://api.telegram.org/bot{TOKEN}/getUpdates"))
    assert nt.main(["chat-id"]) == 1
    assert TOKEN not in capsys.readouterr().out


def test_chat_id_survives_a_non_json_reply(monkeypatch, capsys):
    monkeypatch.setenv("NOTIFY_TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(nt, "_http_get", lambda url, timeout=8.0: (200, "<html>不是 JSON</html>"))
    assert nt.main(["chat-id"]) == 1
    assert "向 Telegram 查詢失敗" in capsys.readouterr().out


def test_http_get_only_accepts_http_and_https():
    with pytest.raises(ValueError, match="http"):
        nt.REAL_HTTP_GET("file:///etc/passwd")


# ── 測試本身不能送出真的推播（這個檢查是為了防止再發生「測試把假警示送到使用者手機」）──

def test_tests_can_never_reach_the_network_or_load_the_real_dotenv(monkeypatch):
    import os
    assert os.getenv("NOTIFY_SKIP_DOTENV") == "1"
    with pytest.raises(BaseException) as e:
        nt._http_post("https://api.telegram.org/botX/sendMessage", b"x", {})                  # 預設的 _http_post 是會直接失敗的替身
    assert "測試不能真的送出推播" in str(e.value) and not isinstance(e.value, Exception)
    with pytest.raises(BaseException):
        nt._http_get("https://api.telegram.org/botX/getUpdates")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.org/x")
    with pytest.raises(BaseException):
        nt.send_sync("t", "m")                                                                 # 就算有設定通道，也送不出去（而且會大聲失敗，不是靜靜吞掉）


def test_main_does_not_read_the_dotenv_file_when_told_not_to(monkeypatch, tmp_path):
    loaded = []
    import types, sys as _sys
    fake = types.SimpleNamespace(load_dotenv=lambda path: loaded.append(path))
    monkeypatch.setitem(_sys.modules, "dotenv", fake)
    nt.main(["status"])
    assert loaded == []                                                                        # NOTIFY_SKIP_DOTENV=1：不讀
    monkeypatch.delenv("NOTIFY_SKIP_DOTENV")
    nt.main(["status"])
    assert len(loaded) == 1 and str(loaded[0]).endswith(".env")


def test_simulation_mode_does_not_push(monkeypatch, http):
    configure(monkeypatch, webhook=True)
    monkeypatch.setenv("SIMULATION", "true")
    assert nt.sim_blocked() is True
    assert nt.notify("t", "m", wait=True) is False and http.calls == []                         # 模擬盤的成交與持倉不是真錢，不推到手機
    monkeypatch.setenv("NOTIFY_SIM", "true")
    assert nt.sim_blocked() is False and nt.notify("t", "m", wait=True) is True and len(http.calls) == 1
    monkeypatch.setenv("SIMULATION", "false")
    monkeypatch.delenv("NOTIFY_SIM")
    assert nt.sim_blocked() is False
    monkeypatch.delenv("SIMULATION")
    assert nt.sim_blocked() is False                                                            # 沒設定＝不擋（測試、CLI）



# ── 失敗重試（發現 68）───────────────────────────────────────────

def test_retry_only_failed_channels_and_note_late_delivery(monkeypatch, clock):
    """10/20 08:35 的盤前摘要因為網路瞬斷（No route to host）沒送到：網路類錯誤要重送，而且只重送失敗的通道；晚 30 秒以上才送到的註明原本時間。"""
    import urllib.error
    configure(monkeypatch, webhook=True, telegram=True)
    h = FakeHttp(fail={"api.telegram.org": urllib.error.URLError("[Errno 65] No route to host")})
    monkeypatch.setattr(nt, "_http_post", h)
    waits = []

    def fake_sleep(sec):
        waits.append(sec)
        nt_time[0] += sec
        if len(waits) == 2:
            h.fail.clear()                                                               # 第二次重試時網路回來了
    nt_time = [1_000_000.0]
    monkeypatch.setattr(nt, "_sleep", fake_sleep)
    monkeypatch.setattr(nt.time, "time", lambda: nt_time[0])
    res = nt._deliver("📋 盤前摘要", "內容", "default", None, 0)
    assert res == {"telegram": "ok", "webhook": "ok"} and waits == [15.0, 60.0]
    tg = [c for c in h.calls if "telegram" in c[0]]
    wh = [c for c in h.calls if "hooks.example.org" in c[0]]
    assert len(tg) == 3 and len(wh) == 1                                                  # webhook 第一次就成功：不重送
    import urllib.parse
    last = urllib.parse.parse_qs(tg[-1][1].decode())["text"][0]
    assert "網路恢復後補送：原本" in last                                                 # 晚了 75 秒
    first = urllib.parse.parse_qs(tg[0][1].decode())["text"][0]
    early = urllib.parse.parse_qs(tg[1][1].decode())["text"][0]
    assert "補送" not in first and "補送" not in early                                    # 第一次重試只晚 15 秒：不加註


def test_config_errors_are_not_retried(monkeypatch, clock):
    configure(monkeypatch, webhook=True)
    h = FakeHttp(status=400)
    monkeypatch.setattr(nt, "_http_post", h)
    res = nt._deliver("t", "m", "high", None, 0)
    assert res == {"webhook": "HTTP 400"} and len(h.calls) == 1
    assert nt.retryable("URLError: <urlopen error [Errno 65] No route to host>") and nt.retryable("TimeoutError: timed out") and nt.retryable("HTTP 502")
    assert not nt.retryable("HTTP 400") and not nt.retryable("ValueError: 通知網址只接受 http／https") and not nt.retryable("NoRealNetwork: x")
