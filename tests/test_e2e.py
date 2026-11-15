"""End to end through Home Assistant's real todo services and entities.

Two in-memory TodoListEntity lists stand in for Alexa and Skylight; edits go
through todo.add_item / todo.update_item, syncs are triggered by the state
changes and the debounce timer, as in a live instance.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.todo import (  # noqa: E402
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigFlow  # noqa: E402
from homeassistant.const import Platform  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_fire_time_changed,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

ALEXA, SKYLIGHT = "todo.alexa_shopping", "todo.skylight_grocery"


class MemoryList(TodoListEntity):
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, name: str, items: list[tuple[str, TodoItemStatus]]) -> None:
        self._attr_name = name
        self._attr_unique_id = name
        self._n = 0
        self._attr_todo_items = [self._make(s, st) for s, st in items]

    def _make(self, summary: str, status: TodoItemStatus) -> TodoItem:
        self._n += 1
        return TodoItem(summary=summary, uid=f"{self._attr_name}-{self._n}", status=status)

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self._attr_todo_items = [*self._attr_todo_items, self._make(item.summary, item.status)]
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        self._attr_todo_items = [item if i.uid == item.uid else i for i in self._attr_todo_items]
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in uids]
        self.async_write_ha_state()


async def _setup_lists(hass: HomeAssistant, *lists: MemoryList) -> MockConfigEntry:
    async def setup_entry(hass, entry):
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.TODO])
        return True

    async def unload_entry(hass, entry):
        return await hass.config_entries.async_unload_platforms(entry, [Platform.TODO])

    async def setup_platform(hass, entry, async_add_entities):
        async_add_entities(lists)

    mock_integration(
        hass,
        MockModule("memory_lists", async_setup_entry=setup_entry, async_unload_entry=unload_entry),
    )
    mock_platform(hass, "memory_lists.config_flow")
    mock_platform(hass, "memory_lists.todo", MockPlatform(async_setup_entry=setup_platform))
    entry = MockConfigEntry(domain="memory_lists")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _settle(hass: HomeAssistant) -> None:
    for _ in range(3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()


def _items(entity: MemoryList) -> list[tuple[str, str, str]]:
    return sorted((i.summary, str(i.status), i.uid) for i in entity.todo_items)


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call("todo", service, data, target={"entity_id": entity_id}, blocking=True)


class MemoryFlow(ConfigFlow):
    VERSION = 1


@pytest.fixture(autouse=True)
def memory_flow():
    with mock_config_flow("memory_lists", MemoryFlow):
        yield


async def test_two_way_sync_through_real_todo_services(hass: HomeAssistant, add_pair) -> None:
    alexa = MemoryList("Alexa Shopping", [("Milk", TodoItemStatus.NEEDS_ACTION)])
    skylight = MemoryList(
        "Skylight Grocery",
        [
            ("Eggs", TodoItemStatus.NEEDS_ACTION),
            ("Tea", TodoItemStatus.COMPLETED),
            ("Tea", TodoItemStatus.COMPLETED),
        ],
    )
    lists = await _setup_lists(hass, alexa, skylight)
    assert hass.states.get(ALEXA).state == "1"
    await add_pair(ALEXA, SKYLIGHT)
    await _settle(hass)
    assert _items(alexa) == [
        ("Eggs", "needs_action", "Alexa Shopping-2"),
        ("Milk", "needs_action", "Alexa Shopping-1"),
    ]
    assert [i[:2] for i in _items(skylight)] == [
        ("Eggs", "needs_action"),
        ("Milk", "needs_action"),
        ("Tea", "completed"),
        ("Tea", "completed"),
    ]

    # Checked off on the Skylight frame -> completed in Alexa.
    await _call(hass, "update_item", SKYLIGHT, item="Eggs", status="completed")
    await _settle(hass)
    assert ("Eggs", "completed", "Alexa Shopping-2") in _items(alexa)

    # Alexa adds Tea -> exactly one of Skylight's two completed Tea reopens.
    await _call(hass, "add_item", ALEXA, item="Tea")
    await _settle(hass)
    tea = [i for i in _items(skylight) if i[0] == "Tea"]
    assert sorted(t[1] for t in tea) == ["completed", "needs_action"]
    assert len(skylight.todo_items) == 4

    # Checked off in Alexa -> completed on Skylight, nothing deleted.
    await _call(hass, "update_item", ALEXA, item="Milk", status="completed")
    await _settle(hass)
    assert ("Milk", "completed") in [i[:2] for i in _items(skylight)]
    assert len(skylight.todo_items) == 4 and len(alexa.todo_items) == 3

    await hass.config_entries.async_unload(lists.entry_id)
