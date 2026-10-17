"""Local-only API configuration. Secrets stay on the server and never in responses."""

import asyncio
import ipaddress
import json
import logging
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from config import Settings

router = APIRouter(prefix="/api/settings", tags=["configuration"])
logger = logging.getLogger("openzxf.settings")
SECRET_FIELDS = ("deepseek_api_key", "fish_audio_api_key", "langsearch_api_key")
PUBLIC_FIELDS = ("deepseek_base_url", "deepseek_model", "fish_audio_voice_id", "fish_audio_latency")
CONFIG_FIELDS = (*PUBLIC_FIELDS, *SECRET_FIELDS)


class ProbeError(ValueError):
    """A user-facing diagnostic written by this app, never by an upstream response."""


class ApiSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    deepseek_api_key: str | None = Field(default=None, max_length=512, repr=False)
    fish_audio_api_key: str | None = Field(default=None, max_length=512, repr=False)
    langsearch_api_key: str | None = Field(default=None, max_length=512, repr=False)
    deepseek_base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    deepseek_model: str | None = Field(default=None, min_length=1, max_length=128)
    fish_audio_voice_id: str | None = Field(default=None, max_length=128)
    fish_audio_latency: Literal["normal", "balanced", "low"] | None = None

    @field_validator(*CONFIG_FIELDS)
    @classmethod
    def no_control_characters(cls, value):
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Control characters are not permitted")
        return value

    def apply(self, current: Settings) -> Settings:
        candidate = replace(current, **self.model_dump(exclude_unset=True, exclude_none=True))
        url = urlsplit(candidate.deepseek_base_url)
        # Query strings commonly contain credentials; never return them as public configuration.
        if url.query or url.fragment:
            raise ValueError("API base URL cannot contain a query or fragment")
        if candidate.fish_audio_api_key and not candidate.fish_audio_voice_id.strip():
            raise ValueError("Voice ID is required")
        return candidate


class ServiceTest(ApiSettingsUpdate):
    service: Literal["chat", "voice", "search"]

    def apply(self, current: Settings) -> Settings:
        update = ApiSettingsUpdate.model_validate(self.model_dump(exclude={"service"}, exclude_unset=True))
        return update.apply(current)


class ApiSettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.load_error = False

    def load(self, base: Settings) -> Settings:
        if not self.path.exists():
            return base
        try:
            if self.path.stat().st_size > 32768:
                raise ValueError("Oversized configuration")
            return ApiSettingsUpdate.model_validate(json.loads(self.path.read_text(encoding="utf-8"))).apply(base)
        except (OSError, ValueError):
            self.load_error = True
            logger.warning("Saved API configuration could not be loaded; original file preserved")
            return base

    def save(self, settings: Settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".api-settings-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({key: getattr(settings, key) for key in CONFIG_FIELDS}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self.load_error = False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def require_local(request: Request, *, write: bool = False):
    # Check both peer and Host to prevent DNS rebinding from a hostile local origin.
    if not request.client or not loopback(request.client.host) or not loopback(request.url.hostname):
        raise HTTPException(403, "API 配置仅可在服务所在电脑通过 localhost 或 127.0.0.1 修改。")
    origin = request.headers.get("origin")
    if origin:
        try:
            source, target = urlsplit(origin), urlsplit(str(request.base_url))
            valid = (source.scheme == target.scheme and source.hostname == target.hostname
                     and (source.port or (443 if source.scheme == "https" else 80))
                     == (target.port or (443 if target.scheme == "https" else 80))
                     and not source.username and not source.password and source.path in {"", "/"}
                     and not source.query and not source.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(403, "请从当前应用页面操作 API 配置。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站操作 API 配置。")
    if write and request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(415, "请使用 JSON 提交配置。")


def snapshot(settings: Settings, store: ApiSettingsStore) -> dict:
    public = {key: getattr(settings, key) for key in PUBLIC_FIELDS}
    url = urlsplit(settings.deepseek_base_url)
    if url.query or url.fragment:  # Legacy environment values must not disclose query credentials.
        public["deepseek_base_url"] = f"{url.scheme}://{url.netloc}{url.path}"
    return {"config": public, "configured": {key: bool(getattr(settings, key)) for key in SECRET_FIELDS},
            "services": settings.services, "load_error": store.load_error}


async def read_update(request: Request, model=ApiSettingsUpdate):
    require_local(request, write=True)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 32768:
            raise HTTPException(413, "配置内容过长。")
    try:
        return model.model_validate(json.loads(data))
    except (ValueError, ValidationError):
        # FastAPI's default validation response echoes input, including secret values.
        raise HTTPException(422, "配置格式无效，请检查字段、长度及选项。") from None


@router.get("")
async def get_settings(request: Request):
    require_local(request)
    return JSONResponse(snapshot(request.app.state.settings, request.app.state.api_settings_store),
                        headers={"Cache-Control": "no-store"})


@router.put("")
async def save_settings(request: Request):
    update = await read_update(request)
    state = request.app.state
    async with state.coordinator.command_lock:
        if state.coordinator.owners:
            raise HTTPException(409, "请先停止当前正在生成的回复，再保存 API 配置。")
        try:
            candidate = update.apply(state.settings)
        except ValueError:
            raise HTTPException(422, "配置无效：请检查 API 地址、模型名称和语音音色 ID。") from None
        services = state.services.reconfigure(candidate)
        try:
            await asyncio.to_thread(state.api_settings_store.save, candidate)
        except OSError:
            raise HTTPException(503, "配置保存失败，请检查服务端数据目录权限。") from None
        state.settings, state.services = candidate, services
        for session in tuple(state.sessions):
            session.settings, session.services = candidate, services
            await session.send({"type": "config_updated", "services": services.readiness})
    return JSONResponse({**snapshot(candidate, state.api_settings_store), "message": "配置已保存并立即生效。"},
                        headers={"Cache-Control": "no-store"})


async def probe_service(service: str, settings: Settings):
    if not settings.services[service]:
        raise ProbeError("请先填写此服务的 API Key。")
    if service == "chat":
        from openai import AsyncOpenAI
        async with AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                               timeout=12.0, max_retries=0) as client:
            models = await client.models.list()
            if settings.deepseek_model not in {model.id for model in models.data}:
                raise ProbeError("连接成功，但当前模型不在服务返回的模型列表中，请检查模型名称。")
    elif service == "voice":
        from fish_audio_client import tts_sentence
        if not await tts_sentence("你好。", settings=settings):
            raise ProbeError("语音服务未返回音频，请检查音色 ID。")
    else:
        from langsearch_client import search
        await search("教育部", count=1, settings=settings)


@router.post("/test")
async def test_settings(request: Request):
    update = await read_update(request, ServiceTest)
    try:
        candidate = update.apply(request.app.state.settings)
    except ValueError:
        raise HTTPException(422, "请检查 API 地址、模型和音色配置。") from None
    try:
        await asyncio.wait_for(probe_service(update.service, candidate), timeout=12)
        message = "连接测试通过。" if update.service != "voice" else "语音测试通过，已成功生成短句音频。"
        return JSONResponse({"ok": True, "message": message}, headers={"Cache-Control": "no-store"})
    except ProbeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, headers={"Cache-Control": "no-store"})
    except (TimeoutError, httpx.TimeoutException):
        return JSONResponse({"ok": False, "message": "连接测试超时，请检查网络和 API 地址。"},
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:
        logger.warning("API connection test failed (%s)", type(exc).__name__)
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        message = ("认证失败，请检查 API Key 和服务权限。" if status in {401, 403} else
                   "额度不足或请求过于频繁，请检查服务账户。" if status in {402, 429} else
                   "连接测试失败，请检查 API 地址、网络及服务配置。")
        return JSONResponse({"ok": False, "message": message}, headers={"Cache-Control": "no-store"})
