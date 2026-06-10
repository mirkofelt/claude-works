import logging

import httpx

logger = logging.getLogger(__name__)

_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


async def transcribe(api_key: str, audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    """Transcribe audio via ElevenLabs Speech-to-Text. Returns text or None on failure."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _STT_URL,
                headers={"xi-api-key": api_key},
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={"model_id": "scribe_v1"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip() or None
    except Exception as e:
        logger.warning("Transcription failed: %s", e)
        return None
