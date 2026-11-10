"""broker._acall 逾時之後 worker 才回覆（多半是錯誤）：沒人等的 future 要有人把例外收掉，asyncio 才不會記「Future exception was never retrieved」（一週 28 次）。"""
from __future__ import annotations

import asyncio
import gc
import itertools
import threading

import core.broker as br


class _Q:
    def __init__(self):
        self.items = []

    def put_nowait(self, x):
        self.items.append(x)


def _client(loop):
    c = object.__new__(br.BrokerClient)                                           # 不經過單例的 __new__：不碰其他測試共用的那個 broker
    c._is_connected = True
    c._req_lock = threading.Lock()
    c._req_counter = itertools.count(1)
    c._pending = {}
    c._cmd_q = _Q()
    c._loop = loop
    return c


def test_late_error_after_timeout_is_retrieved():
    unretrieved = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda l, ctx: unretrieved.append(ctx.get("message")))
        c = _client(loop)
        try:
            await c._acall("list_positions", timeout=0.01)
        except asyncio.TimeoutError:
            pass
        assert c._pending == {}                                                   # 逾時就不等了
        # worker 這時才回錯誤：_dispatch 在 pop 不到 future 時什麼都不做；但如果回覆剛好卡在逾時那一瞬間，
        # set_exception 會落在沒人等的 future 上——直接模擬那個 future 被設例外、然後被回收
        return c

    async def race():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda l, ctx: unretrieved.append(ctx.get("message")))
        c = _client(loop)
        created = []
        real = loop.create_future

        def spy():
            f = real()
            created.append(f)
            return f
        loop.create_future = spy                                                  # type: ignore[method-assign]
        try:
            await c._acall("margin", timeout=0.01)
        except asyncio.TimeoutError:
            pass
        fut = created[0]
        fut.set_exception(RuntimeError("Timeout Topic: api/v1/portfolio/margin"))   # 回覆晚到
        await asyncio.sleep(0)                                                    # 讓 done callback 跑
        del fut, created[:]
        gc.collect()

    asyncio.run(main())
    asyncio.run(race())
    gc.collect()
    assert not any(m and "never retrieved" in m for m in unretrieved), unretrieved


def test_a_cancelled_future_is_left_alone():
    f = asyncio.new_event_loop().create_future()
    f.cancel()
    br._retrieve_exception(f)                                                     # 不丟 CancelledError
