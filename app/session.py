"""Per-connection state with independently cancellable generation and voice work."""

import asyncio
import base64
import copy
import json
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from config import Settings
from storage import ConversationStore, InvalidConversationID, StorageError, validate_conversation_id
from sentence_buffer import SentenceBuffer
from tts_utils import filter_citations, strip_for_tts

from .services import ProviderServices

logger = logging.getLogger("openzxf.session")


class SessionCoordinator:
    """Serialize short commands and keep one active generation per conversation.

    Provider work always runs outside the command lock, so stop stays responsive.
    """

    def __init__(self):
        self.command_lock = asyncio.Lock()
        self.sessions: set[ChatSession] = set()
        self.owners: dict[str, ChatSession] = {}

    async def delete(self, store, conv_id: str, *, initiator=None, request_id=None):
        # Caller holds command_lock, shared by REST and all socket commands.
        affected = [session for session in self.sessions if session.active_conv_id == conv_id]
        for session in affected:
            await session.cancel()
        deleted = store.delete_conversation(conv_id)
        for session in affected:
            session.active_conv_id = None
            await session.history(None, request_id if session is initiator else None)
        for session in tuple(self.sessions):
            await session.conversation_list(request_id if session is initiator else None)
        return deleted


@dataclass
class Turn:
    request_id: str
    conv_id: str
    mode: str
    history: list[dict]
    citations: list[dict] = field(default_factory=list)
    response: str = ""
    cancelled: bool = False
    persisted: bool = False
    done_sent: bool = False
    text_complete: bool = False


