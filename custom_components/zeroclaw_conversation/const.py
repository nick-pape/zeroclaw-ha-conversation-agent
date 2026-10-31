"""Constants for the ZeroClaw Conversation integration."""

from __future__ import annotations

DOMAIN = "zeroclaw_conversation"

# Parent entry: one per ZeroClaw gateway
CONF_URL = "url"
CONF_TOKEN = "token"

# Conversation subentry: one per agent
CONF_AGENT = "agent"
CONF_SESSION_MODE = "session_mode"
CONF_SESSION_NAME = "session_name"
CONF_INCLUDE_LOCATION = "include_location"

# One long-running ZeroClaw session for every turn: the agent remembers the
# conversation across wake words, rooms and days (embodied agent).
SESSION_PERSISTENT = "persistent"
# A fresh ZeroClaw session per HA conversation id (Assist-style, ~5 min).
SESSION_PER_CONVERSATION = "per_conversation"

DEFAULT_URL = "wss://zeroclaw.example/ws/chat"
DEFAULT_AGENT = "default"
DEFAULT_SESSION_NAME = "ha-voice"
DEFAULT_CONVERSATION_NAME = "ZeroClaw"

DEFAULT_CONVERSATION_OPTIONS: dict[str, object] = {
    CONF_AGENT: DEFAULT_AGENT,
    CONF_SESSION_MODE: SESSION_PERSISTENT,
    CONF_SESSION_NAME: DEFAULT_SESSION_NAME,
    CONF_INCLUDE_LOCATION: True,
}

# Covers the hub's 300s HITL window for tools that still need approval.
TURN_TIMEOUT_S = 360.0
