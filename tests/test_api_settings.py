"""Local configuration persistence, live updates and credential boundaries."""

import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from app import create_app
from app.services import ProviderServices
from app import api_settings
from config import Settings
from tests.test_backend import done, receive_until


def client_for(tmp_path, *, chat=None, **options):
    settings = Settings(data_dir=tmp_path, **options)
    services = ProviderServices(settings, chat_stream=chat)
    app = create_app(settings, services=services)
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))


def test_snapshot_never_returns_keys_or_caches_them(tmp_path):
    with client_for(tmp_path, deepseek_api_key="private-model-secret", fish_audio_api_key="private-voice-secret",
                    langsearch_api_key="private-search-secret") as client:
        response = client.get("/api/settings")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "private-" not in response.text
        assert all(response.json()["configured"].values())
        assert response.json()["config"]["deepseek_model"] == "deepseek-v4-pro"


def test_save_keep_clear_and_restart_persistence(tmp_path):
    with client_for(tmp_path) as client:
        first = client.put("/api/settings", json={"deepseek_api_key": "new-private-key", "deepseek_model": "model-a"})
        assert first.status_code == 200
        assert "new-private-key" not in first.text
        assert first.json()["services"]["chat"] is True
        assert client.get("/api/health").json()["services"]["chat"] is True
        kept = client.put("/api/settings", json={"deepseek_model": "model-b"})
        assert kept.json()["configured"]["deepseek_api_key"] is True
        assert client.app.state.services.settings.deepseek_api_key == "new-private-key"
    saved = json.loads((tmp_path / "api_settings.json").read_text(encoding="utf-8"))
    assert saved["deepseek_api_key"] == "new-private-key"
    with client_for(tmp_path) as restarted:
        snapshot = restarted.get("/api/settings").json()
        assert snapshot["config"]["deepseek_model"] == "model-b"
        assert restarted.get("/api/health").json()["services"]["chat"] is True
        cleared = restarted.put("/api/settings", json={"deepseek_api_key": ""})
        assert cleared.json()["configured"]["deepseek_api_key"] is False
    with client_for(tmp_path) as restarted:
        assert restarted.get("/api/health").json()["services"]["chat"] is False


@pytest.mark.parametrize("payload", [
    {"deepseek_api_key": "private-secret\nunsafe"},
    {"deepseek_api_key": 12345},
    {"deepseek_base_url": "https://api.example/?key=private-secret"},
    {"deepseek_base_url": "https://private-secret@example.com"},
    {"deepseek_model": " "},
    {"fish_audio_latency": "invalid"},
    {"host": "0.0.0.0", "deepseek_api_key": "private-secret"},
    {"fish_audio_api_key": "private-secret", "fish_audio_voice_id": ""},
])
def test_invalid_configuration_is_redacted_and_not_saved(tmp_path, payload):
    with client_for(tmp_path) as client:
        result = client.put("/api/settings", json=payload)
        assert result.status_code == 422
        assert "private-secret" not in result.text
        assert not (tmp_path / "api_settings.json").exists()
        assert client.get("/api/health").json()["services"]["chat"] is False


@pytest.mark.parametrize("host,peer", [("http://localhost", "192.168.1.8"), ("http://hostile.example", "127.0.0.1")])
def test_remote_clients_and_rebinding_hosts_cannot_access_settings(tmp_path, host, peer):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url=host, client=(peer, 50000)) as client:
        assert client.get("/api/settings").status_code == 403
        assert client.put("/api/settings", json={"deepseek_api_key": "private-secret"}).status_code == 403


