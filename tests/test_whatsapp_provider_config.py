"""Regressions for provider selection and organisation-specific instances."""
import asyncio
import io
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "provider_config_tests")
import pytest
from fastapi import HTTPException
import server

ACTOR = {"id": "admin", "role": "admin", "active_organization_id": "propzel"}


def test_confirmed_evolution_instance_is_not_replaced_by_generated_name():
    assert server._evolution_instance({"organization_id": "org", "evolution_instance_name": "propzel-paired"}) == "propzel-paired"


def test_environment_credentials_keep_confirmed_organisation_instance(monkeypatch):
    monkeypatch.setenv("EVOLUTION_API_URL", "https://provider.example")
    monkeypatch.setenv("EVOLUTION_API_KEY", "key")
    monkeypatch.setenv("EVOLUTION_INSTANCE_NAME", "taskko-default")
    result = server._with_env_integration_defaults({"organization_id": "org", "evolution_instance_name": "propzel-paired", "whatsapp_provider": "pending"})
    assert result["whatsapp_provider"] == "evolution"
    assert result["evolution_instance_name"] == "propzel-paired"


def test_new_instance_is_saved_on_selected_org_not_singleton(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(server, "db", SimpleNamespace(settings=SimpleNamespace(update_one=update)))
    settings = {}
    asyncio.run(server._persist_whatsapp_instance("propzel", settings, {"instance_id": "new-id"}))
    assert update.call_args.args[0] == {"id": "organization:propzel"}
    assert update.call_args.args[1]["$set"]["organization_id"] == "propzel"
    assert settings["whatsapp_instance_id"] == "new-id"


def test_no_org_cannot_write_global_instance(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(server, "db", SimpleNamespace(settings=SimpleNamespace(update_one=update)))
    asyncio.run(server._persist_whatsapp_instance(None, {}, {"instance_id": "new-id"}))
    update.assert_not_awaited()


def legacy_settings():
    return {"organization_id": "propzel", "whatsapp_provider": "marketly_api", "whatsapp_service_url": "https://provider.example/api", "whatsapp_access_token": "secret", "whatsapp_instance_id": "old-id"}


def test_html_response_is_rejected_with_configuration_reason(monkeypatch):
    reply = Mock()
    reply.__enter__ = Mock(return_value=reply)
    reply.__exit__ = Mock(return_value=False)
    reply.read.return_value = b"<html>Marketly website</html>"
    monkeypatch.setattr(server.urllib.request, "urlopen", Mock(return_value=reply))
    result = asyncio.run(server.whatsapp_service_request("instance", legacy_settings()))
    assert result["status"] == "provider_error"
    assert "WHATSAPP_SERVICE_URL" in result["message"]
    assert "secret" not in str(result)


def test_connect_does_not_claim_ready_after_provider_error(monkeypatch):
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value=legacy_settings()))
    provider = AsyncMock(return_value={"status": "provider_error", "message": "API returned HTML"})
    monkeypatch.setattr(server, "whatsapp_service_request", provider)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.whatsapp_connect(ACTOR))
    assert exc.value.status_code == 502
    assert exc.value.detail == "API returned HTML"
    assert provider.await_count == 1


def test_invalid_access_token_does_not_create_an_instance(monkeypatch):
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value=legacy_settings()))
    provider = AsyncMock(return_value={"status": "error", "message": "Invalid access token"})
    monkeypatch.setattr(server, "whatsapp_service_request", provider)
    with pytest.raises(HTTPException):
        asyncio.run(server.whatsapp_connect(ACTOR))
    assert provider.await_count == 1


def test_expired_instance_is_replaced_and_webhook_is_scoped(monkeypatch):
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value=legacy_settings()))
    monkeypatch.setattr(server, "_whatsapp_webhook_url", lambda: "https://crm.example/api/whatsapp/webhook")
    provider = AsyncMock(side_effect=[{"status": "error", "message": "Instance expired"}, {"status": "success", "instance_id": "propzel-new"}, {"status": "success"}])
    monkeypatch.setattr(server, "whatsapp_service_request", provider)
    update = AsyncMock()
    monkeypatch.setattr(server, "db", SimpleNamespace(settings=SimpleNamespace(update_one=update)))
    result = asyncio.run(server.whatsapp_connect(ACTOR))
    assert result["instance_id"] == "propzel-new"
    assert update.call_args.args[0] == {"id": "organization:propzel"}
    assert provider.call_args.kwargs["params"]["webhook_url"].endswith("/propzel")


def test_qr_returns_502_for_provider_application_error(monkeypatch):
    monkeypatch.setattr(server, "whatsapp_settings_for_actor", AsyncMock(return_value=legacy_settings()))
    monkeypatch.setattr(server, "whatsapp_service_request", AsyncMock(return_value={"status": "provider_error", "message": "API returned HTML"}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.whatsapp_qrcode(ACTOR))
    assert exc.value.status_code == 502
    assert exc.value.detail == "API returned HTML"


def test_already_connected_qr_does_not_replace_paired_instance(monkeypatch):
    monkeypatch.setattr(server, "whatsapp_settings_for_actor", AsyncMock(return_value=legacy_settings()))
    provider = AsyncMock(return_value={"status": "error", "message": "Already connected"})
    monkeypatch.setattr(server, "whatsapp_service_request", provider)
    result = asyncio.run(server.whatsapp_qrcode(ACTOR))
    assert result["connection_state"] == "open"
    assert provider.await_count == 1


@pytest.mark.parametrize("defaults", [
    {"whatsapp_provider": "evolution"},
    {"evolution_api_url": "https://evolution.example"},
    {"evolution_api_key": "key"},
])
def test_incomplete_evolution_config_never_falls_back_to_marketly(monkeypatch, defaults):
    monkeypatch.setattr(server, "_env_integration_defaults", lambda: defaults)
    result = server._with_env_integration_defaults(legacy_settings())
    assert result["whatsapp_provider"] == "evolution"


def test_stored_evolution_credentials_override_stale_provider(monkeypatch):
    monkeypatch.setattr(server, "_env_integration_defaults", lambda: {})
    result = server._with_env_integration_defaults({**legacy_settings(), "evolution_api_url": "https://evolution.example", "evolution_api_key": "key"})
    assert result["whatsapp_provider"] == "evolution"


def test_propzel_defaults_to_evolution(monkeypatch):
    monkeypatch.delenv("WHATSAPP_PROVIDER", raising=False)
    assert server._env_integration_defaults()["whatsapp_provider"] == "evolution"


def test_evolution_does_not_reuse_marketly_instance_id(monkeypatch):
    monkeypatch.delenv("EVOLUTION_INSTANCE_NAME", raising=False)
    settings = {**legacy_settings(), "whatsapp_provider": "evolution"}
    assert server._evolution_instance(settings) == "rrl_propzel"


def test_missing_evolution_key_does_not_call_legacy_provider(monkeypatch):
    settings = {**legacy_settings(), "whatsapp_provider": "evolution", "evolution_api_url": "https://evolution.example"}
    monkeypatch.setattr(server, "whatsapp_settings_for_actor", AsyncMock(return_value=settings))
    legacy = AsyncMock()
    monkeypatch.setattr(server, "whatsapp_service_request", legacy)
    result = asyncio.run(server.whatsapp_profile(ACTOR))
    assert result["provider"] == "evolution"
    assert result["connection_state"] == "unknown"
    assert "EVOLUTION_API_KEY" in result["connection_error"]
    legacy.assert_not_awaited()
