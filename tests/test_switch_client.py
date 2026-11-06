"""The SSE parser against frames shaped like the ones switch-core 0.31.0 sends."""

from collections.abc import AsyncIterator

from khata.switch_client import _parse_sse


async def _lines(*lines: str) -> AsyncIterator[str]:
    for line in lines:
        yield line


async def _collect(*lines: str):
    return [e async for e in _parse_sse(_lines(*lines))]


async def test_control_frame_has_no_sequence():
    events = await _collect(
        "event: connection_state",
        'data: {"type":"connection_state","cursor":12,"heartbeat_interval_seconds":2}',
        "",
    )
    assert len(events) == 1
    assert events[0].kind == "connection_state"
    assert events[0].sequence is None
    assert events[0].raw["cursor"] == 12


async def test_domain_event_carries_sequence_and_payload():
    events = await _collect(
        "id: 4813",
        "event: message",
        'data: {"type":"message","room_id":"r1","sequence":4813,'
        '"payload":{"addressed":true,"body":"@khata-intake hi","attachments":[]}}',
        "",
    )
    (event,) = events
    assert event.sequence == 4813
    assert event.room_id == "r1"
    assert event.is_addressed_message


async def test_keepalive_comments_are_skipped():
    events = await _collect(
        ": keepalive",
        "",
        "id: 7",
        'data: {"type":"room_join","payload":{"listening":false}}',
        "",
    )
    assert [e.kind for e in events] == ["room_join"]
    assert events[0].sequence == 7
    assert not events[0].is_addressed_message


async def test_unaddressed_message_is_not_actionable():
    (event,) = await _collect(
        "id: 9",
        'data: {"type":"message","sequence":9,"payload":{"addressed":false}}',
        "",
    )
    assert not event.is_addressed_message
