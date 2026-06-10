import logging
import httpx

logger = logging.getLogger(__name__)


async def transcribe(api_key: str, audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    """Transcribe audio via OpenAI Whisper API. Returns text or None on failure."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={"model": "whisper-1"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip() or None
    except Exception as e:
        logger.warning("Transcription failed: %s", e)
        return None
