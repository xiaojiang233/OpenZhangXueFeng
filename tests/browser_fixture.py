"""Browser-test app with explicit fake providers and disposable conversation data.

Run only for UI checks: uv run uvicorn tests.browser_fixture:app --port 8765
"""

import asyncio
import atexit
import tempfile
from pathlib import Path

from app import create_app
from app import api_settings
from app.services import ProviderServices
from config import Settings
from storage import ConversationStore

_directory = tempfile.TemporaryDirectory(prefix="ozxf-browser-")
atexit.register(_directory.cleanup)
settings = Settings(data_dir=Path(_directory.name), deepseek_api_key="test-only",
                    fish_audio_api_key="test-only", langsearch_api_key="test-only")


async def chat(messages, search, citations, **kwargs):
    question = messages[-1]["content"]
    if "测试慢回复" in question:
        yield "正在梳理你的问题。"
        await asyncio.sleep(60)
        yield "这个迟到的结果不应出现。"
        return
    await search(question)
    for part in ["先看你的兴趣与能力。\n\n", "## 选择思路\n", "- 结合所在省份和成绩区间。\n",
                 "- 比较课程、城市与就业方向。\n\n", "参考学校的官方说明[1]。"]:
        yield part
        await asyncio.sleep(0.04)


async def search(query):
    return "[1] 院校信息", [{"title": "院校官方信息", "url": "https://example.com/admissions", "snippet": "测试来源"}]


async def tts(text):
    return b"\0\0" * 4410


async def transcribe(audio):
    return {"text": "请帮我规划职业方向", "duration": 1.0}


async def test_connection(service, settings):
    """Explicit fake, so browser interaction tests never call paid providers."""
    if not settings.services[service]:
        raise api_settings.ProbeError("请先填写此服务的 API Key。")


api_settings.probe_service = test_connection


app = create_app(settings, ConversationStore(settings.data_dir),
                 ProviderServices(settings, chat_stream=chat, search=search,
                                  tts=tts, transcribe=transcribe))
