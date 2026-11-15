"""Boot a real Home Assistant core with this integration and two in-memory
to-do lists.

The pytest harness lags Home Assistant releases; this checks the integration
against whatever ``homeassistant`` version is installed (e.g. 2026.11.1):
config flow, preview without writes, enabling sync, both directions, the
outage guard, a forced sync of one pair, unload.

    python scripts/ha_core_smoke.py
"""

from __future__ import annotations

import homeassistant  # noqa: F401 - must be imported first (aliases httpx)

import asyncio
import logging
import os
import pathlib
import tempfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]

from homeassistant import bootstrap, runner  # noqa: E402
from homeassistant.const import __version__  # noqa: E402

ALEXA, SKYLIGHT = "todo.alexa_shopping", "todo.skylight_grocery"

MEMORY_MANIFEST = """{
  "domain": "memory_lists", "name": "Memory lists", "codeowners": [],
  "dependencies": ["todo"], "documentation": "https://example.invalid",
  "iot_class": "local_push", "requirements": [], "version": "0.0.1"
}"""

MEMORY_TODO = '''
from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature


class MemoryList(TodoListEntity):
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, name, items):
        self._attr_name = name
        self._attr_unique_id = name
        self._n = 0
        self._attr_todo_items = [self.make(s) for s in items]

    def make(self, summary, status=TodoItemStatus.NEEDS_ACTION):
        self._n += 1
        return TodoItem(summary=summary, uid=f"{self._attr_name}-{self._n}", status=status)

    def replace_all(self, items):
        self._attr_todo_items = items
        self.async_write_ha_state()

    async def async_create_todo_item(self, item):
        self.replace_all([*self._attr_todo_items, self.make(item.summary)])

    async def async_update_todo_item(self, item):
        self.replace_all([item if i.uid == item.uid else i for i in self._attr_todo_items])

    async def async_delete_todo_items(self, uids):
        self.replace_all([i for i in self._attr_todo_items if i.uid not in uids])


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([MemoryList("Alexa Shopping", ["Milk"]), MemoryList("Skylight Grocery", ["Eggs"])])
'''


def prepare_config_dir(config_dir: str) -> None:
    custom = pathlib.Path(config_dir, "custom_components")
    custom.mkdir()
    os.symlink(ROOT / "custom_components" / "todo_sync", custom / "todo_sync")
    memory = custom / "memory_lists"
    memory.mkdir()
    (memory / "__init__.py").write_text("")
    (memory / "manifest.json").write_text(MEMORY_MANIFEST)
    (memory / "todo.py").write_text(MEMORY_TODO)
    pathlib.Path(config_dir, "configuration.yaml").write_text(
        "homeassistant:\ntodo:\n  - platform: memory_lists\n"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as config_dir:
        await asyncio.to_thread(prepare_config_dir, config_dir)
        with patch(
            "aiohttp.ClientSession._request", side_effect=AssertionError("real network call")
        ):
            hass = await bootstrap.async_setup_hass(
                runner.RuntimeConfig(config_dir=config_dir, skip_pip=True, safe_mode=False)
            )
            assert hass is not None, "Home Assistant failed to start"
            try:
                await hass.async_start()
                await hass.async_block_till_done()
                await run(hass)
            finally:
                await hass.async_stop(force=True)
    print(f"OK: Home Assistant {__version__} smoke test passed")


def entity(hass, entity_id):
    return hass.data["entity_components"]["todo"].get_entity(entity_id)


def items(hass, entity_id) -> list[tuple[str, str]]:
    return sorted((i.summary, str(i.status)) for i in entity(hass, entity_id).todo_items)


async def todo(hass, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call("todo", service, data, target={"entity_id": entity_id}, blocking=True)


async def sync_now(hass, **data) -> None:
    await hass.services.async_call("todo_sync", "sync_now", data, blocking=True)


async def run(hass) -> None:
    import custom_components.todo_sync.manager as manager

    assert hass.states.get(ALEXA).state == "1", hass.states.get(ALEXA)
    flow = await hass.config_entries.flow.async_init("todo_sync", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"list_a": ALEXA, "list_b": SKYLIGHT}
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("todo_sync")[0]
    assert entry.state.value == "loaded", entry.state
    assert entry.options == {"sync_enabled": False}

    # Preview mode: reads only.
    await sync_now(hass)
    response = await hass.services.async_call(
        "todo_sync", "preview", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    preview = response[entry.entry_id]
    assert preview["add_to_a"] == ["Eggs"] and preview["add_to_b"] == ["Milk"], preview
    assert preview["safe"] is True, preview
    assert items(hass, ALEXA) == [("Milk", "needs_action")]
    assert items(hass, SKYLIGHT) == [("Eggs", "needs_action")]

    # Switch syncing on.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(flow["flow_id"], {"sync_enabled": True})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    await sync_now(hass)
    assert items(hass, ALEXA) == [("Eggs", "needs_action"), ("Milk", "needs_action")]
    assert items(hass, SKYLIGHT) == [("Eggs", "needs_action"), ("Milk", "needs_action")]

    await todo(hass, "add_item", ALEXA, item="Bread")
    await todo(hass, "update_item", SKYLIGHT, item="Eggs", status="completed")
    await sync_now(hass)
    assert ("Bread", "needs_action") in items(hass, SKYLIGHT)
    assert ("Eggs", "completed") in items(hass, ALEXA)

    # Outage: Alexa suddenly reads as empty. Nothing is checked off on Skylight.
    manager.ABSENCE_CONFIRM_SECONDS = 0
    entity(hass, ALEXA).replace_all([])
    await sync_now(hass)
    status = entry.runtime_data.status
    assert "reads as empty" in (status.last_error or ""), status.last_error
    assert ("Milk", "needs_action") in items(hass, SKYLIGHT)

    # An admin forces this one pair: the absence is mirrored as check-offs.
    await sync_now(hass, config_entry_id=entry.entry_id, force=True)
    assert all(s == "completed" for _, s in items(hass, SKYLIGHT)), items(hass, SKYLIGHT)
    assert entry.runtime_data.status.last_error is None

    assert await hass.config_entries.async_unload(entry.entry_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
