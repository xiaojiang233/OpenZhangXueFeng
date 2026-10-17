"""
Sentence buffer — accumulates LLM token stream and yields complete sentences
for natural-sounding TTS input.
"""

import re
from typing import AsyncGenerator


# Sentence-ending characters for Chinese and English
SENTENCE_ENDINGS = re.compile(r"[。！？.!?\n]")


class SentenceBuffer:
    """
    Buffers streaming text and yields complete sentences.

    Splits on Chinese/English sentence-ending punctuation.
    Falls back to yielding buffered text if it exceeds max_length
    (prevents unbounded buffering during long run-on output).
    """

    def __init__(self, min_length: int = 4, max_length: int = 200):
        if min_length < 1 or max_length < min_length:
            raise ValueError("Require 1 <= min_length <= max_length")
        self.min_length = min_length
        self.max_length = max_length
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Feed new text, return list of complete sentences."""
        self._buffer += text
        sentences = []
        while self._buffer:
            end_idx = next((m.end() for m in SENTENCE_ENDINGS.finditer(self._buffer)
                            if len(self._buffer[:m.end()].strip()) >= self.min_length), None)
            if end_idx is None and len(self._buffer) < self.max_length:
                break
            end_idx = min(end_idx or self.max_length, self.max_length)
            sentence = self._buffer[:end_idx].strip()
            self._buffer = self._buffer[end_idx:]
            if sentence:
                sentences.append(sentence)

        return sentences

    def flush(self) -> str:
        """Return and clear remaining buffer content."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining

    def reset(self) -> None:
        """Clear buffer state for a new conversation turn."""
        self._buffer = ""


async def sentence_stream(
    text_generator: AsyncGenerator[str, None],
    min_length: int = 4,
    max_length: int = 200,
) -> AsyncGenerator[str, None]:
    """
    Wraps a token-level text generator and yields complete sentences.

    Usage:
        async for sentence in sentence_stream(llm_chat_stream()):
            audio = await tts(sentence)
    """
    buffer = SentenceBuffer(min_length=min_length, max_length=max_length)
    async for chunk in text_generator:
        for sentence in buffer.feed(chunk):
            yield sentence

    remaining = buffer.flush()
    if remaining:
        yield remaining