def test_cross_origin_and_invalid_requests_cannot_write_settings(tmp_path):
    with client_for(tmp_path) as client:
        for origin in ("http://hostile.example", "null", "http://localhost:9000"):
            assert client.put("/api/settings", json={}, headers={"origin": origin}).status_code == 403
        assert client.put("/api/settings", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert client.put("/api/settings", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
        assert client.put("/api/settings", content="broken", headers={"Content-Type": "application/json"}).status_code == 422
        assert client.put("/api/settings", content="x" * 33000, headers={"Content-Type": "application/json"}).status_code == 413
        assert not (tmp_path / "api_settings.json").exists()


def test_test_connection_uses_unsaved_draft_and_redacts_provider_errors(tmp_path, monkeypatch):
    seen = []

    async def probe(service, settings):
        seen.append((service, settings.deepseek_api_key, settings.deepseek_model))
        if settings.deepseek_model == "broken":
            raise RuntimeError("private-secret-provider-response")

    monkeypatch.setattr(api_settings, "probe_service", probe)
    with client_for(tmp_path) as client:
        result = client.post("/api/settings/test", json={"service": "chat", "deepseek_api_key": "draft-secret", "deepseek_model": "draft-model"})
        assert result.json()["ok"] is True
        assert seen == [("chat", "draft-secret", "draft-model")]
        assert not (tmp_path / "api_settings.json").exists()
        assert client.get("/api/health").json()["services"]["chat"] is False
        result = client.post("/api/settings/test", json={"service": "chat", "deepseek_model": "broken"})
        assert result.json()["ok"] is False
        assert "private-secret" not in result.text


def test_write_failure_preserves_running_configuration(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        def failed_save(settings):
            raise OSError("private-path-details")
        monkeypatch.setattr(client.app.state.api_settings_store, "save", failed_save)
        result = client.put("/api/settings", json={"deepseek_api_key": "private-new-key"})
        assert result.status_code == 503
        assert "private-" not in result.text
        assert not client.app.state.services.readiness["chat"]


def test_invalid_provider_key_returns_actionable_error_without_upstream_body(tmp_path, monkeypatch):
    async def failed_probe(service, settings):
        request = httpx.Request("POST", "https://example.com/api")
        response = httpx.Response(401, text="private-upstream-authentication-details", request=request)
        raise httpx.HTTPStatusError("private-provider-secret", request=request, response=response)

    monkeypatch.setattr(api_settings, "probe_service", failed_probe)
    with client_for(tmp_path) as client:
        response = client.post("/api/settings/test", json={"service": "voice", "fish_audio_api_key": "wrong-key"})
        assert response.json()["ok"] is False
        assert "认证失败" in response.json()["message"]
        assert "private-" not in response.text


def test_existing_socket_uses_new_configuration_without_reconnect(tmp_path):
    async def chat(messages, search, citations, **kwargs):
        yield "新配置已生效。"

    with client_for(tmp_path, chat=chat) as client, client.websocket_connect("/ws") as ws:
        result = client.put("/api/settings", json={"deepseek_api_key": "private-new-key"})
        assert result.status_code == 200
        assert ws.receive_json()["type"] == "config_updated"
        ws.send_json({"type": "chat", "text": "你好"})
        frames = receive_until(ws, done)
        assert any(frame.get("content") == "新配置已生效。" for frame in frames)


def test_saving_during_generation_is_rejected_without_losing_turn(tmp_path):
    async def blocked_chat(messages, search, citations, **kwargs):
        yield "保留当前回复。"
        await asyncio.sleep(3600)

    with client_for(tmp_path, chat=blocked_chat, deepseek_api_key="old-key") as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "chat", "text": "你好"})
        receive_until(ws, lambda frame: frame["type"] == "text_chunk")
        result = client.put("/api/settings", json={"deepseek_api_key": "new-key"})
        assert result.status_code == 409
        assert client.app.state.services.settings.deepseek_api_key == "old-key"
        assert not (tmp_path / "api_settings.json").exists()
        ws.send_json({"type": "stop"})
        receive_until(ws, done)


def test_corrupt_configuration_can_be_repaired_from_panel(tmp_path):
    (tmp_path / "api_settings.json").write_text("{broken", encoding="utf-8")
    with client_for(tmp_path) as client:
        assert client.get("/api/settings").json()["load_error"] is True
        assert client.put("/api/settings", json={"deepseek_model": "repaired"}).status_code == 200
        assert client.get("/api/settings").json()["load_error"] is False
