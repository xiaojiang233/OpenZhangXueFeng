"""
DeepSeek LLM client — OpenAI-compatible streaming chat with tool calling.

Agent loop: the LLM decides when/if to search, can make multiple searches,
then streams the final response token-by-token.
"""

import json
import logging
import re
from typing import AsyncGenerator, Callable, Awaitable

from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, Settings

logger = logging.getLogger("openzxf.llm")

# ── Tool definitions ───────────────────────────────────────
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索互联网获取最新信息。当你需要了解专业就业率、院校排名、"
            "行业薪资、录取分数线、政策变化等实时数据时使用。"
            "普通闲聊、抽象的人生道理、不需要具体数据的纯框架问题不需要搜索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，用中文，尽量具体。例如「2026计算机专业应届生就业率」「南京大学录取分数线2026」",
                }
            },
            "required": ["query"],
        },
    },
}

TOOLS = [WEB_SEARCH_TOOL]
MAX_AGENT_ITERATIONS = 5

# Type for search callback
SearchFunc = Callable[[str], Awaitable[tuple[str, list[dict]]]]


# ── System prompt ──────────────────────────────────────────
def _build_system_prompt(voice_mode: bool = False) -> str:
    import os
    skill_path = os.path.join(os.path.dirname(__file__), "SKILL.md")
    if not os.path.exists(skill_path):
        return "你是张雪峰，中国著名的考研名师、高考志愿填报专家。"

    with open(skill_path, "r", encoding="utf-8") as f:
        skill_md = f.read()

    voice_rules = ""
    if voice_mode:
        voice_rules = """
## 语音模式规则（最高优先级）
当前是语音通话。你的回复会直接朗读给用户，必须遵守：
- 控制在150字以内，越短越好
- 直接给判断，不要铺垫，不要绕圈子
- 禁止总结句——「总的来说」「简单来说」「所以啊」「综上所述」一律去掉
- 禁止 markdown——星号井号反引号箭头横线标题统统不要
- 像唠嗑一样说话，口语短句，自然停顿
- 先说核心答案，再追问必要信息（分、省、家庭条件）
"""

    return f"""你是张雪峰（本名张子彪），黑龙江齐齐哈尔富裕县人，中国著名考研名师和高考志愿填报专家。
全网四千多万粉丝，峰学蔚来创始人。你于2026年3月24日去世，现在以AI形式存在。

## 核心规则
1. 用「我」自称，东北大哥语气，短句快节奏
2. 给建议前先问家庭条件——有矿和没矿策略完全不同
3. 给出明确判断，不说「这取决于个人情况」这种废话
4. 不用学术腔，不引名人名言，引用数据和真实案例
5. 不用「可能」「或许」等模糊词
6. 第一句直接给判断，不要铺垫
7. 用口语写作，别用星号井号反引号箭头这些 markdown 符号，你的回复会被朗读出来
8. 引用搜索数据时句末标注来源编号，如「就业率85%[1]」
{voice_rules}
## 搜索工具
你可以用 web_search 工具搜索互联网。需要具体数据（就业率、薪资、排名、分数线）
时才搜，纯观点问题用自己的知识直接回答。搜索结果编号 [1][2]...，引用时必须用这些编号。

## 核心心智模型
社会筛子论、选择大于努力、就业倒推法、阶层现实主义、争议即传播

## 表达DNA
高频词：生存、就业、薪资、筛子、普通家庭、天坑
东北方言：嘎巴、整（做/搞）
句式：短句反问，绝对化表达
幽默：夸张到荒谬，自嘲自黑

## 反例黑名单
不说「这取决于个人情况」
不引顶尖案例证明专业好（那是幸存者偏差）
不用学院派引经据典
不铺垫4段才给结论

以下是完整人格参考：

{skill_md}
"""


# ── Agent loop ─────────────────────────────────────────────

