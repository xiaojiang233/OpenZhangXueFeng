"""Regression tests use temporary storage and fake providers, never paid APIs."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.application import create_app
from app.services import ProviderServices
from config import Settings
from storage import ConversationStore


def make_client(tmp_path, chat_stream=None, **overrides):
    settings = Settings(data_dir=tmp_path, **overrides)
    store = ConversationStore(tmp_path)
    services = ProviderServices(settings, chat_stream=chat_stream)
    return TestClient(create_app(settings, store, services)), store


def receive_until(ws, predicate, max_frames=30):
    frames = []
    for _ in range(max_frames):
        frame = ws.receive_json()
        frames.append(frame)
        if predicate(frame):
            return frames
    pytest.fail(f"Expected frame missing: {frames!r}")


def done(frame):
    return frame.get("type") == "status" and frame.get("status") == "done"


def test_shell_and_history_work_without_provider_keys(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/").status_code == 200
        health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        assert health["services"] == {"chat": False, "voice": False, "search": False}
        assert client.get("/api/conversations").json() == []
        conv = client.post("/api/conversations").json()
        assert client.get(f"/api/conversations/{conv['id']}").json()["messages"] == []
        assert client.delete(f"/api/conversations/{conv['id']}").status_code == 200
        assert client.get("/api/conversations").json() == []
        assert client.post("/api/asr", files={"audio": ("test.wav", b"audio", "audio/wav")}).status_code == 503


def test_bad_socket_messages_do_not_disconnect_session(tmp_path):
    client, _ = make_client(tmp_path)
    with client, client.websocket_connect("/ws") as ws:
        for raw in ("not json", "[]", '{"type":"chat","text":123}', '{"type":"unknown"}'):
            ws.send_text(raw)
            receive_until(ws, lambda f: f["type"] == "error")
        ws.send_json({"type": "chat", "text": "你好", "mode": "text"})
        frames = receive_until(ws, lambda f: f["type"] == "error")
        assert "DEEPSEEK_API_KEY" in frames[-1]["message"]
        ws.send_json({"type": "new_conv"})
        assert receive_until(ws, lambda f: f["type"] == "history")[-1]["messages"] == []


def test_normal_stream_persists_one_answer_and_one_terminal_event(tmp_path):
    async def fake_chat(messages, search, citations, **kwargs):
        yield "你好，"
        yield "一起聊聊。"

    client, store = make_client(tmp_path, fake_chat, deepseek_api_key="test-key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "如何选专业", "mode": "text", "request_id": "normal"})
        frames = receive_until(ws, done)
        assert "".join(f["content"] for f in frames if f["type"] == "text_chunk") == "你好，一起聊聊。"
        assert sum(done(f) for f in frames) == 1
        conv_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        history = store.get_conversation(conv_id)["messages"]
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[-1]["content"] == "你好，一起聊聊。"


@pytest.mark.parametrize("command", ["stop", "new_conv", "load_conv", "delete_conv"])
def test_interrupt_cancels_blocked_stream_and_preserves_partial_once(tmp_path, command):
    closed = threading.Event()

    async def blocked_chat(messages, search, citations, **kwargs):
        try:
            yield "已经生成的部分。"
            await asyncio.sleep(3600)
            yield "不应出现的迟到内容。"
        finally:
            closed.set()

    client, store = make_client(tmp_path, blocked_chat, deepseek_api_key="test-key")
    other = store.create_conversation()
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "测试打断", "mode": "text", "request_id": "old"})
        frames = receive_until(ws, lambda f: f["type"] == "text_chunk")
        active_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        request = {"type": command}
        if command in {"load_conv", "delete_conv"}:
            request["conv_id"] = other["id"] if command == "load_conv" else active_id
        ws.send_json(request)
        if command == "stop":
            response = receive_until(ws, done)
        else:
            response = receive_until(ws, lambda f: f["type"] == "history")
        assert closed.wait(2), "The provider stream must close promptly on interruption"
        assert all(f.get("content") != "不应出现的迟到内容。" for f in response)
        if command == "delete_conv":
            assert store.get_conversation(active_id) is None
        else:
            history = store.get_conversation(active_id)["messages"]
            assert len([m for m in history if m["role"] == "assistant"]) == 1
            assert history[-1]["content"] == "已经生成的部分。"


def test_disconnect_closes_blocked_provider_stream(tmp_path):
    closed = threading.Event()

    async def blocked_chat(messages, search, citations, **kwargs):
        try:
            yield "开始。"
            await asyncio.sleep(3600)
        finally:
            closed.set()

    client, _ = make_client(tmp_path, blocked_chat, deepseek_api_key="test-key")
    with client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "chat", "text": "你好"})
            receive_until(ws, lambda f: f["type"] == "text_chunk")
        assert closed.wait(2)


def test_storage_concurrent_instances_keep_all_messages_and_recover_index(tmp_path):
    first, second = ConversationStore(tmp_path), ConversationStore(tmp_path)
    conv = first.create_conversation()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: (first if i % 2 else second).add_message(conv["id"], "user", f"message-{i}"), range(40)))
    messages = first.get_conversation(conv["id"])["messages"]
    assert {m["content"] for m in messages} == {f"message-{i}" for i in range(40)}
    (tmp_path / "conversations_index.json").write_text("{broken", encoding="utf-8")
    assert second.list_conversations()[0]["id"] == conv["id"]
    assert json.loads((tmp_path / "conversations_index.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("conv_id", ["../escape", "..\\escape", "C:\\outside", "/absolute", "", "invalid"])
def test_storage_rejects_unsafe_identifiers(tmp_path, conv_id):
    store = ConversationStore(tmp_path)
    with pytest.raises(ValueError):
        store.get_conversation(conv_id)


def test_citations_restart_in_new_conversation_and_survive_reload(tmp_path):
    offsets = []

    async def fake_search(query):
        return f"[1] {query}", [{"title": query, "url": "https://example.com/" + query, "snippet": "source"}]

    async def fake_chat(messages, search, citations, **kwargs):
        offsets.append(kwargs["citation_offset"])
        await search(messages[-1]["content"])
        yield "数据来源[1]。"

    client, store = make_client(tmp_path, fake_chat, deepseek_api_key="key", langsearch_api_key="key")
    client.app.state.services.search = fake_search
    with client, client.websocket_connect("/ws") as ws:
        ids = []
        for question in ("first", "second"):
            ws.send_json({"type": "new_conv"})
            receive_until(ws, lambda f: f["type"] == "history")
            ws.send_json({"type": "chat", "text": question})
            frames = receive_until(ws, done)
            ids.append(next(f["conv_id"] for f in frames if f["type"] == "conv_active"))
            assert next(f["data"] for f in frames if f["type"] == "citations")[0]["title"] == question
        assert offsets == [0, 0]
        ws.send_json({"type": "load_conv", "conv_id": ids[0]})
        restored = receive_until(ws, lambda f: f["type"] == "history")[-1]
        assert restored["messages"][-1]["display_citations"][0]["title"] == "first"


def test_generation_failure_preserves_partial_and_connection(tmp_path):
    async def failing_chat(messages, search, citations, **kwargs):
        yield "保留下来的内容。"
        raise RuntimeError("Bearer fake-secret-should-not-leak")

    client, store = make_client(tmp_path, failing_chat, deepseek_api_key="key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        frames = receive_until(ws, done)
        assert any(f["type"] == "error" for f in frames)
        assert "fake-secret" not in json.dumps(frames)
        conv_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        last = store.get_conversation(conv_id)["messages"][-1]
        assert last["content"] == "保留下来的内容。"
        assert last["interrupted"] is True
        ws.send_json({"type": "load_conv", "conv_id": conv_id})
        assert receive_until(ws, lambda f: f["type"] == "history")[-1]["messages"][-1]["content"] == last["content"]


def test_delayed_stop_does_not_cancel_newer_turn(tmp_path):
    closed = threading.Event()

    async def blocked_chat(messages, search, citations, **kwargs):
        try:
            yield "新的回答。"
            await asyncio.sleep(3600)
        finally:
            closed.set()

    client, _ = make_client(tmp_path, blocked_chat, deepseek_api_key="key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好", "request_id": "new-turn"})
        receive_until(ws, lambda f: f["type"] == "text_chunk")
        ws.send_json({"type": "stop", "request_id": "old-turn"})
        ws.send_json({"type": "unknown", "request_id": "ack"})
        receive_until(ws, lambda f: f["type"] == "error" and f.get("request_id") == "ack")
        assert not closed.is_set()
        ws.send_json({"type": "stop", "request_id": "new-turn"})
        receive_until(ws, done)
        assert closed.wait(2)


def test_asr_validates_input_and_sanitizes_upstream_failures(tmp_path):
    async def fake_transcribe(data):
        if data == b"fail":
            raise RuntimeError("secret-service-response")
        return {"text": "识别结果", "duration": 1.5}

    client, _ = make_client(tmp_path, fish_audio_api_key="key", max_upload_bytes=1024)
    client.app.state.services.transcribe = fake_transcribe
    with client:
        def upload(data, content_type="audio/wav"):
            return client.post("/api/asr", files={"audio": ("audio.wav", data, content_type)})
        assert upload(b"ok").json() == {"text": "识别结果", "duration": 1.5}
        assert upload(b"").status_code == 400
        assert upload(b"x" * 1025).status_code == 413
        assert upload(b"html", "text/html").status_code == 415
        failure = upload(b"fail")
        assert failure.status_code == 502
        assert "secret-service-response" not in failure.text


def test_rest_delete_cancels_active_session(tmp_path):
    closed = threading.Event()

    async def blocked_chat(messages, search, citations, **kwargs):
        try:
            yield "开始。"
            await asyncio.sleep(3600)
        finally:
            closed.set()

    client, store = make_client(tmp_path, blocked_chat, deepseek_api_key="key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        frames = receive_until(ws, lambda f: f["type"] == "text_chunk")
        conv_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        assert client.delete(f"/api/conversations/{conv_id}").status_code == 200
        history = receive_until(ws, lambda f: f["type"] == "history")[-1]
        assert history["conv_id"] is None
        assert closed.wait(2)
        assert store.get_conversation(conv_id) is None


@pytest.mark.parametrize("origin", ["http://untrusted.example", "null", "http://testserver.untrusted.example"])
def test_browser_websockets_reject_foreign_origins(tmp_path, origin):
    client, _ = make_client(tmp_path)
    with client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": origin}):
            pytest.fail("Foreign browser origin was accepted")


def test_browser_websocket_accepts_same_origin(tmp_path):
    client, _ = make_client(tmp_path)
    with client, client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        ws.send_json({"type": "new_conv"})
        assert receive_until(ws, lambda f: f["type"] == "history")[-1]["messages"] == []


def test_two_tabs_cannot_interleave_generation_in_same_conversation(tmp_path):
    async def blocked_chat(messages, search, citations, **kwargs):
        yield "已有部分。"
        await asyncio.sleep(3600)

    client, store = make_client(tmp_path, blocked_chat, deepseek_api_key="key")
    conv_id = store.create_conversation()["id"]
    with client, client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
        for ws in (first, second):
            ws.send_json({"type": "load_conv", "conv_id": conv_id})
            receive_until(ws, lambda f: f["type"] == "history")
        first.send_json({"type": "chat", "text": "first", "request_id": "first"})
        receive_until(first, lambda f: f["type"] == "text_chunk")
        second.send_json({"type": "chat", "text": "second", "request_id": "busy"})
        frames = receive_until(second, done)
        assert any(f.get("code") == "conversation_busy" for f in frames)
        assert [m["content"] for m in store.get_conversation(conv_id)["messages"]] == ["first"]
        first.send_json({"type": "stop", "request_id": "first"})
        receive_until(first, done)
        second.send_json({"type": "chat", "text": "second", "request_id": "second"})
        assert receive_until(second, lambda f: f["type"] == "text_chunk")[-1]["content"] == "已有部分。"
        second.send_json({"type": "stop", "request_id": "second"})
        receive_until(second, done)
        assert [m["role"] for m in store.get_conversation(conv_id)["messages"]] == ["user", "assistant", "user", "assistant"]


def test_reloading_same_active_conversation_includes_cancelled_partial(tmp_path):
    async def blocked_chat(messages, search, citations, **kwargs):
        yield "请保留这段。"
        await asyncio.sleep(3600)

    client, _ = make_client(tmp_path, blocked_chat, deepseek_api_key="key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        frames = receive_until(ws, lambda f: f["type"] == "text_chunk")
        conv_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        ws.send_json({"type": "load_conv", "conv_id": conv_id})
        history = receive_until(ws, lambda f: f["type"] == "history")[-1]
        assert history["messages"][-1]["content"] == "请保留这段。"


def test_websocket_delete_cancels_peer_tab_generation(tmp_path):
    closed = threading.Event()

    async def blocked_chat(messages, search, citations, **kwargs):
        try:
            yield "部分回复。"
            await asyncio.sleep(3600)
        finally:
            closed.set()

    client, store = make_client(tmp_path, blocked_chat, deepseek_api_key="key")
    conv_id = store.create_conversation()["id"]
    with client, client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
        for ws in (first, second):
            ws.send_json({"type": "load_conv", "conv_id": conv_id})
            receive_until(ws, lambda f: f["type"] == "history")
        first.send_json({"type": "chat", "text": "你好"})
        receive_until(first, lambda f: f["type"] == "text_chunk")
        second.send_json({"type": "delete_conv", "conv_id": conv_id})
        assert receive_until(first, lambda f: f["type"] == "history")[-1]["conv_id"] is None
        assert receive_until(second, lambda f: f["type"] == "history")[-1]["conv_id"] is None
        assert closed.wait(2)
        assert store.get_conversation(conv_id) is None


def test_stop_finishes_even_if_provider_cleanup_raises(tmp_path):
    async def broken_cleanup(messages, search, citations, **kwargs):
        try:
            yield "保留部分。"
            await asyncio.sleep(3600)
        finally:
            raise RuntimeError("provider cleanup failed")

    client, store = make_client(tmp_path, broken_cleanup, deepseek_api_key="key")
    with client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好", "request_id": "cleanup"})
        frames = receive_until(ws, lambda f: f["type"] == "text_chunk")
        conv_id = next(f["conv_id"] for f in frames if f["type"] == "conv_active")
        ws.send_json({"type": "stop", "request_id": "cleanup"})
        ws.send_json({"type": "new_conv", "request_id": "barrier"})
        frames = receive_until(ws, lambda f: f["type"] == "history")
        assert sum(done(frame) for frame in frames) == 1
        assert store.get_conversation(conv_id)["messages"][-1]["content"] == "保留部分。"
