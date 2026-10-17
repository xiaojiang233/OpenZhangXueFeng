"""Typed application settings, with optional independently configured providers."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    fish_audio_api_key: str = field(default="", repr=False)
    fish_audio_voice_id: str = "802e3bc2b27e49c2995d23ef70e6ac89"
    fish_audio_latency: str = "balanced"
    fish_audio_format: str = "pcm"
    langsearch_api_key: str = field(default="", repr=False)
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: Path = PROJECT_DIR / "data"
    max_message_chars: int = 12000
    max_upload_bytes: int = 20 * 1024 * 1024
    max_history_messages: int = 80
    generation_timeout_seconds: int = 180

    def __post_init__(self):
        limits = {
            "port": (1, 65535), "max_message_chars": (1, 100000),
            "max_upload_bytes": (1024, 100 * 1024 * 1024),
            "max_history_messages": (2, 1000),
            "generation_timeout_seconds": (5, 1800),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        url = urlsplit(self.deepseek_base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("DEEPSEEK_BASE_URL must be an HTTP(S) URL without embedded credentials")
        if not self.deepseek_model.strip():
            raise ValueError("DEEPSEEK_MODEL cannot be empty")
        if self.fish_audio_format != "pcm":
            raise ValueError("FISH_AUDIO_FORMAT must be pcm for browser audio playback")
        if self.fish_audio_latency not in {"normal", "balanced", "low"}:
            raise ValueError("FISH_AUDIO_LATENCY must be normal, balanced or low")
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())

    @classmethod
    def from_env(cls):
        return cls(
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro").strip(),
            fish_audio_api_key=os.getenv("FISH_AUDIO_API_KEY", "").strip(),
            fish_audio_voice_id=os.getenv("FISH_AUDIO_VOICE_ID", "802e3bc2b27e49c2995d23ef70e6ac89").strip(),
            fish_audio_latency=os.getenv("FISH_AUDIO_LATENCY", "balanced").strip(),
            fish_audio_format=os.getenv("FISH_AUDIO_FORMAT", "pcm").strip(),
            langsearch_api_key=os.getenv("LANGSEARCH_API_KEY", "").strip(),
            host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_integer("PORT", 8000, 1, 65535),
            data_dir=Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))),
            max_message_chars=_integer("MAX_MESSAGE_CHARS", 12000, 1, 100000),
            max_upload_bytes=_integer("MAX_UPLOAD_BYTES", 20 * 1024 * 1024, 1024, 100 * 1024 * 1024),
            max_history_messages=_integer("MAX_HISTORY_MESSAGES", 80, 2, 1000),
            generation_timeout_seconds=_integer("GENERATION_TIMEOUT_SECONDS", 180, 5, 1800),
        )

    @property
    def services(self) -> dict[str, bool]:
        return {"chat": bool(self.deepseek_api_key), "voice": bool(self.fish_audio_api_key),
                "search": bool(self.langsearch_api_key)}


settings = Settings.from_env()

# Keep the provider modules' public imports compatible.
DEEPSEEK_API_KEY = settings.deepseek_api_key
DEEPSEEK_BASE_URL = settings.deepseek_base_url
DEEPSEEK_MODEL = settings.deepseek_model
FISH_AUDIO_API_KEY = settings.fish_audio_api_key
FISH_AUDIO_VOICE_ID = settings.fish_audio_voice_id
FISH_AUDIO_LATENCY = settings.fish_audio_latency
FISH_AUDIO_FORMAT = settings.fish_audio_format
LANGSEARCH_API_KEY = settings.langsearch_api_key
HOST = settings.host
PORT = settings.port


def validate() -> dict[str, bool]:
    """Return readiness. Missing optional provider keys never prevent startup."""
    return settings.services
