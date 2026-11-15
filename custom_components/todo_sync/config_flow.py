"""Config flow: pick the two lists; options: switch writing on or off."""
from __future__ import annotations

from typing import Any

import probatio

from homeassistant.components.todo import TodoListEntityFeature
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_LIST_A, CONF_LIST_B, CONF_SYNC_ENABLED, DOMAIN

_TODO = selector.EntitySelector(selector.EntitySelectorConfig(domain="todo"))
_REQUIRED_FEATURES = TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM


class TodoSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TodoSyncOptionsFlow:
        return TodoSyncOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            a, b = user_input[CONF_LIST_A], user_input[CONF_LIST_B]
            if a == b:
                errors["base"] = "same_list"
            elif error := self._list_error(a) or self._list_error(b):
                errors["base"] = error
            else:
                await self.async_set_unique_id("|".join(sorted((a, b))))
                self._abort_if_unique_id_configured()
                title = " ↔ ".join(
                    (s.name if (s := self.hass.states.get(e)) else e) for e in (a, b)
                )
                # A new pair only previews until syncing is switched on.
                return self.async_create_entry(
                    title=title,
                    data={CONF_LIST_A: a, CONF_LIST_B: b},
                    options={CONF_SYNC_ENABLED: False},
                )

        schema = probatio.Schema(
            {probatio.Required(CONF_LIST_A): _TODO, probatio.Required(CONF_LIST_B): _TODO}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    def _list_error(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return "list_unavailable"
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        if features & _REQUIRED_FEATURES != _REQUIRED_FEATURES:
            return "list_unsupported"
        for entry in self._async_current_entries(include_ignore=False):
            if entity_id in (entry.data.get(CONF_LIST_A), entry.data.get(CONF_LIST_B)):
                return "list_in_use"
        return None


class TodoSyncOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        enabled = self.config_entry.options.get(CONF_SYNC_ENABLED, True)
        schema = probatio.Schema(
            {probatio.Required(CONF_SYNC_ENABLED, default=enabled): selector.BooleanSelector()}
        )
        return self.async_show_form(step_id="init", data_schema=schema)
