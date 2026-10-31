"""ZeroClaw Conversation: a ZeroClaw agent as a Home Assistant conversation agent."""

from __future__ import annotations

import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import ZeroClawAuthError, ZeroClawError, probe
from .const import CONF_AGENT, CONF_TOKEN, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (Platform.CONVERSATION,)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a ZeroClaw gateway entry."""
    agents = [s.data[CONF_AGENT] for s in entry.subentries.values() if s.data.get(CONF_AGENT)]
    try:
        # Any agent will do: this checks the gateway and the token.
        if agents:
            await probe(
                async_get_clientsession(hass),
                base_url=entry.data[CONF_URL],
                token=entry.data[CONF_TOKEN],
                agent=agents[0],
            )
    except ZeroClawAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except (ZeroClawError, aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"ZeroClaw gateway unreachable: {err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when subentries or data change."""
    await hass.config_entries.async_reload(entry.entry_id)
