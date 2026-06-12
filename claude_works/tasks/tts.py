import io
import logging

import httpx

logger = logging.getLogger(__name__)


async def synthesize(text: str, cfg: dict) -> "tuple[bytes | None, str | None]":
    """Synthesize text to MP3 audio bytes. Returns (audio_bytes, error_reason)."""
    provider = cfg.get("provider", "gtts")
    if provider == "elevenlabs":
        return await _elevenlabs(text, cfg)
    return _gtts(text, cfg)


def _gtts(text: str, cfg: dict) -> "tuple[bytes | None, str | None]":
    try:
        from gtts import gTTS
        lang = cfg.get("language", "de")
        tts = gTTS(text=text, lang=lang)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue(), None
    except Exception as e:
        logger.warning("gtts failed: %s", e)
        return None, f"gTTS error: {e}"


_ELEVENLABS_ERRORS = {
    401: "ElevenLabs: Ungültiger API-Key oder Guthaben aufgebraucht. Bitte Key/Credits prüfen.",
    403: "ElevenLabs: Zugriff verweigert (falscher Key oder Plan).",
    404: "ElevenLabs: Voice ID nicht gefunden. Voice ID in Settings prüfen.",
    422: "ElevenLabs: Ungültige Anfrage (Text zu lang oder Modell unbekannt).",
    429: "ElevenLabs: Rate Limit erreicht. Kurz warten und nochmal versuchen.",
}


async def _elevenlabs(text: str, cfg: dict) -> "tuple[bytes | None, str | None]":
    api_key = cfg.get("elevenlabs_api_key", "")
    voice_id = cfg.get("elevenlabs_voice_id", "")
    if not api_key or not voice_id:
        msg = "ElevenLabs: API-Key oder Voice ID fehlen (Settings → TTS)."
        logger.warning(msg)
        return None, msg
    model = cfg.get("elevenlabs_model", "eleven_multilingual_v2")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
            )
            if resp.status_code != 200:
                user_msg = _ELEVENLABS_ERRORS.get(
                    resp.status_code,
                    f"ElevenLabs: HTTP {resp.status_code} — {resp.text[:100]}",
                )
                logger.warning("ElevenLabs TTS failed: %s", user_msg)
                return None, user_msg
            return resp.content, None
    except Exception as e:
        msg = f"ElevenLabs: Verbindungsfehler — {e}"
        logger.warning(msg)
        return None, msg