class ChatSession:
    def __init__(self, ws: WebSocket, settings: Settings, store: ConversationStore,
                 services: ProviderServices, coordinator: SessionCoordinator | None = None):
        self.ws, self.settings, self.store, self.services = ws, settings, store, services
        self.active_conv_id: str | None = None
        self.turn: Turn | None = None
        self.generation: asyncio.Task | None = None
        self.closed = False
        self._send_lock = asyncio.Lock()
        self.coordinator = coordinator or SessionCoordinator()
        self.command_lock = self.coordinator.command_lock

    async def send(self, payload: dict, *, request_id: str | None = None,
                   turn: Turn | None = None):
        async with self._send_lock:
            if self.closed or (turn and (turn is not self.turn or turn.cancelled)):
                return
            if turn:
                payload = {**payload, "request_id": turn.request_id, "conv_id": turn.conv_id}
            elif request_id is not None:
                payload = {**payload, "request_id": request_id}
            try:
                await self.ws.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                # A disconnected peer must not abort another tab's shared deletion.
                self.closed = True

    async def error(self, message: str, *, code: str = "invalid_message", request_id=None,
                    turn: Turn | None = None):
        await self.send({"type": "error", "message": message, "code": code},
                        request_id=request_id, turn=turn)

    async def cancel(self, *, notify: bool = True):
        turn, task = self.turn, self.generation
        if turn:
            turn.cancelled = True
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if turn and notify and not turn.done_sent:
            turn.done_sent = True
            await self.send({"type": "status", "status": "done", "cancelled": True,
                             "request_id": turn.request_id, "conv_id": turn.conv_id})
        if turn and self.coordinator.owners.get(turn.conv_id) is self:
            self.coordinator.owners.pop(turn.conv_id, None)
        self.turn, self.generation = None, None

    async def history(self, conversation: dict | None, request_id=None):
        messages = []
        for message in (conversation or {}).get("messages", []):
            item = copy.deepcopy(message)
            if item["role"] == "assistant":
                _, item["display_citations"] = filter_citations(item["content"], item.get("citations", []))
            messages.append(item)
        await self.send({"type": "history", "conv_id": self.active_conv_id,
                         "messages": messages}, request_id=request_id)

    async def conversation_list(self, request_id=None, turn=None):
        await self.send({"type": "conv_list", "conversations": self.store.list_conversations()},
                        request_id=request_id, turn=turn)

    async def run(self):
        await self.ws.accept()
        try:
            while True:
                packet = await self.ws.receive()
                if packet["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(packet.get("code", 1000))
                raw = packet.get("text")
                if not isinstance(raw, str) or len(raw) > self.settings.max_message_chars * 6 + 2048:
                    await self.error("请发送长度合适的 JSON 文本消息。")
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    await self.error("消息格式无效，请发送 JSON 对象。")
                    continue
                if not isinstance(message, dict):
                    await self.error("消息必须是 JSON 对象。")
                    continue
                request_id = message.get("request_id")
                if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
                    await self.error("request_id 必须是 1 到 128 个字符的文本。")
                    continue
                async with self.command_lock:
                    try:
                        await self.handle(message, request_id)
                    except InvalidConversationID:
                        await self.error("对话编号无效。", request_id=request_id)
                    except FileNotFoundError:
                        await self.error("这段对话不存在或已被删除。", code="not_found", request_id=request_id)
                    except (StorageError, OSError):
                        logger.warning("Conversation command failed")
                        await self.error("对话数据暂时无法读写，请检查数据目录。", code="storage_error", request_id=request_id)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.closed = True
            await self.cancel(notify=False)

    async def handle(self, message: dict, request_id: str | None):
        kind = message.get("type")
        if not isinstance(kind, str):
            await self.error("消息类型必须是文本。", request_id=request_id)
            return
        if kind == "stop":
            # A delayed stop for an earlier turn must not cancel a newer request.
            if not request_id or not self.turn or request_id == self.turn.request_id:
                await self.cancel()
            return
        if kind == "new_conv":
            await self.cancel()
            self.active_conv_id = None
            await self.history(None, request_id)
            return
        if kind in {"load_conv", "delete_conv"}:
            conv_id = validate_conversation_id(message.get("conv_id"))
            if kind == "load_conv":
                conversation = self.store.get_conversation(conv_id)
                if conversation is None:
                    raise FileNotFoundError
                await self.cancel()
                self.active_conv_id = conv_id
                conversation = self.store.get_conversation(conv_id)
                if conversation is None:
                    self.active_conv_id = None
                    raise FileNotFoundError
                await self.history(conversation, request_id)
            else:
                await self.cancel()
                await self.coordinator.delete(self.store, conv_id, initiator=self, request_id=request_id)
            return
        if kind != "chat":
            await self.error("不支持的消息类型。", request_id=request_id)
            return
        text, mode = message.get("text"), message.get("mode", "text")
        if not isinstance(text, str) or not text.strip():
            await self.error("请输入要咨询的问题。", request_id=request_id)
            return
        if len(text) > self.settings.max_message_chars:
            await self.error(f"单条消息不能超过 {self.settings.max_message_chars} 个字符。", request_id=request_id)
            return
        if not isinstance(mode, str) or mode not in {"text", "voice"}:
            await self.error("mode 仅支持 text 或 voice。", request_id=request_id)
            return
        await self.cancel()
        request_id = request_id or uuid.uuid4().hex
        if not self.services.readiness["chat"]:
            await self.error("文字服务尚未配置，请在侧栏“API 设置”中填写模型密钥（DEEPSEEK_API_KEY）。",
                             code="chat_unavailable", request_id=request_id)
            await self.send({"type": "status", "status": "done"}, request_id=request_id)
            return
        if self.active_conv_id:
            conversation = self.store.get_conversation(self.active_conv_id)
            if conversation is None:
                self.active_conv_id = None
        if self.active_conv_id and self.coordinator.owners.get(self.active_conv_id) not in {None, self}:
            await self.error("这段对话正在其他窗口回复，请等待完成或新建对话。",
                             code="conversation_busy", request_id=request_id)
            await self.send({"type": "status", "status": "done"}, request_id=request_id)
            return
        created = not self.active_conv_id
        if created:
            conversation = self.store.create_conversation()
            self.active_conv_id = conversation["id"]
        self.store.add_message(self.active_conv_id, "user", text.strip(), request_id=request_id)
        conversation = self.store.get_conversation(self.active_conv_id)
        citations = next((copy.deepcopy(m["citations"]) for m in reversed(conversation["messages"])
                          if m["role"] == "assistant" and isinstance(m.get("citations"), list)), [])
        history = [{"role": m["role"], "content": m["content"]}
                   for m in conversation["messages"][-self.settings.max_history_messages:]]
        self.turn = Turn(request_id, self.active_conv_id, mode, history, citations)
        self.coordinator.owners[self.active_conv_id] = self
        if created:
            await self.send({"type": "conv_active", "conv_id": self.active_conv_id}, request_id=request_id)
        self.generation = asyncio.create_task(self.generate(self.turn))

    def persist(self, turn: Turn, *, interrupted: bool = False):
        if turn.persisted or not turn.response.strip():
            return
        turn.response, _ = filter_citations(turn.response, turn.citations)
        self.store.add_message(turn.conv_id, "assistant", turn.response, turn.citations,
                               interrupted=interrupted, request_id=turn.request_id)
        turn.persisted = True

    async def finish(self, turn: Turn, **extra):
        if not turn.done_sent and not turn.cancelled and turn is self.turn:
            turn.done_sent = True
            await self.send({"type": "status", "status": "done", **extra}, turn=turn)

    async def generate(self, turn: Turn):
        try:
            await asyncio.wait_for(self.stream(turn), timeout=self.settings.generation_timeout_seconds)
        except asyncio.CancelledError:
            try:
                self.persist(turn, interrupted=True)
            except (OSError, StorageError):
                logger.warning("Interrupted reply could not be persisted")
            raise
        except Exception as exc:
            logger.warning("Reply failed (%s)", type(exc).__name__)
            try:
                self.persist(turn, interrupted=True)
            except (OSError, StorageError):
                logger.warning("Partial reply could not be persisted")
            if isinstance(exc, TimeoutError):
                message = "回复超时，已有内容已保留，请稍后重试。"
            elif turn.persisted:
                message = "语音暂时无法播放，文字回复已保留。" if turn.text_complete else "回复中断，已有内容已保留，请稍后重试。"
            else:
                message = "回复服务暂时不可用，请检查 API 配置或稍后重试。"
            await self.error(message, code="generation_error", turn=turn)
            await self.finish(turn)
        finally:
            if self.coordinator.owners.get(turn.conv_id) is self:
                self.coordinator.owners.pop(turn.conv_id, None)

    async def stream(self, turn: Turn):
        await self.send({"type": "status", "status": "thinking"}, turn=turn)

        async def search(query: str):
            if not self.services.readiness["search"]:
                return "联网搜索未配置，请说明无法核实实时数据，不要编造来源。", []
            await self.send({"type": "status", "status": "searching"}, turn=turn)
            try:
                text, citations = await self.services.search(query)
                turn.citations.extend(citations)
                return text, citations
            except Exception as exc:
                logger.warning("Search failed (%s)", type(exc).__name__)
                return "本次联网搜索暂时失败，请说明无法核实实时数据，不要编造来源。", []
            finally:
                await self.send({"type": "status", "status": "thinking"}, turn=turn)

        output_citations = []
        stream = self.services.chat_stream(turn.history, search, output_citations,
                                           voice_mode=turn.mode == "voice",
                                           citation_offset=len(turn.citations))
        try:
            async for chunk in stream:
                if turn.cancelled:
                    break
                if not isinstance(chunk, str):
                    continue
                turn.response += chunk
                await self.send({"type": "text_chunk", "content": chunk}, turn=turn)
        finally:
            if hasattr(stream, "aclose"):
                await stream.aclose()
        turn.response, display = filter_citations(turn.response, turn.citations)
        self.persist(turn)
        turn.text_complete = True
        await self.send({"type": "citations", "data": display, "full": turn.citations}, turn=turn)
        await self.conversation_list(turn=turn)
        if turn.mode == "voice" and self.services.readiness["voice"]:
            buffer = SentenceBuffer(min_length=1, max_length=200)
            sentences = buffer.feed(turn.response)
            remaining = buffer.flush()
            if remaining:
                sentences.append(remaining)
            for sentence in sentences:
                text = strip_for_tts(sentence)
                if not text.strip():
                    continue
                audio = await self.services.tts(text)
                if audio:
                    await self.send({"type": "audio", "data": base64.b64encode(audio).decode("ascii"),
                                     "format": "pcm", "sample_rate": 44100, "channels": 1}, turn=turn)
        await self.finish(turn)
