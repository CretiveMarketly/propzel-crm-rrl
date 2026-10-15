"""Offline regressions: no live CRM database or provider calls."""
import asyncio
import copy
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rrl_payload_tests")
import pytest
import server


@pytest.mark.parametrize("raw,expected", [
    ({"TalkDuration": "00:01:33", "CallDuration": 100}, 93),
    ({"data": [{"talk_time": "45.9"}]}, 45),
    ({"duration": "01:20"}, 80),
    ({"TalkDuration": 0, "CallDuration": 100}, 0),
    ({"duration": "NaN"}, 0),
    ({"duration": "Infinity"}, 0),
    ({"duration": -5}, 0),
])
def test_duration_formats(raw, expected):
    assert server._callerdesk_duration({"raw": raw}) == expected


def test_call_sid_preferred_over_campaign_id():
    assert server._extract_callerdesk_sid({"campaign_id": "campaign", "data": {"CallSid": 123}}) == "123"


class Calls:
    def __init__(self):
        self.rows = []

    async def find_one(self, query, projection=None):
        return next((copy.deepcopy(row) for row in self.rows if all(row.get(k) == v for k, v in query.items())), None)

    async def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if all(row.get(k) == v for k, v in query.items())), None)
        if row is None:
            assert upsert
            row = dict(query, **update.get("$setOnInsert", {}))
            self.rows.append(row)
        row.update(copy.deepcopy(update["$set"]))


def test_partial_callbacks_preserve_call_data_and_tenant(monkeypatch):
    calls = Calls()
    monkeypatch.setattr(server, "db", SimpleNamespace(callerdesk_calls=calls))

    async def run():
        first = await server._save_callerdesk_call(
            None, None, call_sid="123", organization_id="org-a", direction="outgoing",
            status_text="connected", phone_from="111", phone_to="222", duration_sec=33,
            recording_url="https://example.com/recording.wav", raw={"TalkDuration": 33},
        )
        second = await server._save_callerdesk_call(
            None, None, call_sid=123, organization_id="org-a", direction="incoming",
            status_text="initiated", phone_from=None, phone_to=None, raw={"event": "recording"},
        )
        assert first == second
        assert len(calls.rows) == 1
        row = calls.rows[0]
        assert row["organization_id"] == "org-a"
        assert row["duration_sec"] == 33
        assert row["recording_url"].endswith("recording.wav")
        assert row["direction"] == "outgoing"
        assert row["status"] == "connected"
        assert row["phone_to"] == "222"
        assert row["raw"]["TalkDuration"] == 33
        await server._save_callerdesk_call(
            None, None, call_sid="123", organization_id="org-b", direction="incoming",
            status_text="connected", phone_from=None, phone_to=None, duration_sec=7,
        )
        assert len(calls.rows) == 2
        assert calls.rows[1]["duration_sec"] == 7
    asyncio.run(run())


def test_recording_only_webhook_keeps_existing_duration(monkeypatch):
    payload = {"data": {"CallSid": 123, "CallRecordingUrl": "https://example.com/audio.wav"}}
    existing = {"call_sid": "123", "organization_id": "org", "duration_sec": 42, "status": "connected", "direction": "outgoing"}
    monkeypatch.setattr(server, "db", SimpleNamespace(callerdesk_calls=SimpleNamespace(find_one=AsyncMock(return_value=existing))))
    monkeypatch.setattr(server, "_callerdesk_organization_from_payload", AsyncMock(return_value="org"))
    monkeypatch.setattr(server, "get_integration_settings", AsyncMock(return_value={}))
    save = AsyncMock()
    monkeypatch.setattr(server, "_save_callerdesk_call", save)
    request = SimpleNamespace(headers={"content-type": "application/json"}, json=AsyncMock(return_value=payload))
    assert asyncio.run(server.callerdesk_webhook(request)) == {"ok": True}
    kwargs = save.call_args.kwargs
    assert kwargs["call_sid"] == "123"
    assert kwargs["organization_id"] == "org"
    assert kwargs["duration_sec"] == 42
    assert kwargs["status_text"] == "connected"
    assert kwargs["direction"] == "outgoing"
    assert kwargs["recording_url"] == payload["data"]["CallRecordingUrl"]


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self, *args):
        return copy.deepcopy(self.rows)


def test_campaign_joins_duration_even_without_recording(monkeypatch):
    numbers = [{"id": "number", "call_sid": "123", "duration_sec": 0}]
    calls = [{"call_sid": "123", "duration_sec": 0, "raw": {"TalkDuration": "00:00:33"}, "status": "connected"}]
    find = Mock(return_value=Cursor(calls))
    monkeypatch.setattr(server, "db", SimpleNamespace(
        callerdesk_campaign_numbers=SimpleNamespace(find=Mock(return_value=Cursor(numbers))),
        callerdesk_calls=SimpleNamespace(find=find),
    ))
    monkeypatch.setattr(server, "_expire_old_callerdesk_campaign_calls", AsyncMock())
    monkeypatch.setattr(server, "organization_scope", lambda user: {"organization_id": "org"})
    result = asyncio.run(server.callerdesk_campaign_calls("campaign", status_filter=None, user={"role": "super_admin"}))
    assert result["items"][0]["duration_sec"] == 33
    assert result["items"][0]["status"] == "connected"
    assert find.call_args.args[0]["organization_id"] == "org"


def test_lead_timeline_recovers_raw_recording_and_duration(monkeypatch):
    activities = [{"id": "activity", "meta": {"provider": "callerdesk", "call_sid": "123", "duration_sec": 0}, "message": "CallerDesk call initiated · 0s"}]
    calls = [{"call_sid": "123", "status": "connected", "raw": {"TalkDuration": 33, "CallRecordingUrl": "https://example.com/call.wav"}}]
    monkeypatch.setattr(server, "db", SimpleNamespace(
        activities=SimpleNamespace(find=Mock(return_value=Cursor(activities))),
        callerdesk_calls=SimpleNamespace(find=Mock(return_value=Cursor(calls))),
    ))
    monkeypatch.setattr(server, "require_lead_access", AsyncMock())
    monkeypatch.setattr(server, "organization_scope", lambda user: {"organization_id": "org"})
    monkeypatch.setattr(server, "sanitize_activity_doc", lambda doc, user: doc)
    result = asyncio.run(server.list_activities(lead_id="lead", user={"role": "super_admin"}))
    assert result[0]["meta"]["duration_sec"] == 33
    assert result[0]["meta"]["recording_url"].endswith("call.wav")
    assert result[0]["message"] == "CallerDesk call connected · 33s"
