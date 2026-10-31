"""client.py against a fake ZeroClaw gateway speaking the zeroclaw.v1 frames."""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web

from zeroclaw_client import (
    VOICE_REPLY_RULE,
    ZeroClawAuthError,
    ZeroClawError,
    ZeroClawUnknownAgent,
    chat_url,
    frame_message,
    probe,
    run_turn,
)

TOKEN = "zc_test"


def gateway(script):
    """App whose /ws/chat checks the bearer, records what the client sent, then
    plays `script(sent)` back: a list of frames (dicts) to send after the message."""
    seen: dict = {"frames": [], "query": None, "protocol": None}

    async def ws_chat(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.Response(status=401)
        if request.query.get("agent") == "nobody":
            return web.Response(status=400)
        ws = web.WebSocketResponse(protocols=("zeroclaw.v1",))
        await ws.prepare(request)
        seen["query"] = dict(request.query)
        seen["protocol"] = ws.ws_protocol
        async for msg in ws:
            frame = json.loads(msg.data)
            seen["frames"].append(frame)
            if frame["type"] == "connect":
                await ws.send_json({"type": "connected", "message": "Connection established"})
                if frame.get("session_id") == "ha-probe":
                    continue
            elif frame["type"] == "message":
                for out in script(seen):
                    if out == "CLOSE":
                        await ws.close()
                        return ws
                    await ws.send_json(out)
            elif frame["type"] == "approval_response":
                await ws.send_json({"type": "chunk", "content": "denied, ok"})
                await ws.send_json({"type": "done", "full_response": "denied, ok"})
        return ws

    app = web.Application()
    app.router.add_get("/ws/chat", ws_chat)
    return app, seen


async def collect(server, **kw):
    params = dict(token=TOKEN, agent="assistant", session_id="ha-voice", text="hi")
    params.update(kw)
    async with aiohttp.ClientSession() as session:
        return [e async for e in run_turn(session, base_url=str(server.make_url("/ws/chat")), **params)]


async def test_streams_chunks_then_done(aiohttp_server):
    app, seen = gateway(lambda s: [
        {"type": "session_start", "session_id": "ha-voice"},
        {"type": "chunk", "content": "Lights "},
        {"type": "tool_call", "id": "1", "name": "assist_HassTurnOff", "args": {}},
        {"type": "tool_result", "id": "1", "name": "assist_HassTurnOff", "output": "ok"},
        {"type": "chunk", "content": "off."},
        {"type": "done", "full_response": "Lights off."},
    ])
    events = await collect(await aiohttp_server(app))
    assert [e.text for e in events if e.kind == "text"] == ["Lights ", "off."]
    assert [e.kind for e in events] == ["text", "tool_call", "tool_result", "text", "done"]
    assert seen["query"] == {"agent": "assistant", "session_id": "ha-voice"}
    assert seen["protocol"] == "zeroclaw.v1"
    assert seen["frames"][0] == {"type": "connect", "session_id": "ha-voice", "device_name": "home-assistant"}
    assert seen["frames"][1] == {"type": "message", "content": "hi"}


async def test_done_without_chunks_yields_full_response(aiohttp_server):
    app, _ = gateway(lambda s: [{"type": "done", "full_response": "Hey."}])
    events = await collect(await aiohttp_server(app))
    assert [(e.kind, e.text) for e in events] == [("text", "Hey."), ("done", "")]


async def test_error_frame_raises(aiohttp_server):
    app, _ = gateway(lambda s: [{"type": "error", "message": "provider down"}])
    with pytest.raises(ZeroClawError, match="provider down"):
        await collect(await aiohttp_server(app))


async def test_close_mid_turn_raises(aiohttp_server):
    app, _ = gateway(lambda s: [{"type": "chunk", "content": "half"}, "CLOSE"])
    with pytest.raises(ZeroClawError, match="before the turn finished"):
        await collect(await aiohttp_server(app))


async def test_bad_token_is_auth_error(aiohttp_server):
    app, _ = gateway(lambda s: [])
    with pytest.raises(ZeroClawAuthError):
        await collect(await aiohttp_server(app), token="wrong")


async def test_approval_request_is_denied(aiohttp_server):
    app, seen = gateway(lambda s: [{"type": "approval_request", "request_id": "r1", "summary": "x"}])
    events = await collect(await aiohttp_server(app))
    assert {"type": "approval_response", "request_id": "r1", "decision": "deny"} in seen["frames"]
    assert events[-1].kind == "done"


async def test_probe_ok_and_bad_token(aiohttp_server):
    app, seen = gateway(lambda s: [])
    server = await aiohttp_server(app)
    url = str(server.make_url("/ws/chat"))
    async with aiohttp.ClientSession() as session:
        await probe(session, base_url=url, token=TOKEN, agent="assistant")
        with pytest.raises(ZeroClawAuthError):
            await probe(session, base_url=url, token="nope", agent="assistant")
        with pytest.raises(ZeroClawUnknownAgent):
            await probe(session, base_url=url, token=TOKEN, agent="nobody")
    assert [f["type"] for f in seen["frames"]] == ["connect"]


def test_chat_url():
    assert chat_url("wss://zeroclaw.example", "assistant", "ha voice") == (
        "wss://zeroclaw.example/ws/chat?agent=assistant&session_id=ha+voice"
    )
    assert chat_url("wss://x/ws/chat/", "a", "b") == "wss://x/ws/chat?agent=a&session_id=b"


def test_frame_message():
    rule = f"[{VOICE_REPLY_RULE}]"
    assert frame_message("lights off") == f"[Home Assistant voice]\n{rule}\nlights off"
    assert frame_message("lights off", area="Kitchen", extra=" timer done ") == (
        f"[Home Assistant voice, Kitchen]\n{rule}\n[context: timer done]\nlights off"
    )


async def _events(*events):
    for e in events:
        yield e


async def test_chat_log_deltas_split_messages_around_tools():
    from zeroclaw_client import TurnEvent, chat_log_deltas

    out = [d async for d in chat_log_deltas(_events(
        TurnEvent("text", "Let me check."),
        TurnEvent("tool_call", data={"id": "c1", "name": "assist_GetLiveContext", "args": {"a": 1}}),
        TurnEvent("tool_result", data={"id": "c1", "name": "assist_GetLiveContext", "output": "x" * 5000}),
        TurnEvent("text", "It's off."),
        TurnEvent("done", data={}),
    ))]
    assert out[:3] == [
        {"role": "assistant"},
        {"content": "Let me check."},
        {"tool_call": {"id": "c1", "name": "assist_GetLiveContext", "args": {"a": 1}}},
    ]
    assert out[3]["role"] == "tool_result" and out[3]["tool_call_id"] == "c1"
    assert len(out[3]["output"]) == 2000
    assert out[4:] == [{"role": "assistant"}, {"content": "It's off."}]


async def test_chat_log_deltas_tool_first_and_tool_last():
    from zeroclaw_client import TurnEvent, chat_log_deltas

    out = [d async for d in chat_log_deltas(_events(
        TurnEvent("tool_call", data={"id": "c1", "name": "t", "args": None}),
        TurnEvent("tool_result", data={"id": "c1", "name": "t", "output": {"ok": True}}),
    ))]
    assert out == [
        {"role": "assistant"},
        {"tool_call": {"id": "c1", "name": "t", "args": {}}},
        {"role": "tool_result", "tool_call_id": "c1", "tool_name": "t", "output": '{"ok": true}'},
    ]
