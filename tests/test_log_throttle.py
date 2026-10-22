"""重複錯誤的 log 節流（發現 65）：第一次完整、之後每 5 分鐘一行彙總、換了錯誤再完整記一次、恢復記一行；worker 與手動停損監控都接上了。"""
from __future__ import annotations

import logging

from core import log_throttle as lt

ERR = RuntimeError("list_positions: Shioaji error Session error … code: NotReady (c0,s1) req 1234")


def test_signature_ignores_numbers_and_keeps_the_type():
    a = lt.signature(RuntimeError("timeout req 12 session (c0,s1)"))
    b = lt.signature(RuntimeError("timeout req 99 session (c3,s7)"))
    assert a == b and a.startswith("RuntimeError: ")
    assert lt.signature(ValueError("x")) != lt.signature(RuntimeError("x"))
    assert len(lt.signature(RuntimeError("x" * 500))) <= len("RuntimeError: ") + 160
    assert lt.signature("純文字") == ": 純文字"


def test_throttle_sequence():
    t = lt.ErrorThrottle(window=300)
    sig = lt.signature(ERR)
    assert t.error("list_positions", sig, 0) == ("full", 0)
    assert t.error("list_positions", sig, 5) == ("quiet", 0)
    assert t.error("list_positions", sig, 299) == ("quiet", 0)
    assert t.error("list_positions", sig, 300) == ("summary", 3)
    assert t.error("list_positions", sig, 301) == ("quiet", 0)
    assert t.error("margin", sig, 301) == ("full", 0)                                   # 另一個查詢各算各的
    assert t.error("list_positions", lt.signature(ValueError("別的")), 302) == ("full", 0)     # 換了一種錯誤：再記一次完整的
    assert t.success("list_positions", 400) == (1, 98)
    assert t.success("list_positions", 401) is None
    assert t.error("list_positions", sig, 500) == ("full", 0)                           # 恢復之後再壞：重新開始


def test_worker_logs_full_once_then_summaries_then_recovery(caplog):
    from core import shioaji_worker as w
    caplog.set_level(logging.INFO, logger="core.shioaji_worker")
    t = lt.ErrorThrottle(window=300)
    for k in range(10):
        w.log_failure(t, "list_positions", ERR, 10.0 * k, "Traceback (most recent call last): ...")
    text = caplog.text
    assert text.count("Traceback") == 1 and text.count("Worker 執行 list_positions 失敗") == 1
    w.log_failure(t, "list_positions", ERR, 300.0, "Traceback ...")
    assert "過去 5 分鐘又發生 10 次" in caplog.text and caplog.text.count("Traceback") == 1
    w.log_success(t, "list_positions", 360.0)
    assert "Worker 執行 list_positions 恢復（先前連續失敗 11 次、360 秒）" in caplog.text
    caplog.clear()
    w.log_failure(t, "margin", ERR, 400.0, "Traceback ...")
    w.log_success(t, "margin", 401.0)                                                   # 只失敗一次：完整的已經記了，不另外記恢復
    assert "恢復" not in caplog.text


def test_manual_monitor_warnings_are_throttled(caplog):
    from core.manual_monitor import ManualOrderMonitor
    m = ManualOrderMonitor()
    caplog.set_level(logging.INFO, logger="core.manual_monitor")
    for _ in range(30):
        m._warn("positions", "ManualMonitor 查詢部位失敗", ERR)
    assert caplog.text.count("ManualMonitor 查詢部位失敗") == 1
    m._recovered("positions")
    assert "ManualMonitor 查詢部位恢復（先前連續失敗 30 次" in caplog.text
    m._warn("trades", "ManualMonitor 查詢委託狀態逾時")
    assert "ManualMonitor 查詢委託狀態逾時" in caplog.text


def test_throttle_errors_never_escape(monkeypatch, caplog):
    """在停損監控迴圈與 worker 的 except 裡呼叫：節流本身壞掉也不能把例外丟出去（迴圈會死掉）。"""
    from core import shioaji_worker as w
    from core.manual_monitor import ManualOrderMonitor

    class Broken(lt.ErrorThrottle):
        def error(self, *a):
            raise RuntimeError("broken")

        def success(self, *a):
            raise RuntimeError("broken")
    m = ManualOrderMonitor()
    m._log_throttle = Broken()
    caplog.set_level(logging.INFO)
    m._warn("positions", "ManualMonitor 查詢部位失敗", ERR)
    m._recovered("positions")
    w.log_failure(Broken(), "list_positions", ERR, 0.0, "Traceback ...")
    w.log_success(Broken(), "list_positions", 1.0)
    assert "ManualMonitor 查詢部位失敗" in caplog.text and "Worker 執行 list_positions 失敗" in caplog.text


# ── uvicorn 存取紀錄（發現 66）───────────────────────────────────

def access_record(method, status, path="/api/quote/last"):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("100.97.169.26:51234", method, path, "1.1", status), None)


def test_quiet_access_filter():
    f = lt.QuietAccessFilter()
    assert not f.filter(access_record("GET", 200)) and not f.filter(access_record("GET", 304)) and not f.filter(access_record("OPTIONS", 200))
    assert f.filter(access_record("GET", 404)) and f.filter(access_record("GET", 503)) and f.filter(access_record("GET", 500))
    assert f.filter(access_record("POST", 200, "/api/order/place")) and f.filter(access_record("DELETE", 200))
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "別的訊息 %s", ("x",), None)
    assert f.filter(other)


def test_install_access_filter(monkeypatch):
    name = "test.access.logger"
    lg = logging.getLogger(name)
    monkeypatch.delenv("ACCESS_LOG", raising=False)
    assert lt.install_access_filter(name) and lt.install_access_filter(name)
    assert sum(isinstance(x, lt.QuietAccessFilter) for x in lg.filters) == 1                   # 不會裝兩次
    for x in list(lg.filters):
        lg.removeFilter(x)
    monkeypatch.setenv("ACCESS_LOG", "all")
    assert not lt.install_access_filter(name) and not lg.filters
    src = (lt.__file__.rsplit("/core/", 1)[0] + "/main.py")
    assert "install_access_filter()" in open(src, encoding="utf-8").read()
