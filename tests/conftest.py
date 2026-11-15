"""Fixtures for the Home Assistant tests: an in-memory stand-in for the todo
services of the two lists, with fault injection.

The fake keeps each list's entity state at its number of open items, as
every real todo entity does, and matches update_item/remove_item targets by
uid or summary like Home Assistant's todo services.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.todo_sync.const import (  # noqa: E402
    CONF_LIST_A,
    CONF_LIST_B,
    CONF_SYNC_ENABLED,
    DOMAIN,
)

OPEN, DONE = "needs_action", "completed"
ALL_FEATURES = 7  # create, delete, update


class FakeTodo:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.items: dict[str, list[dict[str, str]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.read_override: dict[str, Callable[[], Awaitable[Any]]] = {}
        self.write_error: dict[tuple[str, str], Exception] = {}
        # Lists whose adds only show up after reveal() (a polled service).
        self.delay_adds: set[str] = set()
        self.hidden: dict[str, list[dict[str, str]]] = {}
        self._n = 0

    # -- lists and what people do in the apps ---------------------------------

    def add_list(self, entity_id: str, *names: str, done: tuple[str, ...] = ()) -> None:
        self.items[entity_id] = []
        for name in names:
            self._new(entity_id, name, OPEN)
        for name in done:
            self._new(entity_id, name, DONE)
        self.publish(entity_id)

    def _new(self, entity_id: str, summary: str, status: str) -> dict[str, str]:
        self._n += 1
        item = {"uid": f"{entity_id}-{self._n}", "summary": summary, "status": status}
        self.items[entity_id].append(item)
        return item

    def publish(self, entity_id: str) -> None:
        count = sum(i["status"] == OPEN for i in self.items[entity_id])
        self.hass.states.async_set(
            entity_id, str(count), {"supported_features": ALL_FEATURES, "friendly_name": entity_id}
        )

    def user_add(self, entity_id: str, summary: str, status: str = OPEN) -> None:
        self._new(entity_id, summary, status)
        self.publish(entity_id)

    def user_set(self, entity_id: str, summary: str, status: str) -> None:
        for item in self.items[entity_id]:
            if item["summary"] == summary:
                item["status"] = status
        self.publish(entity_id)

    def user_delete(self, entity_id: str, summary: str) -> None:
        self.items[entity_id] = [i for i in self.items[entity_id] if i["summary"] != summary]
        self.publish(entity_id)

    def user_rename(self, entity_id: str, old: str, new: str) -> None:
        for item in self.items[entity_id]:
            if item["summary"] == old:
                item["summary"] = new
        self.publish(entity_id)

    def reveal(self, entity_id: str) -> None:
        self.items[entity_id].extend(self.hidden.pop(entity_id, []))
        self.publish(entity_id)

    def open(self, entity_id: str) -> list[str]:
        return sorted(i["summary"] for i in self.items[entity_id] if i["status"] == OPEN)

    def done(self, entity_id: str) -> list[str]:
        return sorted(i["summary"] for i in self.items[entity_id] if i["status"] == DONE)

    def writes(self, entity_id: str | None = None) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            c for c in self.calls if c[0] != "get_items" and entity_id in (None, c[1])
        ]

    # -- the todo services --------------------------------------------------------

    def register(self) -> None:
        for service in ("get_items", "add_item", "update_item", "remove_item"):
            self.hass.services.async_register(
                "todo",
                service,
                self._handler(service),
                supports_response=(
                    SupportsResponse.ONLY if service == "get_items" else SupportsResponse.NONE
                ),
            )

    def _handler(self, service: str) -> Callable[[ServiceCall], Awaitable[Any]]:
        async def handle(call: ServiceCall) -> Any:
            entity_id = call.data["entity_id"]
            entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            self.calls.append((service, entity_id, data))
            if service == "get_items":
                if entity_id in self.read_override:
                    return await self.read_override[entity_id]()
                return {entity_id: {"items": [dict(i) for i in self.items[entity_id]]}}
            if (service, entity_id) in self.write_error:
                raise self.write_error[(service, entity_id)]
            if service == "add_item":
                if entity_id in self.delay_adds:
                    self._n += 1
                    self.hidden.setdefault(entity_id, []).append(
                        {"uid": f"{entity_id}-{self._n}", "summary": data["item"], "status": OPEN}
                    )
                    return None
                self._new(entity_id, data["item"], OPEN)
            elif service == "update_item":
                self._find(entity_id, data["item"])["status"] = data["status"]
            elif service == "remove_item":
                for value in data["item"]:
                    self.items[entity_id].remove(self._find(entity_id, value))
            self.publish(entity_id)
            return None

        return handle

    def _find(self, entity_id: str, value: str) -> dict[str, str]:
        for item in self.items[entity_id]:
            if value in (item["uid"], item["summary"]):
                return item
        raise ServiceValidationError(f"item {value} not found")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    return None


@pytest.fixture
async def fake(hass: HomeAssistant) -> FakeTodo:
    assert await async_setup_component(hass, "todo", {})
    todo = FakeTodo(hass)
    todo.register()
    return todo


@pytest.fixture
async def add_pair(hass: HomeAssistant):
    """Set up a pair (config entry); unloaded again after the test."""
    entries: list[MockConfigEntry] = []

    async def _add(list_a: str, list_b: str, enabled: bool = True) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=f"{list_a} ↔ {list_b}",
            data={CONF_LIST_A: list_a, CONF_LIST_B: list_b},
            options={CONF_SYNC_ENABLED: enabled},
            unique_id="|".join(sorted((list_a, list_b))),
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield _add
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
