"""
TTS text utilities — strip markdown formatting for clean speech output.

Applied to LLM output before sending to Fish Audio TTS,
so the spoken version sounds natural without markdown artifacts.
"""

import re


def strip_markdown(text: str) -> str:
    """
    Remove markdown formatting that sounds bad when spoken.

    Preserves the meaning while removing visual-only formatting:
    - **bold** → bold
    - *italic* → italic
    - `code`  → code
    - ~~strike~~ → strike
    - ### headings → headings
    - [text](url) → text
    - > quotes → quotes
    - --- dividers → removed
    - Numbered/bullet lists → natural text
    """

    # 1. Remove images: ![alt](url)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)

    # 2. Convert links: [text](url) → text
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)

    # 3. Remove bold/italic markers: **, *, __, _
    # Bold: **text** or __text__
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    # Italic: *text* or _text_ (but not inside words with underscores like variable_names)
    text = re.sub(r'(?<!\w)\*([^*]+)\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', text)

    # 4. Remove inline code backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # 5. Remove strikethrough: ~~text~~
    text = re.sub(r'~~([^~]+)~~', r'\1', text)

    # 6. Remove heading markers: ###, ##, #
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    # 7. Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

    # 8. Remove blockquote markers
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)

    # 9. Convert numbered lists: "1. item" → "1、item"
    text = re.sub(r'^(\d+)\.\s+', r'\1、', text, flags=re.MULTILINE)

    # 10. Convert bullet lists: "- item" or "* item" → "· item"
    text = re.sub(r'^[-*]\s+', '· ', text, flags=re.MULTILINE)

    # 11. Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 12. Remove leading/trailing whitespace
    text = text.strip()

    return text


def _parse_cited_numbers(text: str, limit: int = 1000) -> set[int]:
    """Extract all unique citation numbers from text like '...[1]...[2,3]...'"""
    nums = set()
    def bounded_number(value: str) -> int:
        value = value.lstrip('0') or '0'
        return limit + 1 if len(value) > len(str(limit)) else int(value)

    for m in re.finditer(r'\[(\d+(?:[,\-\s]+\d+)*)\]', text):
        for part in re.finditer(r'(\d+)(?:\s*-\s*(\d+))?', m.group(1)):
            first = bounded_number(part.group(1))
            last = bounded_number(part.group(2)) if part.group(2) else first
            nums.update(range(max(1, first), min(limit, last) + 1))
    return nums


def filter_citations(full_response: str, citations: list[dict]) -> tuple[str, list[dict]]:
    """
    Keep only citations the LLM actually referenced.
    Preserves original numbering so badge [N] still maps to original position.

    Returns (unchanged_text, filtered_citations_with_gaps).
    """
    if not citations:
        return full_response, []

    cited = _parse_cited_numbers(full_response, len(citations))
    if not cited:
        return full_response, []

    # Filter to cited entries; keep original indices (1-based)
    max_idx = min(max(cited), len(citations))
    filtered = []
    for i in range(max_idx):
        c = citations[i] if i < len(citations) else None
        filtered.append(c if (i + 1) in cited else None)

    # Trim trailing Nones
    while filtered and filtered[-1] is None:
        filtered.pop()

    return full_response, filtered


def strip_citations(text: str) -> str:
    """
    Remove citation markers like [1], [2,3], [1-3] from TTS text.
    These are visual-only and sound confusing when read aloud.
    """
    text = re.sub(r'\s*\[[\d,\-\s]+\]', '', text)
    return text


def strip_for_tts(text: str) -> str:
    """
    Full TTS cleaning pipeline: markdown + citations.
    """
    text = strip_markdown(text)
    text = strip_citations(text)
    return text


def strip_light(text: str) -> str:
    """
    Light markdown stripping — only remove the most egregious TTS artifacts.
    Preserves more formatting than strip_markdown().
    """
    # Remove bold markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    # Remove inline code
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Remove heading markers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

    return text.strip()
