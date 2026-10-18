"""Offline WhatsApp regressions. Provider requests and all storage are mocked."""
import asyncio
import base64
import io
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "whatsapp_regressions")

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
import server


ACTOR = {"id": "admin", "role": "admin", "active_organization_id": "org"}
CONV = {"id": "chat", "organization_id": "org", "contact_phone": "919876543210", "chat_id": "919876543210@s.whatsapp.net"}
LEAD = {"id": "lead", "organization_id": "org", "phone": "919876543210"}
SETTINGS = {"organization_id": "org", "whatsapp_provider": "evolution", "evolution_instance_name": "instance"}


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args): return self
    async def to_list(self, *args): return self.rows


@pytest.fixture
def fake(monkeypatch):
    def collection():
        return SimpleNamespace(find_one=AsyncMock(return_value=None), find=Mock(return_value=Cursor([])), insert_one=AsyncMock(), update_one=AsyncMock(), update_many=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
    database = SimpleNamespace(**{name: collection() for name in ["whatsapp_messages", "whatsapp_conversations", "whatsapp_media", "organizations", "users"]})
    monkeypatch.setattr(server, "db", database)
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value=dict(SETTINGS)))
    monkeypatch.setattr(server, "log_activity", AsyncMock())
    monkeypatch.setattr(server, "_find_lead_by_phone_digits", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "evolution_request", AsyncMock(return_value={"status": "success", "instance": {"state": "open"}, "key": {"id": "provider-id"}}))
    return database


def event(message=None, **key):
    return {"event": "messages.upsert", "data": {"key": {"remoteJid": "919876543210@s.whatsapp.net", "id": "inbound-id", "fromMe": False, **key}, "message": message or {"conversation": "hello"}, "messageTimestamp": 1780000000}}


def test_sent_text_has_organization_and_exact_conversation(fake, monkeypatch):
    monkeypatch.setattr(server, "ensure_whatsapp_conversation_for_lead", AsyncMock(return_value=dict(CONV)))
    result = asyncio.run(server.create_whatsapp_message_for_lead(LEAD, server.WhatsAppSendBody(text="Hello"), ACTOR))
    assert result["message"]["organization_id"] == "org"
    assert result["message"]["conversation_id"] == "chat"
    assert result["message"]["provider_message_id"] == "provider-id"
    paths = [call.args[1] for call in server.evolution_request.call_args_list]
    assert any("sendText" in path for path in paths)


