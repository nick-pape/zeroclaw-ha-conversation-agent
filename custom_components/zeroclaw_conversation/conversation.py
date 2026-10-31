"""Conversation entity: hands each Assist turn to a ZeroClaw agent."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import ZeroClawAuthError, ZeroClawError, chat_log_deltas, frame_message, run_turn
from .const import (
    CONF_AGENT,
    CONF_INCLUDE_LOCATION,
    CONF_SESSION_MODE,
    CONF_SESSION_NAME,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_SESSION_NAME,
    DOMAIN,
    SESSION_PER_CONVERSATION,
    TURN_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one conversation entity per conversation subentry."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue
        async_add_entities(
            [ZeroClawConversationEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class ZeroClawConversationEntity(conversation.ConversationEntity):
    """A ZeroClaw agent. The agent owns its tools, memory and persona."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="ZeroClaw",
            model=f"agent: {subentry.data[CONF_AGENT]}",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        # The agent acts through its own tools (HA's Assist MCP via the hub),
        # so HA's intent tools are never offered.
        self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

    def _session_id(self, conversation_id: str) -> str:
        options = self.subentry.data
        if options.get(CONF_SESSION_MODE) == SESSION_PER_CONVERSATION:
            return f"ha-{conversation_id}"
        return options.get(CONF_SESSION_NAME) or DEFAULT_SESSION_NAME

    def _area_name(self, device_id: str | None) -> str | None:
        """Name the room the satellite that heard this turn is in."""
        if not device_id:
            return None
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area else None

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Send the turn to ZeroClaw and stream the reply into the chat log."""
        options = self.subentry.data
        area = self._area_name(user_input.device_id) if options.get(CONF_INCLUDE_LOCATION, True) else None
        message = frame_message(user_input.text, area=area, extra=user_input.extra_system_prompt)

        try:
            async for _ in chat_log.async_add_delta_content_stream(
                self.entity_id,
                self._stream(message, self._session_id(chat_log.conversation_id)),
            ):
                pass
        except ZeroClawAuthError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"ZeroClaw rejected the token: {err}") from err
        except (ZeroClawError, TimeoutError) as err:
            raise HomeAssistantError(f"ZeroClaw agent failed: {err}") from err

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _stream(self, message: str, session_id: str) -> AsyncGenerator[dict[str, Any]]:
        """Adapt ZeroClaw turn events to HA chat log deltas.

        ZeroClaw runs its own tools, so they enter the chat log as *external*
        tool calls: HA records and displays them without executing anything.
        Text followed by a tool call is also what makes the Assist pipeline start
        streaming TTS before the turn ends.
        """
        events = run_turn(
            async_get_clientsession(self.hass),
            base_url=self.entry.data[CONF_URL],
            token=self.entry.data[CONF_TOKEN],
            agent=self.subentry.data[CONF_AGENT],
            session_id=session_id,
            text=message,
            timeout=TURN_TIMEOUT_S,
        )
        async for delta in chat_log_deltas(events):
            if call := delta.get("tool_call"):
                yield {
                    "tool_calls": [
                        llm.ToolInput(
                            id=call["id"],
                            tool_name=call["name"],
                            tool_args=call["args"],
                            external=True,
                        )
                    ]
                }
            elif delta.get("role") == "tool_result":
                data = {"output": delta["output"]}
                yield {
                    "role": "tool_result",
                    "tool_call_id": delta["tool_call_id"],
                    "tool_name": delta["tool_name"],
                    # HA 2026.11+ wants `result`; older versions read `tool_result`.
                    **(
                        {"result": llm.ToolResult(data=data)}
                        if hasattr(llm, "ToolResult")
                        else {"tool_result": data}
                    ),
                }
            else:
                yield delta
