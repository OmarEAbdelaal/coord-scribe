import asyncio
import json
import time

from mcp import Client

from coordy.mcp_server import build_server
from tests.conftest import write_wav

REAL_NAMES = ["Omar", "Ahmed", "عمر", "Nile", "Marina", "Elsayed"]


def call(server, name, args=None):
    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, args or {})
            text = "\n".join(getattr(c, "text", "") for c in result.content)
            return result, text

    return asyncio.run(go())


def data(server, name, args=None):
    """Structured result: list tools come back as {"result": [...]}."""

    async def go():
        async with Client(server) as client:
            result = await client.call_tool(name, args or {})
            assert not result.is_error, result.content
            sc = result.structured_content
            return sc["result"] if sc and set(sc) == {"result"} else (sc or json.loads(result.content[0].text))

    return asyncio.run(go())


def tools(server):
    async def go():
        async with Client(server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    return asyncio.run(go())


def test_tools_registered(settings, engine):
    names = tools(build_server(settings, engine))
    for expected in ["list_codes", "list_inbox", "process_recording", "job_status", "list_meetings", "get_minutes",
                     "get_transcript", "search", "list_items", "add_item", "update_item", "label_speakers", "regenerate_minutes"]:
        assert expected in names


def test_full_flow_never_leaks_names(settings, engine):
    server = build_server(settings, engine)
    write_wav(settings.paths.inbox / "2026-10-28 site meeting.wav")

    _, text = call(server, "list_codes")
    assert "P01" in text and "Lead Architect" in text
    _, text = call(server, "list_inbox")
    assert "site meeting.wav" in text

    job_id = data(server, "process_recording", {"file": "2026-10-28 site meeting.wav", "project": "PRJ01"})["id"]
    for _ in range(100):
        job = data(server, "job_status", {"job_id": job_id})[0]
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    mid = job["meeting_id"]

    outputs = []
    for name, args in [
        ("list_meetings", {}),
        ("get_minutes", {"meeting_id": mid}),
        ("get_transcript", {"meeting_id": mid}),
        ("search", {"query": "shafts"}),
        ("list_items", {"status": ""}),
    ]:
        result, text = call(server, name, args)
        assert not result.is_error, text
        outputs.append(text)
    everything = "\n".join(outputs)
    for real in REAL_NAMES:
        assert real not in everything, real
    assert "P01" in everything and "shafts" in everything


def test_item_updates_and_errors(settings, engine, recording):
    mid = engine.process(recording)
    server = build_server(settings, engine)
    item_id = engine.store.items(meeting_id=mid, kind="action")[0]["id"]

    result, text = call(server, "update_item", {"item_id": item_id, "status": "closed", "note": "Done by Ahmed"})
    assert not result.is_error and "closed" in text and "Ahmed" not in text
    assert engine.store.items(meeting_id=mid, kind="action")[0]["note"] == "Done by P02"

    result, _ = call(server, "add_item", {"meeting_id": mid, "kind": "risk", "text": "Authority NOC delay"})
    assert not result.is_error
    result, _ = call(server, "add_item", {"meeting_id": mid, "kind": "nonsense", "text": "x"})
    assert result.is_error
    result, _ = call(server, "update_item", {"item_id": item_id, "status": "whatever"})
    assert result.is_error
    result, _ = call(server, "label_speakers", {"meeting_id": mid, "mapping": {"S1": "Omar Elsayed"}})
    assert result.is_error
    result, text = call(server, "label_speakers", {"meeting_id": mid, "mapping": {"S1": "P01"}})
    assert not result.is_error and "P01" in text
    result, _ = call(server, "get_minutes", {"meeting_id": "missing"})
    assert result.is_error
    result, _ = call(server, "process_recording", {"file": "nope.wav"})
    assert result.is_error
