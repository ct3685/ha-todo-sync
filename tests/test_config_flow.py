"""Config and options flow."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.todo_sync.const import (  # noqa: E402
    CONF_LIST_A,
    CONF_LIST_B,
    CONF_SYNC_ENABLED,
    DOMAIN,
)

from .conftest import FakeTodo  # noqa: E402

A, B, C = "todo.alexa_shopping", "todo.skylight_grocery", "todo.skylight_costco"


async def _submit(hass: HomeAssistant, a: str, b: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LIST_A: a, CONF_LIST_B: b}
    )


async def test_new_pair_starts_in_preview_mode(hass: HomeAssistant, fake: FakeTodo) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    result = await _submit(hass, A, B)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_LIST_A: A, CONF_LIST_B: B}
    assert result["options"] == {CONF_SYNC_ENABLED: False}
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.runtime_data.sync_enabled is False
    await entry.runtime_data.async_sync()
    assert fake.writes() == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_same_list_twice(hass: HomeAssistant, fake: FakeTodo) -> None:
    fake.add_list(A)
    result = await _submit(hass, A, A)
    assert result["errors"] == {"base": "same_list"}


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
async def test_unavailable_list(hass: HomeAssistant, fake: FakeTodo, state) -> None:
    fake.add_list(A)
    if state:
        hass.states.async_set(B, state)
    result = await _submit(hass, A, B)
    assert result["errors"] == {"base": "list_unavailable"}


async def test_read_only_list(hass: HomeAssistant, fake: FakeTodo) -> None:
    fake.add_list(A)
    hass.states.async_set(B, "0", {"supported_features": 0})
    result = await _submit(hass, A, B)
    assert result["errors"] == {"base": "list_unsupported"}


async def test_list_already_in_another_pair(hass: HomeAssistant, fake: FakeTodo, add_pair) -> None:
    for entity_id in (A, B, C):
        fake.add_list(entity_id)
    await add_pair(A, B)
    for a, b in ((A, C), (C, B), (B, A)):
        result = await _submit(hass, a, b)
        assert result["errors"] == {"base": "list_in_use"}


async def test_options_switch_syncing_on(hass: HomeAssistant, fake: FakeTodo, add_pair) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    entry = await add_pair(A, B, enabled=False)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SYNC_ENABLED: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.sync_enabled is True
    await entry.runtime_data.async_sync()
    assert fake.open(B) == ["Milk"]
