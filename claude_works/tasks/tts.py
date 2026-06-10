import io
import logging

import httpx

logger = logging.getLogger(__name__)


async def synthesize(text: str, cfg: dict) -> bytes | None:
    """Synthesize text to MP3 audio bytes. Returns None on failure."""
    provider = cfg.get("provider", "gtts")
    if provider == "elevenlabs":
        return await _elevenlabs(text, cfg)
    return _gtts(text, cfg)


def _gtts(text: str, cfg: dict) -> bytes | None:
    try:
        from gtts import gTTS
        lang = cfg.get("language", "de")
        tts = gTTS(text=text, lang=lang)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning("gtts failed: %s", e)
        return None


async def _elevenlabs(text: str, cfg: dict) -> bytes | None:
    api_key = cfg.get("elevenlabs_api_key", "")
    voice_id = cfg.get("elevenlabs_voice_id", "")
    if not api_key or not voice_id:
        logger.warning("ElevenLabs TTS: elevenlabs_api_key and elevenlabs_voice_id required in tts config")
        return None
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
            resp.raise_for_status()
            return resp.content
    except Exception as e:
        logger.warning("ElevenLabs TTS failed: %s", e)
        return None
