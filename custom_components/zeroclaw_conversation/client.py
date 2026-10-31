"""WebSocket client for a ZeroClaw gateway's /ws/chat endpoint.

No Home Assistant imports, so it can be tested against a plain aiohttp server.

Protocol (ZeroClaw v0.8.x, subprotocol ``zeroclaw.v1``):
  client -> {"type": "connect", "session_id": ..., "device_name": ...}
  server -> {"type": "connected"}
  client -> {"type": "message", "content": "..."}
  server -> chunk* / thinking* / tool_call* / tool_result* ... then done | error | aborted
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

PROTOCOL = "zeroclaw.v1"


class ZeroClawError(Exception):
    """The gateway refused the connection or ended the turn with an error."""


class ZeroClawAuthError(ZeroClawError):
    """The gateway rejected the bearer token."""


class ZeroClawUnknownAgent(ZeroClawError):
    """The gateway has no agent with that alias."""


@dataclass
class TurnEvent:
    """One event from a turn: ``kind`` is text, tool_call, tool_result or done."""

    kind: str
    text: str = ""
    data: dict[str, Any] | None = None


VOICE_REPLY_RULE = (
    "Your reply is spoken aloud: one or two short sentences, plain words, "
    "no markdown, lists or URLs. Don't narrate what you checked."
)


def frame_message(text: str, *, area: str | None = None, extra: str | None = None) -> str:
    """Prefix the user's words with where they came from.

    The agent shares one session across rooms and channels, so it needs to know
    this turn is spoken (reply for TTS) and which room it was heard in. The
    reply rule rides on every turn because smaller models lose it when it only
    lives in a long system prompt.
    """
    where = f"Home Assistant voice, {area}" if area else "Home Assistant voice"
    lines = [f"[{where}]", f"[{VOICE_REPLY_RULE}]"]
    if extra:
        lines.append(f"[context: {extra.strip()}]")
    lines.append(text)
    return "\n".join(lines)


def chat_url(base_url: str, agent: str, session_id: str) -> str:
    """Build the /ws/chat URL for an agent and session."""
    base = base_url.rstrip("/")
    if not base.endswith("/ws/chat"):
        base = f"{base}/ws/chat"
    return f"{base}?{urlencode({'agent': agent, 'session_id': session_id})}"


async def _open(
    session: aiohttp.ClientSession, url: str, token: str, timeout: float
) -> aiohttp.ClientWebSocketResponse:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        return await session.ws_connect(
            url,
            protocols=(PROTOCOL,),
            headers=headers,
            heartbeat=30,
            timeout=aiohttp.ClientWSTimeout(ws_receive=timeout, ws_close=10.0),
            max_msg_size=0,
        )
    except aiohttp.WSServerHandshakeError as err:
        if err.status in (401, 403):
            raise ZeroClawAuthError(f"gateway rejected the token (HTTP {err.status})") from err
        if err.status == 400:
            # Every /ws/chat session is bound to an agent; an unknown alias is a 400.
            raise ZeroClawUnknownAgent(f"gateway refused the agent (HTTP {err.status})") from err
        raise ZeroClawError(f"gateway handshake failed (HTTP {err.status})") from err


async def probe(
    session: aiohttp.ClientSession, *, base_url: str, token: str, agent: str, timeout: float = 10.0
) -> None:
    """Check the gateway accepts the token and agent without running a turn."""
    url = chat_url(base_url, agent, "ha-probe")
    async with await _open(session, url, token, timeout) as ws:
        await ws.send_json({"type": "connect", "session_id": "ha-probe", "device_name": "home-assistant"})
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame.get("type") == "connected":
                return
            if frame.get("type") == "error":
                raise ZeroClawError(frame.get("message") or "error")
        raise ZeroClawError("gateway closed the connection during connect")


async def run_turn(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    agent: str,
    session_id: str,
    text: str,
    device_name: str = "home-assistant",
    timeout: float = 360.0,
) -> AsyncGenerator[TurnEvent]:
    """Send one message and yield the agent's reply as it streams.

    Text arrives as ``text`` events (chunk deltas). If the agent finished without
    streaming any chunks, the final ``done`` text is yielded as one ``text`` event,
    so callers only ever need to handle ``text``.
    """
    ws = await _open(session, chat_url(base_url, agent, session_id), token, timeout)
    async with ws:
        await ws.send_json({"type": "connect", "session_id": session_id, "device_name": device_name})
        await ws.send_json({"type": "message", "content": text})
        streamed = False
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    raise ZeroClawError(f"websocket error: {ws.exception()}")
                continue
            try:
                frame: dict[str, Any] = json.loads(msg.data)
            except json.JSONDecodeError:
                _LOGGER.warning("Ignoring non-JSON frame: %.200s", msg.data)
                continue

            kind = frame.get("type")
            if kind == "chunk":
                content = frame.get("content") or ""
                if content:
                    streamed = True
                    yield TurnEvent("text", content)
            elif kind in ("tool_call", "tool_result"):
                yield TurnEvent(kind, data=frame)
            elif kind == "approval_request":
                # Approvals belong to the hub (ntfy), not to a voice satellite.
                # Deny rather than leave the turn hanging until it times out.
                _LOGGER.warning("Denying gateway approval request %s", frame.get("request_id"))
                await ws.send_json(
                    {"type": "approval_response", "request_id": frame.get("request_id"), "decision": "deny"}
                )
            elif kind == "done":
                full = frame.get("full_response") or ""
                if not streamed and full:
                    yield TurnEvent("text", full)
                yield TurnEvent("done", data=frame)
                return
            elif kind in ("error", "aborted"):
                raise ZeroClawError(frame.get("message") or kind)
            # connected, session_start, thinking, agent_start/end, history_trimmed: ignored

        raise ZeroClawError("gateway closed the connection before the turn finished")


# Tool output is shown in HA's chat log and sent to the frontend; ZeroClaw's can
# be huge (e.g. a whole-house live context), so keep only the head of it.
MAX_TOOL_OUTPUT_CHARS = 2000


async def chat_log_deltas(events: AsyncIterator[TurnEvent]) -> AsyncGenerator[dict[str, Any]]:
    """Shape turn events like HA's chat log deltas (types left to the caller).

    Yields ``{"role": "assistant"}`` to open each assistant message, then
    ``{"content": ...}`` and ``{"tool_call": {id, name, args}}`` inside it, and
    ``{"role": "tool_result", "tool_call_id", "tool_name", "output"}`` after it.
    A tool call starts a new assistant message once its result is in, so the text
    before and after a tool round are separate messages, as HA expects.
    """
    open_message = False
    async for event in events:
        if event.kind == "text":
            if not open_message:
                yield {"role": "assistant"}
                open_message = True
            yield {"content": event.text}
        elif event.kind == "tool_call" and event.data:
            if not open_message:
                yield {"role": "assistant"}
                open_message = True
            yield {
                "tool_call": {
                    "id": str(event.data.get("id") or ""),
                    "name": str(event.data.get("name") or "tool"),
                    "args": event.data.get("args") if isinstance(event.data.get("args"), dict) else {},
                }
            }
        elif event.kind == "tool_result" and event.data:
            output = event.data.get("output")
            if not isinstance(output, str):
                output = json.dumps(output, default=str)
            yield {
                "role": "tool_result",
                "tool_call_id": str(event.data.get("id") or ""),
                "tool_name": str(event.data.get("name") or "tool"),
                "output": output[:MAX_TOOL_OUTPUT_CHARS],
            }
            open_message = False