@pytest.mark.parametrize("failure", ["provider_error", "pending_credentials", "pending_provider", "error", "failed"])
def test_failed_text_is_not_saved_as_sent(fake, monkeypatch, failure):
    monkeypatch.setattr(server, "forward_whatsapp_message", AsyncMock(return_value={"status": failure, "message": "Provider rejected"}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.create_whatsapp_message_for_lead(LEAD, server.WhatsAppSendBody(text="Hello"), ACTOR, dict(CONV)))
    assert exc.value.status_code == 502
    fake.whatsapp_messages.insert_one.assert_not_awaited()


def test_unlinked_contact_can_receive_reply(fake):
    fake.whatsapp_conversations.find_one.return_value = dict(CONV)
    result = asyncio.run(server.send_whatsapp_conversation_message("chat", server.WhatsAppSendBody(text="Welcome"), ACTOR))
    assert result["message"]["lead_id"] is None
    assert result["message"]["conversation_id"] == "chat"
    server.log_activity.assert_not_awaited()


def test_executive_cannot_send_to_another_users_chat(fake):
    fake.whatsapp_conversations.find_one.return_value = {**CONV, "assigned_to": "someone-else"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.send_whatsapp_conversation_message("chat", server.WhatsAppSendBody(text="Hi"), {"id": "exec", "role": "executive", "active_organization_id": "org"}))
    assert exc.value.status_code == 403
    server.evolution_request.assert_not_awaited()


def test_inbound_lid_uses_real_alternate_phone(fake):
    result = asyncio.run(server._process_whatsapp_webhook(event(remoteJid="12345@lid", remoteJidAlt="919876543210@s.whatsapp.net"), "org"))
    assert result["ok"]
    conv = fake.whatsapp_conversations.insert_one.call_args.args[0]
    assert conv["chat_id"] == "919876543210@s.whatsapp.net"
    message = fake.whatsapp_messages.insert_one.call_args.args[0]
    assert message["text"] == "hello"
    assert message["organization_id"] == "org"
    assert fake.whatsapp_messages.find_one.call_args.args[0]["organization_id"] == "org"


@pytest.mark.parametrize("jid", ["1234@g.us", "status@broadcast", "12345@lid"])
def test_non_phone_identifiers_do_not_create_fake_contacts(fake, jid):
    result = asyncio.run(server._process_whatsapp_webhook(event(remoteJid=jid), "org"))
    assert result["ignored"] == "non_phone_chat"
    fake.whatsapp_messages.insert_one.assert_not_awaited()


def test_mobile_sent_message_is_synced_without_unread_increment(fake):
    asyncio.run(server._process_whatsapp_webhook(event(fromMe=True), "org"))
    message = fake.whatsapp_messages.insert_one.call_args.args[0]
    assert message["direction"] == "outgoing"
    assert fake.whatsapp_conversations.update_one.call_args.args[1]["$inc"]["unread_count"] == 0


def test_dotted_status_updates_are_not_new_messages(fake):
    result = asyncio.run(server._process_whatsapp_webhook({"event": "messages.update", "data": {"keyId": "provider-id", "status": 4}}, "org"))
    assert result["status_updated"]
    query, update = fake.whatsapp_messages.update_many.call_args.args
    assert query["organization_id"] == "org"
    assert update["$set"]["provider_status"] == "read"
    fake.whatsapp_messages.insert_one.assert_not_awaited()


@pytest.mark.parametrize("status,expected", [(0, "failed"), (1, "pending"), (2, "sent"), (3, "delivered"), (4, "read"), (5, "played")])
def test_baileys_numeric_delivery_status(status, expected):
    assert server._evolution_message_status({"data": {"keyId": "id", "status": status}}) == ("id", expected)


def test_shared_executive_account_reuses_workspace_conversation(fake):
    fake.whatsapp_conversations.find_one.return_value = CONV
    actor = {"id": "exec", "role": "executive", "active_organization_id": "org"}
    result = asyncio.run(server.ensure_whatsapp_conversation_for_lead(LEAD, actor))
    assert result["id"] == "chat"
    assert fake.whatsapp_conversations.find_one.call_args.args[0]["whatsapp_account_user_id"] is None
    fake.whatsapp_conversations.insert_one.assert_not_awaited()


def test_legacy_webhook_without_org_resolves_known_instance(fake):
    fake.organizations.find.return_value = Cursor([{"id": "org"}])
    fake.organizations.find_one.return_value = {"id": "org"}
    payload = {**event(), "instance": server._evolution_instance(SETTINGS)}
    request = SimpleNamespace(method="POST", headers={"content-type": "application/json"}, json=AsyncMock(return_value=payload))
    assert asyncio.run(server.whatsapp_webhook(request))["ok"]
    assert fake.whatsapp_messages.insert_one.call_args.args[0]["organization_id"] == "org"


def test_legacy_webhook_with_unknown_instance_cannot_choose_tenant(fake):
    fake.organizations.find.return_value = Cursor([{"id": "org"}])
    payload = {**event(), "instance": "unknown"}
    request = SimpleNamespace(method="POST", headers={"content-type": "application/json"}, json=AsyncMock(return_value=payload))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.whatsapp_webhook(request))
    assert exc.value.status_code == 400
    fake.whatsapp_messages.insert_one.assert_not_awaited()


def test_batched_receiving_webhooks(fake):
    fake.organizations.find_one.return_value = {"id": "org"}
    payload = {"event": "messages.upsert", "data": [event()["data"], event(id="second-id")["data"]]}
    request = SimpleNamespace(method="POST", headers={"content-type": "application/json"}, json=AsyncMock(return_value=payload))
    result = asyncio.run(server.whatsapp_webhook(request, "org"))
    assert result["processed"] == 2
    assert fake.whatsapp_messages.insert_one.await_count == 2


def test_decrypted_inbound_media_is_available_without_provider_base64_in_json(fake):
    encoded = base64.b64encode(b"test-image").decode()
    server.evolution_request.return_value = {"base64": encoded}
    payload = event({"imageMessage": {"url": "https://mmg.whatsapp.net/encrypted.enc", "mimetype": "image/png"}})
    asyncio.run(server._process_whatsapp_webhook(payload, "org"))
    assert "getBase64FromMediaMessage" in server.evolution_request.call_args.args[1]
    media_update = fake.whatsapp_media.update_one.call_args.args[1]["$set"]
    assert media_update["content"] == b"test-image"
    msg = fake.whatsapp_messages.insert_one.call_args.args[0]
    assert msg["media_url"] is None
    assert msg["media_path"].endswith("/media")


