import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.quote_hub import quote_hub

logger = logging.getLogger(__name__)
router = APIRouter()


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
