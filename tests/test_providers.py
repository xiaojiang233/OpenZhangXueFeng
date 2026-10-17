"""Provider and speech regressions, exercised without network credentials."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

import deepseek_client
from langsearch_client import _format_results
from sentence_buffer import SentenceBuffer, sentence_stream
from tts_utils import filter_citations, strip_for_tts


def chunk(content=None, *, tools=None, reasoning=None, usage_only=False):
    if usage_only:
        return SimpleNamespace(choices=[])
    delta = SimpleNamespace(
        content=content, tool_calls=tools, reasoning_content=reasoning,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def tool_part(index=0, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeStream:
    def __init__(self, chunks, *, block_after=False):
        self.chunks = chunks
        self.block_after = block_after
        self.blocked = asyncio.Event()
        self.entered = False
        self.closed = False
        self.exit_type = None

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True
        self.exit_type = exc_type

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for item in self.chunks:
            if isinstance(item, BaseException):
                raise item
            yield item
        if self.block_after:
            self.blocked.set()
            await asyncio.Event().wait()


class FakeClient:
    def __init__(self, streams):
        self.streams = streams
        self.requests = []
        self.entered = False
        self.closed = False
        self.exit_type = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True
        self.exit_type = exc_type

    async def create(self, **options):
        # The real SDK serializes each request before later agent iterations.
        self.requests.append(deepcopy(options))
        index = len(self.requests) - 1
        if index >= len(self.streams):
            raise AssertionError("Unexpected extra LLM request")
        return self.streams[index]


@pytest.fixture
def provider(monkeypatch):
    def install(*streams):
        client = FakeClient(list(streams))
        monkeypatch.setattr(deepseek_client, "AsyncOpenAI", lambda **kwargs: client)
        return client

    return install


async def no_search(query):
    raise AssertionError(f"Unexpected search: {query}")


async def collect(*args, **kwargs):
    return [piece async for piece in deepseek_client.chat_stream_agent(*args, **kwargs)]


def test_answer_is_streamed_once_and_accepts_usage_only_chunks(provider):
    stream = FakeStream([
        chunk(usage_only=True), chunk("先看就业"), chunk(), chunk("，再选专业。"),
        chunk(usage_only=True),
    ])
    client = provider(stream)
    messages = [{"role": "user", "content": "怎么选专业？"}]
    citations = []

    result = asyncio.run(collect(messages, no_search, citations))

    assert result == ["先看就业", "，再选专业。"]
    assert len(client.requests) == 1
    assert client.requests[0]["stream"] is True
    assert client.requests[0]["messages"][-1] == messages[0]
    assert messages == [{"role": "user", "content": "怎么选专业？"}]
    assert citations == []
    assert stream.entered and stream.closed
    assert client.entered and client.closed


def test_streamed_tool_fragments_keep_call_order_and_offset_citations(provider):
    tools = FakeStream([
        chunk(reasoning="检索", tools=[
            tool_part(1, "call_b", "web_", '{"quer'),
            tool_part(0, "call_a", "web_", '{"query":"就业'),
        ]),
        chunk(reasoning="计划", tools=[
            tool_part(0, name="search", arguments='率"}'),
            tool_part(1, name="search", arguments='y":"薪资"}'),
        ]),
        chunk(usage_only=True),
    ])
    answer = FakeStream([chunk("建议[4]，薪资[6]。")])
    client = provider(tools, answer)
    searched = []
    first = {"title": "就业 A", "url": "https://example.edu/a", "snippet": "A"}
    second = {"title": "就业 B", "url": "https://example.edu/b", "snippet": "B"}
    third = {"title": "薪资", "url": "https://example.edu/c", "snippet": "C"}

    async def search(query):
        searched.append(query)
        if query == "就业率":
            return "[1] A\n[2] B\n共同结论[1, 2]", [first, second]
        return "[1] C", [third]

    citations = []
    result = asyncio.run(collect([], search, citations, citation_offset=3))

    assert result == ["建议[4]，薪资[6]。"]
    assert searched == ["就业率", "薪资"]
    assert citations == [first, second, third]
    assert len(client.requests) == 2
    second_request = client.requests[1]["messages"]
    assistant = next(message for message in second_request if message["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == ["call_a", "call_b"]
    assert assistant["reasoning_content"] == "检索计划"
    tool_messages = [message for message in second_request if message["role"] == "tool"]
    assert [(message["tool_call_id"], message["content"]) for message in tool_messages] == [
        ("call_a", "[4] A\n[5] B\n共同结论[4, 5]"),
        ("call_b", "[6] C"),
    ]
    assert tools.closed and answer.closed and client.closed


@pytest.mark.parametrize(("name", "arguments"), [
    ("unsupported", '{"query":"就业"}'),
    ("web_search", "{broken"),
    ("web_search", "null"),
    ("web_search", "[]"),
    ("web_search", '{"query":42}'),
    ("web_search", '{"query":"  "}'),
    ("web_search", '{"query":"' + "长" * 1001 + '"}'),
])
def test_invalid_tools_receive_matching_error_response(provider, name, arguments):
    stream = FakeStream([chunk(tools=[tool_part(0, "invalid_call", name, arguments)])])
    client = provider(stream, FakeStream([chunk("请补充查询条件。")]))
    citations = []

    result = asyncio.run(collect([], no_search, citations))

    assert result == ["请补充查询条件。"]
    assert len(client.requests) == 2
    message = client.requests[1]["messages"][-1]
    assert message["role"] == "tool"
    assert message["tool_call_id"] == "invalid_call"
    assert "工具参数错误" in message["content"]
    assert citations == []
    assert client.closed and stream.closed


def test_search_failure_returns_information_limit_to_model(provider):
    tools = FakeStream([chunk(tools=[
        tool_part(0, "search_call", "web_search", '{"query":"分数线"}'),
    ])])
    client = provider(tools, FakeStream([chunk("最新分数线需要核实。")]))
    citations = []

    async def failed_search(query):
        raise RuntimeError("private upstream credential detail")

    result = asyncio.run(collect([], failed_search, citations))

    assert result == ["最新分数线需要核实。"]
    error_context = client.requests[1]["messages"][-1]["content"]
    assert "搜索服务暂时不可用" in error_context
    assert "private upstream" not in error_context
    assert citations == []
    assert tools.closed and client.closed


def test_search_disabled_omits_tools(provider):
    client = provider(FakeStream([chunk("先了解你的情况。")]))

    assert asyncio.run(collect([], no_search, [], search_enabled=False)) == ["先了解你的情况。"]
    assert len(client.requests) == 1
    assert "tools" not in client.requests[0]
    assert "tool_choice" not in client.requests[0]
    assert client.closed


def test_search_loop_has_a_final_request_without_tools(monkeypatch, provider):
    monkeypatch.setattr(deepseek_client, "MAX_AGENT_ITERATIONS", 2)
    client = provider(
        FakeStream([chunk(tools=[tool_part(0, "one", "web_search", '{"query":"一"}')])]),
        FakeStream([chunk(tools=[tool_part(0, "two", "web_search", '{"query":"二"}')])]),
        FakeStream([chunk("根据已有信息回答。")]),
    )
    queries = []

    async def search(query):
        queries.append(query)
        return "没有找到结果", []

    assert asyncio.run(collect([], search, [])) == ["根据已有信息回答。"]
    assert queries == ["一", "二"]
    assert len(client.requests) == 3
    assert all(request["stream"] for request in client.requests)
    assert all("tools" in request for request in client.requests[:2])
    assert "tools" not in client.requests[-1]
    assert client.closed and all(stream.closed for stream in client.streams)


def test_cancelling_stream_closes_http_stream_and_client(provider):
    async def scenario():
        stream = FakeStream([chunk("部分回答")], block_after=True)
        client = provider(stream)
        seen = []

        async def consume():
            async for piece in deepseek_client.chat_stream_agent([], no_search, []):
                seen.append(piece)

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.blocked.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen == ["部分回答"]
        assert stream.closed and client.closed
        assert stream.exit_type is asyncio.CancelledError
        assert client.exit_type is asyncio.CancelledError

    asyncio.run(scenario())


def test_closing_generator_after_first_chunk_closes_resources(provider):
    async def scenario():
        stream = FakeStream([chunk("第一段"), chunk("不再读取的第二段")])
        client = provider(stream)
        generator = deepseek_client.chat_stream_agent([], no_search, [])
        assert await anext(generator) == "第一段"
        await generator.aclose()
        assert stream.closed and client.closed

    asyncio.run(scenario())


def test_stream_error_propagates_after_closing_resources(provider):
    stream = FakeStream([chunk("部分回答"), RuntimeError("stream interrupted")])
    client = provider(stream)

    with pytest.raises(RuntimeError, match="stream interrupted"):
        asyncio.run(collect([], no_search, []))

    assert stream.closed and client.closed


def test_sentence_buffer_preserves_short_sentences_and_flushes_tail():
    buffer = SentenceBuffer(min_length=4, max_length=20)
    pieces = buffer.feed("好。")
    pieces += buffer.feed("行。继续说")
    pieces.append(buffer.flush())

    assert "".join(pieces) == "好。行。继续说"
    assert buffer.flush() == ""


def test_sentence_buffer_bounds_a_single_long_feed():
    buffer = SentenceBuffer(min_length=4, max_length=20)
    text = "长" * 135 + "。" + "尾" * 47
    pieces = buffer.feed(text)
    tail = buffer.flush()
    if tail:
        pieces.append(tail)

    assert "".join(pieces) == text
    assert all(0 < len(piece) <= 20 for piece in pieces)


def test_sentence_stream_flushes_short_final_utterance():
    async def source():
        yield "好。"
        yield "还需了解家庭情况"

    async def scenario():
        return [piece async for piece in sentence_stream(source())]

    assert "".join(asyncio.run(scenario())) == "好。还需了解家庭情况"


def test_citation_filter_ignores_huge_indices_and_keeps_numbering_gaps():
    citations = [{"title": str(index)} for index in range(1, 5)]
    text = "已核实[2]，无效引用[999999999]和[0]。"

    unchanged, display = filter_citations(text, citations)

    assert unchanged == text
    assert display == [None, citations[1]]
    assert filter_citations("仅有无效引用[999999999]", citations)[1] == []


def test_citation_filter_handles_numbers_larger_than_python_integer_digit_limit():
    citations = [{"title": "可用来源"}]
    enormous_number = "9" * 5000

    assert filter_citations(f"无效引用[{enormous_number}]", citations)[1] == []
    assert filter_citations(f"有效范围[1-{enormous_number}]", citations)[1] == citations
    assert filter_citations(f"前导零[{'0' * 5000}1]", citations)[1] == citations


def test_citation_filter_expands_ranges_and_accepts_mixed_separators():
    citations = [{"title": str(index)} for index in range(1, 7)]

    assert filter_citations("来源[1-3, 5]", citations)[1] == [
        citations[0], citations[1], citations[2], None, citations[4],
    ]
    assert filter_citations("来源[1, 3  6]", citations)[1] == [
        citations[0], None, citations[2], None, None, citations[5],
    ]
    assert filter_citations("范围[1-999999999]", citations)[1] == citations


def test_tts_strips_visible_formatting_and_citation_markers():
    assert strip_for_tts("### **先看就业**[1-3, 5]\n[学校](https://example.edu)") == "先看就业\n学校"


def test_search_results_skip_invalid_urls_and_keep_compact_numbering():
    data = {"data": {"webPages": {"value": [
        None,
        {"title": "无效脚本", "url": "javascript:alert(1)"},
        {"title": "学校 A", "url": "https://example.edu/a", "snippet": "A 数据"},
        "invalid result",
        {"title": "内嵌网页", "url": "data:text/html,unsafe"},
        {"title": "相对地址", "url": "/local"},
        {"name": "学校 B", "link": "http://example.org/b", "description": "B 数据"},
        {"title": "没有网址"},
    ]}}}

    context, citations = _format_results(data)

    assert citations == [
        {"title": "学校 A", "url": "https://example.edu/a", "snippet": "A 数据"},
        {"title": "学校 B", "url": "http://example.org/b", "snippet": "B 数据"},
    ]
    assert "[1] 学校 A" in context
    assert "[2] 学校 B" in context
    assert "[3]" not in context
    assert "javascript:" not in context
    assert "data:text/html" not in context
