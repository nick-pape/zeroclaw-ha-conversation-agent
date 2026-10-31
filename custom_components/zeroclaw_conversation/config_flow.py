"""Config flow for ZeroClaw Conversation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import ZeroClawAuthError, ZeroClawError, ZeroClawUnknownAgent, probe
from .const import (
    CONF_AGENT,
    CONF_INCLUDE_LOCATION,
    CONF_SESSION_MODE,
    CONF_SESSION_NAME,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_CONVERSATION_OPTIONS,
    DEFAULT_SESSION_NAME,
    DEFAULT_URL,
    DOMAIN,
    SESSION_PER_CONVERSATION,
    SESSION_PERSISTENT,
)

_LOGGER = logging.getLogger(__name__)


async def _validate(hass, url: str, token: str, agent: str) -> dict[str, str]:
    """Probe the gateway; return a form errors dict (empty on success)."""
    try:
        await probe(async_get_clientsession(hass), base_url=url, token=token, agent=agent)
    except ZeroClawAuthError:
        return {"base": "invalid_auth"}
    except ZeroClawUnknownAgent:
        return {"base": "unknown_agent"}
    except (ZeroClawError, aiohttp.ClientError, TimeoutError):
        return {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error probing the ZeroClaw gateway")
        return {"base": "unknown"}
    return {}


class ZeroClawConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a ZeroClaw gateway."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway URL and its paired bearer token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            agent = user_input[CONF_AGENT].strip()
            errors = await _validate(self.hass, url, user_input[CONF_TOKEN], agent)
            if not errors:
                return self.async_create_entry(
                    title="ZeroClaw",
                    data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                    subentries=[
                        {
                            "subentry_type": "conversation",
                            "data": {**DEFAULT_CONVERSATION_OPTIONS, CONF_AGENT: agent},
                            "title": user_input.get(CONF_NAME) or agent,
                            "unique_id": None,
                        }
                    ],
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=DEFAULT_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_AGENT): TextSelector(),
                    vol.Optional(CONF_NAME, default=DEFAULT_CONVERSATION_NAME): TextSelector(),
                }
            ),
            errors=errors or None,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The gateway rejected the token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            agent = next(
                (s.data[CONF_AGENT] for s in entry.subentries.values() if s.data.get(CONF_AGENT)),
                "",
            )
            errors = await _validate(self.hass, entry.data[CONF_URL], user_input[CONF_TOKEN], agent)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors or None,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """One conversation subentry per ZeroClaw agent."""
        return {"conversation": ConversationSubentryFlowHandler}


class ConversationSubentryFlowHandler(ConfigSubentryFlow):
    """Add or edit a conversation agent backed by one ZeroClaw agent alias."""

    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a new agent."""
        self.options = dict(DEFAULT_CONVERSATION_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing agent."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Agent alias, session mode, location context."""
        if user_input is not None:
            self.options.update(user_input)
            if self._is_new:
                name = self.options.pop(CONF_NAME, DEFAULT_CONVERSATION_NAME)
                return self.async_create_entry(title=name, data=self.options)
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), data=self.options
            )

        schema: dict[Any, Any] = {}
        if self._is_new:
            schema[vol.Required(CONF_NAME, default=DEFAULT_CONVERSATION_NAME)] = TextSelector()
        schema[vol.Required(CONF_AGENT)] = TextSelector()
        schema[vol.Required(CONF_SESSION_MODE, default=SESSION_PERSISTENT)] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(label="One persistent session", value=SESSION_PERSISTENT),
                    SelectOptionDict(
                        label="New session per conversation", value=SESSION_PER_CONVERSATION
                    ),
                ],
                mode="dropdown",
            )
        )
        schema[vol.Required(CONF_SESSION_NAME, default=DEFAULT_SESSION_NAME)] = TextSelector()
        schema[vol.Required(CONF_INCLUDE_LOCATION, default=True)] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(schema), self.options),
        )
