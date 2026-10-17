"""
LangSearch web search client.

API reference: https://docs.langsearch.com/
"""

import httpx
from urllib.parse import urlsplit
from config import LANGSEARCH_API_KEY, Settings


LANGSEARCH_URL = "https://api.langsearch.com/v1/web-search"


async def search(
    query: str,
    freshness: str = "noLimit",
    count: int = 10,
    summary: bool = True,
    *,
    settings: Settings | None = None,
) -> tuple[str, list[dict]]:
    """
    Perform a web search via LangSearch and return formatted results.

    Returns:
        Tuple of (formatted_context_text, citations_list).
        citations_list is a list of {"title": ..., "url": ..., "snippet": ...}.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            LANGSEARCH_URL,
            headers={
                "Authorization": f"Bearer {settings.langsearch_api_key if settings else LANGSEARCH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "freshness": freshness,
                "summary": summary,
                "count": count,
            },
        )
        response.raise_for_status()
        data = response.json()

    return _format_results(data)


def _format_results(data: dict) -> tuple[str, list[dict]]:
    """Parse LangSearch response into LLM-friendly text + citations list."""
    parts = []
    citations = []

    # AI summary (if available)
    summary = data.get("summary", "")
    if summary:
        parts.append(f"摘要：{summary}")

    # LangSearch wraps results in data.data.webPages.value
    inner = data.get("data") or {}
    if not isinstance(inner, dict):
        inner = {}
    web_pages = inner.get("webPages") or {}
    results = web_pages.get("value") if isinstance(web_pages, dict) else []
    # Fallback: try other common nesting patterns
    if not results:
        results = inner.get("results") or data.get("results") or []

    for item in results:
        if isinstance(item, dict):
            title = item.get("title") or item.get("name", "")
            url = item.get("url") or item.get("link", "")
            snippet = item.get("snippet") or item.get("summary") or item.get("description", "")
            if not isinstance(url, str):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    continue
            except ValueError:
                continue
            title = str(title or parsed.netloc)
            snippet = str(snippet or "")
            i = len(citations) + 1
            parts.append(f"[{i}] {title}\n    URL: {url}\n    摘要: {snippet}")
            citations.append({"title": title, "url": url, "snippet": snippet})

    if not parts:
        return "（未找到搜索结果）", []

    return "\n\n".join(parts), citations
