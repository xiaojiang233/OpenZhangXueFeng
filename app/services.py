"""Injectable provider functions keep HTTP and WebSocket flows testable offline."""

from dataclasses import dataclass
from typing import Callable

from config import Settings


@dataclass
class ProviderServices:
    settings: Settings
    chat_stream: Callable | None = None
    search: Callable | None = None
    tts: Callable | None = None
    transcribe: Callable | None = None

    def __post_init__(self):
        self._overrides = {name: getattr(self, name) for name in ("chat_stream", "search", "tts", "transcribe")
                           if getattr(self, name) is not None}
        if self.chat_stream is None:
            from deepseek_client import chat_stream_agent

            def stream(messages, search_func, citations_out, **kwargs):
                return chat_stream_agent(messages, search_func, citations_out,
                                         settings=self.settings,
                                         search_enabled=self.settings.services["search"], **kwargs)

            self.chat_stream = stream
        if self.search is None:
            from langsearch_client import search

            async def configured_search(query):
                return await search(query, settings=self.settings)

            self.search = configured_search
        if self.tts is None or self.transcribe is None:
            from fish_audio_client import transcribe_audio, tts_sentence

            async def configured_tts(text):
                return await tts_sentence(text, settings=self.settings)

            async def configured_transcribe(audio):
                return await transcribe_audio(audio, settings=self.settings)

            self.tts = self.tts or configured_tts
            self.transcribe = self.transcribe or configured_transcribe

    @property
    def readiness(self) -> dict[str, bool]:
        return self.settings.services

    def reconfigure(self, settings: Settings):
        """New provider closures use new credentials; existing in-flight calls keep their snapshot."""
        return ProviderServices(settings, **self._overrides)