def test_outbound_attachment_uses_inferred_mime_and_retains_download(fake):
    upload = UploadFile(io.BytesIO(b"%PDF-test"), filename="file.pdf", headers=Headers({"content-type": "application/octet-stream"}))
    result = asyncio.run(server._send_whatsapp_attachment(CONV, LEAD, upload, "Document", ACTOR))
    payload = server.evolution_request.call_args.args[3]
    assert payload["mediatype"] == "document"
    assert payload["mimetype"] == "application/pdf"
    assert base64.b64decode(payload["media"]) == b"%PDF-test"
    assert result["message"]["media_path"].endswith("/media")
    assert fake.whatsapp_media.update_one.call_args.args[0]["organization_id"] == "org"


def test_rejected_media_is_not_saved(fake):
    server.evolution_request.side_effect = [{"instance": {"state": "open"}}, {"status": "provider_error", "message": "Rejected"}]
    upload = UploadFile(io.BytesIO(b"data"), filename="file.pdf")
    with pytest.raises(HTTPException):
        asyncio.run(server._send_whatsapp_attachment(CONV, LEAD, upload, "", ACTOR))
    fake.whatsapp_messages.insert_one.assert_not_awaited()
    fake.whatsapp_media.update_one.assert_not_awaited()


def test_upload_size_limit_checked_before_provider(fake):
    upload = UploadFile(io.BytesIO(b"a" * (server.WHATSAPP_ATTACHMENT_LIMIT + 1)), filename="large.pdf")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server._send_whatsapp_attachment(CONV, LEAD, upload, "", ACTOR))
    assert exc.value.status_code == 413
    server.evolution_request.assert_not_awaited()


def test_media_download_requires_conversation_access(fake):
    fake.whatsapp_messages.find_one.return_value = {"id": "media-id", "conversation_id": "chat", "organization_id": "org"}
    fake.whatsapp_conversations.find_one.return_value = {**CONV, "assigned_to": "other"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.download_whatsapp_media("media-id", {"id": "exec", "role": "executive", "active_organization_id": "org"}))
    assert exc.value.status_code == 403
    fake.whatsapp_media.find_one.assert_not_awaited()


def test_download_returns_original_bytes_and_safe_filename(fake):
    fake.whatsapp_messages.find_one.return_value = {"id": "media-id", "conversation_id": "chat", "organization_id": "org"}
    fake.whatsapp_conversations.find_one.return_value = dict(CONV)
    fake.whatsapp_media.find_one.return_value = {"content": b"%PDF-test", "filename": "name\r\n.pdf", "mimetype": "application/pdf"}
    response = asyncio.run(server.download_whatsapp_media("media-id", ACTOR))
    assert response.body == b"%PDF-test"
    assert "\r" not in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"


def test_legacy_sent_rows_are_only_recovered_inside_authorized_chat(fake):
    fake.whatsapp_conversations.find_one.return_value = dict(CONV)
    asyncio.run(server.whatsapp_messages("chat", ACTOR))
    query = fake.whatsapp_messages.find.call_args.args[0]
    assert query["conversation_id"] == "chat"
    assert query["$or"] == [{"organization_id": "org"}, {"organization_id": None}]


def test_chat_loads_latest_messages_then_displays_in_chronological_order(fake):
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[{"id": "newest"}, {"id": "older"}]))
    cursor.sort = Mock(return_value=cursor)
    fake.whatsapp_messages.find.return_value = cursor
    result = asyncio.run(server._list_whatsapp_messages(CONV))
    cursor.sort.assert_called_once_with("created_at", -1)
    assert [message["id"] for message in result] == ["older", "newest"]


def test_webhook_registration_failure_is_reported(fake, monkeypatch):
    monkeypatch.setattr(server, "_whatsapp_webhook_url", lambda: "https://crm.example/api/whatsapp/webhook")
    server.evolution_request.side_effect = [{"instance": {"state": "open"}}, {"status": "provider_error", "message": "Webhook rejected"}]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.whatsapp_connect(ACTOR))
    assert exc.value.status_code == 502


def test_executive_connection_sets_personal_instance_override(fake, monkeypatch):
    monkeypatch.setattr(server, "_whatsapp_webhook_url", lambda: "https://crm.example/api/whatsapp/webhook")
    actor = {"id": "exec", "role": "executive", "active_organization_id": "org"}
    asyncio.run(server.whatsapp_connect(actor))
    for call in server.evolution_request.call_args_list:
        assert call.args[2]["_evolution_instance_override"] == server._executive_evolution_instance(actor, "org")
    payload = server.evolution_request.call_args.args[3]["webhook"]
    assert payload["url"].endswith("/org/exec")
    assert payload["webhook_base64"] is False