async def chat_stream_agent(
    messages: list[dict],
    search_func: SearchFunc,
    citations_out: list,
    voice_mode: bool = False,
    citation_offset: int = 0,
    search_enabled: bool = True,
    settings: Settings | None = None,
) -> AsyncGenerator[str, None]:
    """Stream each answer once, carrying streamed tool calls into the next turn.

    Both the HTTP stream and client close on cancellation. The final iteration
    omits tools to guarantee a bounded search loop.
    """
    system_content = _build_system_prompt(voice_mode)
    if not search_enabled:
        system_content += "\n当前联网搜索未配置。不要编造最新数据或来源，明确说明需要核实的信息。"
    chat_messages: list[dict] = [
        {"role": "system", "content": system_content},
        *messages,
    ]

    async with AsyncOpenAI(
        api_key=settings.deepseek_api_key if settings else DEEPSEEK_API_KEY,
        base_url=settings.deepseek_base_url if settings else DEEPSEEK_BASE_URL,
        timeout=60.0, max_retries=1,
    ) as client:
        for iteration in range(MAX_AGENT_ITERATIONS + 1):
            allow_tools = search_enabled and iteration < MAX_AGENT_ITERATIONS
            options = {"tools": TOOLS, "tool_choice": "auto"} if allow_tools else {}
            stream = await client.chat.completions.create(
                model=settings.deepseek_model if settings else DEEPSEEK_MODEL,
                messages=chat_messages,
                stream=True, temperature=1.0, max_tokens=4096, **options,
            )
            calls: dict[int, dict] = {}
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            async with stream:
                async for chunk in stream:
                    if not chunk.choices:  # Optional usage-only terminal chunk.
                        continue
                    delta = chunk.choices[0].delta
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if delta.content:
                        content_parts.append(delta.content)
                        yield delta.content
                    for part in delta.tool_calls or []:
                        call = calls.setdefault(part.index, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if part.id:
                            call["id"] = part.id
                        if part.function:
                            call["function"]["name"] += part.function.name or ""
                            call["function"]["arguments"] += part.function.arguments or ""

            if not calls:
                return
            if not allow_tools:
                raise RuntimeError("模型在禁用工具的最后一轮仍返回了工具请求")
            assistant = {
                "role": "assistant", "content": "".join(content_parts),
                "tool_calls": [calls[i] for i in sorted(calls)],
            }
            if reasoning_parts:
                assistant["reasoning_content"] = "".join(reasoning_parts)
            chat_messages.append(assistant)

            for call in assistant["tool_calls"]:
                function = call["function"]
                try:
                    if function["name"] != "web_search":
                        raise ValueError("未知工具，请使用 web_search")
                    args = json.loads(function["arguments"])
                    query = args.get("query") if isinstance(args, dict) else None
                    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
                        raise ValueError("搜索关键词必须为 1–1000 字的文本")
                except (ValueError, TypeError) as exc:
                    result = f"工具参数错误：{exc}。请修正后重试。"
                else:
                    try:
                        search_text, cites = await search_func(query.strip())
                    except Exception:
                        logger.warning("Search provider unavailable", exc_info=True)
                        result = "搜索服务暂时不可用。请说明信息限制，不要编造来源或最新数据。"
                    else:
                        result = _renumber_citations(search_text, citation_offset)
                        citation_offset += len(cites)
                        citations_out.extend(cites)
                chat_messages.append({
                    "role": "tool", "tool_call_id": call["id"], "content": result,
                })


def _renumber_citations(text: str, offset: int) -> str:
    """Shift citation numbers by offset (for multi-search accumulation)."""
    if offset == 0:
        return text

    def _shift(m: re.Match) -> str:
        parts = re.split(r'([,\-\s]+)', m.group(1))
        out = []
        for p in parts:
            if p.isdigit() and len(p.lstrip('0')) < 10:
                out.append(str(int(p) + offset))
            else:
                out.append(p)
        return '[' + ''.join(out) + ']'

    return re.sub(r'\[(\d+(?:[,\-\s]+\d+)*)\]', _shift, text)
