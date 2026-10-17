"""HTTP and WebSocket transport adapters."""

import asyncio
import logging
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket
from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage import InvalidConversationID, StorageError

from .session import ChatSession

router = APIRouter()
logger = logging.getLogger("openzxf.routes")


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value):
        if not value.strip():
            raise ValueError("Title cannot be empty")
        return value.strip()


@router.get("/api/health")
async def health(request: Request):
    services = request.app.state.services.readiness
    return {"status": "ready" if all(services.values()) else "degraded", "services": services,
            "version": "2.0.0", "limits": {"message_chars": request.app.state.settings.max_message_chars,
                                             "upload_bytes": request.app.state.settings.max_upload_bytes}}


async def storage_call(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except InvalidConversationID as exc:
        raise HTTPException(400, "对话编号无效。") from exc
    except (StorageError, OSError) as exc:
        logger.warning("Conversation storage unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "对话数据暂时无法读写，请检查数据目录。") from exc


@router.get("/api/conversations")
async def list_conversations(request: Request):
    return await storage_call(request.app.state.store.list_conversations)


@router.post("/api/conversations")
async def create_conversation(request: Request):
    return await storage_call(request.app.state.store.create_conversation)


@router.get("/api/conversations/{conv_id}")
async def get_conversation(request: Request, conv_id: str):
    conversation = await storage_call(request.app.state.store.get_conversation, conv_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在或已被删除。")
    return conversation


@router.delete("/api/conversations/{conv_id}")
async def delete_conversation(request: Request, conv_id: str):
    # Validate before interrupting any open connection.
    from storage import validate_conversation_id
    try:
        validate_conversation_id(conv_id)
    except InvalidConversationID as exc:
        raise HTTPException(400, "对话编号无效。") from exc
    coordinator = request.app.state.coordinator
    try:
        async with coordinator.command_lock:
            deleted = await coordinator.delete(request.app.state.store, conv_id)
    except (StorageError, OSError) as exc:
        raise HTTPException(503, "对话数据暂时无法读写，请检查数据目录。") from exc
    return {"ok": True, "deleted": deleted}


@router.patch("/api/conversations/{conv_id}")
async def rename_conversation(request: Request, conv_id: str, update: ConversationUpdate):
    try:
        conversation = await asyncio.to_thread(request.app.state.store.update_conversation,
                                              conv_id, title=update.title)
    except InvalidConversationID as exc:
        raise HTTPException(400, "对话编号无效。") from exc
    except (StorageError, OSError) as exc:
        raise HTTPException(503, "对话数据暂时无法读写，请检查数据目录。") from exc
    if conversation is None:
        raise HTTPException(404, "对话不存在或已被删除。")
    return conversation


@router.post("/api/asr")
async def transcribe(request: Request, audio: UploadFile = File(...)):
    try:
        if not request.app.state.services.readiness["voice"]:
            raise HTTPException(503, "语音服务尚未配置，请在“API 设置”中填写 Fish Audio 密钥。")
        content_type = (audio.content_type or "").split(";", 1)[0].lower()
        if content_type and not (content_type.startswith("audio/") or content_type in {
            "application/octet-stream", "video/webm", "video/mp4"}):
            raise HTTPException(415, "请上传音频文件。")
        limit = request.app.state.settings.max_upload_bytes
        chunks, length = [], 0
        while chunk := await audio.read(min(65536, limit + 1 - length)):
            length += len(chunk)
            if length > limit:
                raise HTTPException(413, "音频文件过大，请缩短录音后重试。")
            chunks.append(chunk)
        if not length:
            raise HTTPException(400, "录音内容为空，请重新录制。")
        result = await request.app.state.services.transcribe(b"".join(chunks))
        return {"text": result.get("text", ""), "duration": result.get("duration", 0)}
    except HTTPException:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise HTTPException(504, "语音识别超时，请重试。") from exc
    except Exception as exc:
        logger.warning("Audio transcription failed (%s)", type(exc).__name__)
        raise HTTPException(502, "语音识别暂时不可用，请稍后重试。") from exc
    finally:
        await audio.close()


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin is not None:
        try:
            browser = urlsplit(origin)
            target = urlsplit(str(ws.url))
            allowed_scheme = "https" if target.scheme == "wss" else "http"
            origin_port = browser.port or (443 if browser.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "wss" else 80)
            allowed = (browser.scheme == allowed_scheme and browser.hostname == target.hostname
                       and origin_port == target_port and not browser.username and not browser.password
                       and browser.path in {"", "/"} and not browser.query and not browser.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            await ws.close(code=1008)
            return
    state = ws.app.state
    session = ChatSession(ws, state.settings, state.store, state.services, state.coordinator)
    state.sessions.add(session)
    try:
        await session.run()
    finally:
        state.sessions.discard(session)
