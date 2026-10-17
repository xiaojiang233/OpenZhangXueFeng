"""
Fish Audio TTS + ASR client.

TTS: HTTP API per-sentence for streaming voice generation.
ASR: HTTP API for speech-to-text transcription.

API reference: https://docs.fish.audio
"""

import httpx
import msgpack

from config import (
    FISH_AUDIO_API_KEY,
    FISH_AUDIO_VOICE_ID,
    FISH_AUDIO_LATENCY,
    Settings,
)


FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_AUDIO_ASR_URL = "https://api.fish.audio/v1/asr"


# ═══════════════════════════════════════════════════════════
# TTS
# ═══════════════════════════════════════════════════════════

async def tts_sentence(text: str, *, settings: Settings | None = None) -> bytes:
    """
    Convert a single sentence to speech via Fish Audio HTTP API.

    IMPORTANT: Caller should strip markdown from `text` before calling.
    Returns PCM 44100Hz Int16 mono, the browser audio transport contract.
    """
    if not text.strip():
        return b""

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            FISH_AUDIO_TTS_URL,
            headers={
                "Authorization": f"Bearer {settings.fish_audio_api_key if settings else FISH_AUDIO_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "reference_id": settings.fish_audio_voice_id if settings else FISH_AUDIO_VOICE_ID,
                "format": "pcm",
                "sample_rate": 44100,
                "latency": settings.fish_audio_latency if settings else FISH_AUDIO_LATENCY,
            },
        )
        response.raise_for_status()
        return response.content


# ═══════════════════════════════════════════════════════════
# ASR (Speech-to-Text)
# ═══════════════════════════════════════════════════════════

async def transcribe_audio(
    audio_bytes: bytes,
    language: str | None = None,
    ignore_timestamps: bool = True,
    *,
    settings: Settings | None = None,
) -> dict:
    """
    Transcribe audio to text using Fish Audio ASR.

    API: POST /v1/asr  (multipart/form-data)

    Args:
        audio_bytes: Raw audio data (WAV, MP3, WebM, etc.)
        language: Optional language hint (e.g. "zh", "en"). None for auto-detect.
        ignore_timestamps: Skip precise timestamps for lower latency (default True).

    Returns:
        Dict with keys: text (str), duration (float), segments (list).
    """
    if not audio_bytes:
        return {"text": "", "duration": 0, "segments": []}

    # Fish Audio ASR only accepts application/form-data or application/msgpack.
    # We use msgpack (binary serialization) for reliability.
    payload: dict = {
        "audio": audio_bytes,
        "ignore_timestamps": ignore_timestamps,
    }
    if language:
        payload["language"] = language

    packed = msgpack.packb(payload, use_bin_type=True)

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            FISH_AUDIO_ASR_URL,
            headers={
                "Authorization": f"Bearer {settings.fish_audio_api_key if settings else FISH_AUDIO_API_KEY}",
                "Content-Type": "application/msgpack",
            },
            content=packed,
        )
        response.raise_for_status()
        return response.json()
