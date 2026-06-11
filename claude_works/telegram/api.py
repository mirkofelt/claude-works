import logging
import httpx
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PARAMS = {"chat_id", "message_id", "action", "offset", "timeout", "file_id"}


class TelegramAPI:
    BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        safe = {k: v for k, v in params.items() if k in _LOG_PARAMS and v is not None}
        logger.debug("TG -> %s %s", method, safe)
        url = self.BASE.format(token=self._token, method=method)
        try:
            resp = await self._client.post(url, json={k: v for k, v in params.items() if v is not None})
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
            logger.debug("TG <- %s ok", method)
            return data["result"]
        except Exception as e:
            logger.error("TG %s failed: %s", method, e)
            raise

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return await self._call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=allowed_updates or ["message", "edited_message", "message_reaction", "callback_query"],
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        return await self._call("sendChatAction", chat_id=chat_id, action=action)

    async def set_message_reaction(
        self, chat_id: int, message_id: int, emoji: str | None
    ) -> bool:
        reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
        return await self._call(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )

    async def send_voice(self, chat_id: int, voice: bytes, caption: str | None = None) -> dict[str, Any]:
        url = self.BASE.format(token=self._token, method="sendVoice")
        files = {"voice": ("voice.ogg", voice, "audio/ogg")}
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        resp = await self._client.post(url, data=data, files=files)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result.get('description', 'unknown')}")
        return result["result"]

    async def get_file(self, file_id: str) -> bytes:
        meta = await self._call("getFile", file_id=file_id)
        file_path = meta["file_path"]
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
        remove_keyboard: bool = False,
    ) -> dict[str, Any]:
        markup = {"inline_keyboard": []} if remove_keyboard else reply_markup
        return await self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=markup,
        )

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        await self._call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
        )
        return True

    async def send_location(
        self,
        chat_id: int,
        latitude: float,
        longitude: float,
        title: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        if title:
            return await self._call(
                "sendVenue",
                chat_id=chat_id,
                latitude=latitude,
                longitude=longitude,
                title=title,
                address=" ",
                reply_markup=reply_markup,
            )
        return await self._call(
            "sendLocation",
            chat_id=chat_id,
            latitude=latitude,
            longitude=longitude,
            reply_markup=reply_markup,
        )
