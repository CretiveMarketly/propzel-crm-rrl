import os

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "rrl_payload_tests")

import server


def test_callerdesk_call_report_camel_case_fields_are_extracted():
    payload = {
        "type": "call_report",
        "SourceNumber": "07092537669",
        "DestinationNumber": "08062986107",
        "DialWhomNumber": "09606571365",
        "CallDuration": 46,
        "TalkDuration": 33,
        "Status": "ANSWER",
        "CallSid": "56829818",
        "CallRecordingUrl": "https://callrecords.callerdesk.io/recording.wav",
        "Direction": "IVR",
    }

    assert server._extract_callerdesk_value(payload, {"CallRecordingUrl"}) == payload["CallRecordingUrl"]
    assert server._extract_callerdesk_value(payload, {"recording_url", "CallRecordingUrl"}) == payload["CallRecordingUrl"]
    assert server._extract_callerdesk_value(payload, {"TalkDuration"}) == 33
    assert server._extract_callerdesk_value(payload, {"CallDuration"}) == 46
    assert server._extract_callerdesk_value(payload, {"SourceNumber"}) == payload["SourceNumber"]
    assert server._extract_callerdesk_value(payload, {"DestinationNumber"}) == payload["DestinationNumber"]
    assert server._extract_callerdesk_value(payload, {"CallSid"}) == payload["CallSid"]
    assert server._callerdesk_recording_url({"raw": payload}) == payload["CallRecordingUrl"]

    call_row = server._mask_call_row({"raw": payload, "duration_sec": 0}, {"role": "super_admin"})
    assert call_row["recording_url"] == payload["CallRecordingUrl"]
    assert call_row["duration_sec"] == 33
