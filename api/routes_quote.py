import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/last")
def last_prices() -> dict[str, float]:
    """最新價快取（code→價），由報價推播填入。純讀我方記憶體，不對永豐發查詢。
    前端下單面板用來顯示台指現價。"""
    return quote_hub.all_last_prices()


@router.websocket("/ws")
async def quote_ws(ws: WebSocket) -> None:
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    quote_hub.add_ws_client(q)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                await ws.send_text('{"ping":1}')
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Quote WebSocket error: %s", e)
    finally:
        quote_hub.remove_ws_client(q)
