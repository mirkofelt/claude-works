import asyncio
import logging
import os
import time
from typing import Callable, Awaitable

from .api import TelegramAPI

logger = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], Awaitable[None]]

_ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "callback_query"]
_OFFSET_FILE = os.environ.get("TELEGRAM_OFFSET_FILE", "/data/telegram_offset")


class TelegramPoller:
    def __init__(self, api: TelegramAPI, on_update: UpdateHandler, skip_before_ts: int | None = None) -> None:
        self._api = api
        self._on_update = on_update
        self._offset: int = self._load_offset()
        self._running = False
        self._task: asyncio.Task | None = None
        self._skip_before_ts: int = skip_before_ts if skip_before_ts is not None else int(time.time())

    def _load_offset(self) -> int:
        try:
            with open(_OFFSET_FILE) as f:
                return int(f.read().strip())
        except Exception:
            return 0

    def _persist_offset(self) -> None:
        try:
            with open(_OFFSET_FILE, "w") as f:
                f.write(str(self._offset))
        except Exception:
            pass

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poller")
        logger.info("Poller started (offset=%d, skipping messages before ts=%d)", self._offset, self._skip_before_ts)

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
                    # Extract timestamp — callback_query uses its nested message's date
                    if "callback_query" in update:
                        msg_ts = (update["callback_query"].get("message") or {}).get("date", 0)
                    else:
                        msg_ts = (update.get("message") or update.get("edited_message") or {}).get("date", 0)
                    if msg_ts and msg_ts <= self._skip_before_ts:
                        logger.debug("Skipping stale update %d (ts=%d <= %d)", uid, msg_ts, self._skip_before_ts)
                        continue
                    utype = next((k for k in update if k != "update_id"), "unknown")
                    logger.debug("Update %d type=%s", uid, utype)
                    asyncio.create_task(
                        self._dispatch(update),
                        name=f"update-{uid}",
                    )
                if updates:
                    self._persist_offset()
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
