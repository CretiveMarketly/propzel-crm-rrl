"""Offline connection and session regressions; no live API or DB calls."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "connect_auth_tests")

import pytest
from fastapi import HTTPException, Response
import server


def test_fresh_bearer_token_takes_priority_over_stale_cookie(monkeypatch):
    decode = Mock(return_value={"sub": "user", "type": "access"})
    monkeypatch.setattr(server, "decode_token", decode)
    monkeypatch.setattr(server, "db", SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={"id": "user", "role": "admin", "organization_id": "org"}))))
    request = SimpleNamespace(cookies={"access_token": "expired-cookie"}, headers={"Authorization": "Bearer fresh-token"})
    result = asyncio.run(server.get_current_user(request))
    decode.assert_called_once_with("fresh-token")
    assert result["active_organization_id"] == "org"


@pytest.mark.parametrize("json_token,cookie_token,expected", [("json-refresh", None, "json-refresh"), (None, "cookie-refresh", "cookie-refresh"), ("json-refresh", "stale-cookie", "json-refresh")])
def test_refresh_supports_cross_domain_json_and_legacy_cookies(monkeypatch, json_token, cookie_token, expected):
    decode = Mock(return_value={"sub": "user", "type": "refresh"})
    monkeypatch.setattr(server, "decode_token", decode)
    monkeypatch.setattr(server, "db", SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={"id": "user", "email": "user@example.test", "role": "admin"}))))
    monkeypatch.setattr(server, "set_auth_cookies", lambda *args: {"access_token": "fresh", "refresh_token": "refresh"})
    request = SimpleNamespace(cookies={"refresh_token": cookie_token} if cookie_token else {})
    result = asyncio.run(server.refresh(request, Response(), server.RefreshTokenBody(refresh_token=json_token)))
    decode.assert_called_once_with(expected)
    assert result["access_token"] == "fresh"


def test_refresh_rejects_disabled_account(monkeypatch):
    monkeypatch.setattr(server, "decode_token", lambda token: {"sub": "user", "type": "refresh"})
    monkeypatch.setattr(server, "db", SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={"id": "user", "active": False}))))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.refresh(SimpleNamespace(cookies={}), Response(), server.RefreshTokenBody(refresh_token="refresh")))
    assert exc.value.status_code == 403


def test_connect_provider_calls_have_bounded_timeouts(monkeypatch):
    settings = {"organization_id": "org", "whatsapp_provider": "evolution"}
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(server, "_whatsapp_webhook_url", lambda: "https://crm.example/api/whatsapp/webhook")
    provider = AsyncMock(side_effect=[{"instance": {"state": "open"}}, {"status": "success"}])
    monkeypatch.setattr(server, "evolution_request", provider)
    asyncio.run(server.whatsapp_connect({"id": "admin", "role": "admin", "active_organization_id": "org"}))
    assert [call.kwargs["timeout"] for call in provider.call_args_list] == [10, 15]


def test_profile_exposes_provider_error_without_api_key(monkeypatch):
    monkeypatch.setattr(server, "whatsapp_settings_for_actor", AsyncMock(return_value={"whatsapp_provider": "evolution", "evolution_instance_name": "instance", "evolution_api_url": "https://provider.example", "evolution_api_key": "secret"}))
    monkeypatch.setattr(server, "evolution_request", AsyncMock(return_value={"status": "provider_error", "message": "Evolution API returned HTTP 401"}))
    result = asyncio.run(server.whatsapp_profile({"role": "admin", "active_organization_id": "org"}))
    assert result["connection_error"] == "Evolution API returned HTTP 401"
    assert "secret" not in str(result)


def test_qr_generation_keeps_provider_error_reason(monkeypatch):
    monkeypatch.setattr(server, "whatsapp_settings_for_actor", AsyncMock(return_value={"whatsapp_provider": "evolution", "evolution_instance_name": "instance"}))
    provider = AsyncMock(return_value={"status": "provider_error", "message": "Evolution API returned HTTP 401"})
    monkeypatch.setattr(server, "evolution_request", provider)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.whatsapp_qrcode({"role": "admin", "active_organization_id": "org"}))
    assert exc.value.status_code == 502
    assert exc.value.detail == "Evolution API returned HTTP 401"
    assert provider.call_args.kwargs["timeout"] == 25
