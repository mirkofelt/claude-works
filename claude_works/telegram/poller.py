import asyncio
import logging
import time
from typing import AsyncIterator, Callable, Awaitable

from .api import TelegramAPI

logger = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], Awaitable[None]]


class TelegramPoller:
    def __init__(self, api: TelegramAPI, on_update: UpdateHandler) -> None:
        self._api = api
        self._on_update = on_update
        self._offset: int = 0
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poller")
        logger.info("Poller started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Poller stopped")

    @property
    def is_running(self) -> bool:
        return self._running and (self._task is not None) and not self._task.done()

    async def _poll_loop(self) -> None:
        backoff = 1
        while self._running:
            try:
                updates = await self._api.get_updates(offset=self._offset, timeout=25)
                backoff = 1
                for update in updates:
                    uid = update["update_id"]
                    utype = next((k for k in update if k != "update_id"), "unknown")
                    logger.debug("Update %d type=%s", uid, utype)
                    self._offset = uid + 1
                    asyncio.create_task(
                        self._dispatch(update),
                        name=f"update-{uid}",
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll error: %s — retry in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _dispatch(self, update: dict) -> None:
        try:
            await self._on_update(update)
        except Exception as e:
            logger.error("Update dispatch error (update_id=%s): %s", update.get("update_id"), e)
