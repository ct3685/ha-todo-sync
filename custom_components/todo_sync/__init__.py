"""To-do List Sync: keeps two Home Assistant to-do lists in step."""
from __future__ import annotations

import probatio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_FORCE,
    CONF_LIST_A,
    CONF_LIST_B,
    DOMAIN,
    SERVICE_PREVIEW,
    SERVICE_SYNC_NOW,
    STORAGE_VERSION,
)
from .manager import TodoSyncManager

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type TodoSyncConfigEntry = ConfigEntry[TodoSyncManager]

SYNC_NOW_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        probatio.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)
PREVIEW_SCHEMA = probatio.Schema({probatio.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    def _targets(call: ServiceCall) -> list[TodoSyncConfigEntry]:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if (entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID)) is None:
            return entries
        found = [e for e in entries if e.entry_id == entry_id]
        if not found:
            raise ServiceValidationError(f"No loaded To-do List Sync pair with id {entry_id}")
        return found

    async def _sync_now(call: ServiceCall) -> None:
        if call.data[ATTR_FORCE]:
            # Forcing overrides the outage guard: one pair at a time, and
            # only by an administrator (automations run without a user).
            if ATTR_CONFIG_ENTRY_ID not in call.data:
                raise ServiceValidationError(
                    "force: true needs config_entry_id - force one pair at a time"
                )
            if call.context.user_id and not (
                (user := await hass.auth.async_get_user(call.context.user_id)) and user.is_admin
            ):
                raise Unauthorized(context=call.context)
        targets = _targets(call)
        if call.data[ATTR_FORCE] and not targets[0].runtime_data.sync_enabled:
            raise ServiceValidationError(
                "This pair is in preview mode; enable syncing in its options first"
            )
        for entry in targets:
            await entry.runtime_data.async_sync(force=call.data[ATTR_FORCE])

    async def _preview(call: ServiceCall) -> ServiceResponse:
        return {e.entry_id: await e.runtime_data.async_preview() for e in _targets(call)}

    hass.services.async_register(DOMAIN, SERVICE_SYNC_NOW, _sync_now, schema=SYNC_NOW_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_PREVIEW,
        _preview,
        schema=PREVIEW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TodoSyncConfigEntry) -> bool:
    # Two pairs writing to one list would treat each other's writes as user
    # changes. Both stay stopped until one of them is removed or disabled.
    mine = {entry.data[CONF_LIST_A], entry.data[CONF_LIST_B]}
    for other in hass.config_entries.async_entries(DOMAIN):
        if (
            other.entry_id != entry.entry_id
            and other.disabled_by is None
            and mine & {other.data[CONF_LIST_A], other.data[CONF_LIST_B]}
        ):
            raise ConfigEntryError(
                f"A list of this pair is also in '{other.title}'; "
                "each list can be in one pair only"
            )
    manager = TodoSyncManager(hass, entry)
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TodoSyncConfigEntry) -> bool:
    await entry.runtime_data.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TodoSyncConfigEntry) -> None:
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}").async_remove()
