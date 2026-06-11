import asyncio
import logging
import time
from typing import Callable, Awaitable

from .api import TelegramAPI

logger = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], Awaitable[None]]

_ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "callback_query"]


class TelegramPoller:
    def __init__(self, api: TelegramAPI, on_update: UpdateHandler, skip_before_ts: int | None = None) -> None:
        self._api = api
        self._on_update = on_update
        self._offset: int = 0
        self._running = False
        self._task: asyncio.Task | None = None
        self._skip_before_ts: int = skip_before_ts if skip_before_ts is not None else int(time.time())

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poller")
        logger.info("Poller started (skipping messages before ts=%d)", self._skip_before_ts)

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
                updates = await self._api.get_updates(
                    offset=self._offset,
                    timeout=25,
                    allowed_updates=_ALLOWED_UPDATES,
                )
                backoff = 1
                for update in updates:
                    uid = update["update_id"]
                    self._offset = uid + 1
                    # Skip messages that arrived before bot startup
                    msg_ts = (update.get("message") or update.get("edited_message") or {}).get("date", 0)
                    if msg_ts and msg_ts < self._skip_before_ts:
                        logger.debug("Skipping stale update %d (ts=%d < %d)", uid, msg_ts, self._skip_before_ts)
                        continue
                    utype = next((k for k in update if k != "update_id"), "unknown")
                    logger.debug("Update %d type=%s", uid, utype)
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
