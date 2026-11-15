"""Sync against mocked todo services: normal use, outages, failures, pairs."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.exceptions import (  # noqa: E402
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er  # noqa: E402

from custom_components.todo_sync import manager as manager_module  # noqa: E402
from custom_components.todo_sync.const import DOMAIN  # noqa: E402

from .conftest import DONE, OPEN, FakeTodo  # noqa: E402

A, B = "todo.alexa_shopping", "todo.skylight_grocery"
A2, B2 = "todo.alexa_costco", "todo.skylight_costco"
CONFIRM = timedelta(minutes=7)


async def sync(entry, force: bool = False) -> None:
    await entry.runtime_data.async_sync(force=force)


def status(entry):
    return entry.runtime_data.status


def store_key(entry) -> str:
    return f"{DOMAIN}_{entry.entry_id}"


# -- initial sync ------------------------------------------------------------------


async def test_both_lists_empty(hass: HomeAssistant, fake: FakeTodo, add_pair) -> None:
    fake.add_list(A)
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)
    assert fake.writes() == []
    assert status(entry).last_error is None and status(entry).last_sync is not None


async def test_one_list_populated(hass: HomeAssistant, fake: FakeTodo, add_pair) -> None:
    fake.add_list(A, "Milk", "Bread")
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)
    assert fake.open(B) == ["Bread", "Milk"]
    assert fake.writes(A) == []


async def test_both_populated_with_overlap_never_removes_or_completes(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk", "eggs", done=("Flour",))
    fake.add_list(B, "Eggs", "Bread", done=("Sugar",))
    entry = await add_pair(A, B)
    await sync(entry)
    assert fake.open(A) == ["Bread", "Milk", "eggs"]
    assert fake.open(B) == ["Bread", "Eggs", "Milk"]
    assert fake.done(A) == ["Flour"] and fake.done(B) == ["Sugar"]
    assert all(c[0] != "remove_item" and c[2].get("status") != DONE for c in fake.writes())


# -- normal operation ----------------------------------------------------------------


async def test_everyday_changes_in_both_directions(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    fake.add_list(A)
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)

    fake.user_add(A, "Milk")  # "Alexa, add milk"
    await sync(entry)
    assert fake.open(B) == ["Milk"]

    fake.user_add(B, "Bread")  # added on the Skylight frame
    await sync(entry)
    assert fake.open(A) == ["Bread", "Milk"]

    fake.user_set(A, "Milk", DONE)  # checked off in the Alexa app
    await sync(entry)
    assert fake.done(B) == ["Milk"]

    fake.user_set(B, "Milk", OPEN)  # unchecked on Skylight
    await sync(entry)
    assert fake.open(A) == ["Bread", "Milk"]

    fake.user_set(B, "Bread", DONE)  # checked off on Skylight
    await sync(entry)
    assert fake.done(A) == ["Bread"]

    fake.user_set(A, "Bread", OPEN)  # unchecked on Alexa
    await sync(entry)
    assert fake.open(B) == ["Bread", "Milk"]

    fake.user_delete(A, "Milk")  # deleted on Alexa: waits for confirmation
    await sync(entry)
    assert fake.open(B) == ["Bread", "Milk"]
    assert status(entry).waiting == ["Milk"]
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.done(B) == ["Milk"]

    fake.user_delete(B, "Bread")  # deleted on Skylight: checked off on Alexa
    freezer.tick(CONFIRM)
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.done(A) == ["Bread"]
    assert all(c[0] != "remove_item" for c in fake.writes())
    assert status(entry).last_error is None


async def test_rename_on_either_side_creates_no_duplicates(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    fake.add_list(A, "Milk", "Bread")
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)

    fake.user_rename(B, "Bread", "Sourdough")
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.open(A) == ["Milk", "Sourdough"] and fake.done(A) == ["Bread"]
    assert fake.open(B) == ["Milk", "Sourdough"]

    fake.user_rename(A, "Milk", "Oat milk")
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.open(B) == ["Oat milk", "Sourdough"] and fake.done(B) == ["Milk"]
    await sync(entry)
    assert fake.open(A) == ["Oat milk", "Sourdough"]


async def test_readding_a_completed_item_reopens_it(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)
    fake.user_set(B, "Milk", DONE)
    await sync(entry)
    assert fake.done(A) == ["Milk"]
    # Alexa adds a fresh Milk next to the completed one.
    fake.user_add(A, "Milk")
    await sync(entry)
    assert fake.open(B) == ["Milk"] and fake.done(B) == []
    assert [c[0] for c in fake.writes(B)] == ["add_item", "update_item"]


async def test_rapid_updates_and_concurrent_runs_write_once(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A)
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)
    for name in ("Milk", "Eggs", "Tea"):
        fake.user_add(A, name)
    await asyncio.gather(sync(entry), sync(entry), sync(entry))
    assert [c[2]["item"] for c in fake.writes(B)] == ["Eggs", "Milk", "Tea"]
    assert fake.open(B) == ["Eggs", "Milk", "Tea"]


async def test_state_changes_trigger_a_debounced_sync(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from homeassistant.util import dt as dt_util

    fake.add_list(A)
    fake.add_list(B)
    await add_pair(A, B)
    fake.user_add(A, "Milk")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert fake.open(B) == ["Milk"]


# -- reads that cannot be trusted ----------------------------------------------------


async def _synced_pair(fake: FakeTodo, add_pair, *names: str):
    fake.add_list(A, *names)
    fake.add_list(B, *names)
    entry = await add_pair(A, B)
    await sync(entry)
    return entry


@pytest.mark.parametrize(
    "response",
    [
        {},
        {B: {}},
        {B: {"items": None}},
        {"todo.other": {"items": []}},
        {B: {"items": [{"summary": "Milk", "status": OPEN}]}},
        {B: {"items": [{"uid": "1", "summary": "Milk", "status": "weird"}]}},
    ],
)
async def test_malformed_or_missing_response_writes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer, hass_storage, response
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    before = copy.deepcopy(hass_storage[store_key(entry)])

    async def read() -> Any:
        return response

    fake.read_override[B] = read
    for _ in range(3):
        freezer.tick(CONFIRM)
        await sync(entry)
    assert fake.writes() == []
    assert "Unusable read" in status(entry).last_error
    assert hass_storage[store_key(entry)] == before


async def test_no_response_at_all_writes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")

    async def read() -> Any:
        return None

    fake.read_override[A] = read
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.writes() == []
    assert status(entry).last_error


async def test_read_timeout_writes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, monkeypatch
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    monkeypatch.setattr(manager_module, "SERVICE_TIMEOUT_SECONDS", 0.01)

    async def hang() -> Any:
        await asyncio.sleep(1)

    fake.read_override[B] = hang
    fake.user_add(A, "Eggs")
    await sync(entry)
    assert fake.writes() == []
    assert "timed out" in status(entry).last_error


async def test_partial_list_is_detected_by_the_open_item_count(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk", "Eggs", "Bread")

    async def partial() -> Any:
        return {A: {"items": [dict(i) for i in fake.items[A][:1]]}}

    fake.read_override[A] = partial
    for _ in range(3):
        freezer.tick(CONFIRM)
        await sync(entry)
    assert fake.writes() == []
    assert "reports 3" in status(entry).last_error


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
async def test_unavailable_list_is_not_synced(
    hass: HomeAssistant, fake: FakeTodo, add_pair, state
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    fake.user_add(B, "Eggs")
    if state is None:
        hass.states.async_remove(A)
    else:
        hass.states.async_set(A, state)
    await sync(entry)
    assert fake.writes() == []
    assert "not available" in status(entry).last_error


@pytest.mark.parametrize("count", [1, 2, 3, 4])
async def test_unexpectedly_empty_small_list_wipes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer, count
) -> None:
    names = [f"Item {i}" for i in range(count)]
    entry = await _synced_pair(fake, add_pair, *names)
    # E.g. Alexa's initial item fetch failed after a restart: the entity is
    # available and consistently reports an empty list.
    fake.items[A] = []
    fake.publish(A)
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.writes() == []
    assert fake.open(B) == names
    assert "reads as empty" in status(entry).last_error


async def test_large_unexpected_deletion_is_refused(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    names = [f"Item {i:02}" for i in range(10)]
    entry = await _synced_pair(fake, add_pair, *names)
    for name in names[:6]:
        fake.user_delete(B, name)
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.writes() == []
    assert "6 of 10" in status(entry).last_error


async def test_duplicate_open_names_are_held_back_and_reported(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B, "Milk", "milk")
    entry = await add_pair(A, B)
    await sync(entry)
    fake.user_set(A, "Milk", DONE)
    await sync(entry)
    assert fake.writes() == []
    assert status(entry).conflicts == [
        f"Milk: several open items with this name on {B}"
    ]
    # Once the duplicate is resolved the check-off goes through.
    fake.user_delete(B, "milk")
    await sync(entry)
    assert fake.done(B) == ["Milk"] and status(entry).conflicts == []


# -- failed writes and restarts --------------------------------------------------------


async def test_failed_write_is_retried_without_duplicates(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B, "Bread")
    entry = await add_pair(A, B)
    fake.write_error[("add_item", B)] = HomeAssistantError("Skylight said no")
    await sync(entry)
    assert fake.open(A) == ["Bread", "Milk"]  # the write before the failure
    assert "Skylight said no" in status(entry).last_error
    del fake.write_error[("add_item", B)]
    await sync(entry)
    await sync(entry)
    assert fake.open(A) == ["Bread", "Milk"] and fake.open(B) == ["Bread", "Milk"]
    assert [c[0] for c in fake.writes(A)] == ["add_item"]
    assert status(entry).last_error is None


async def test_failed_check_off_is_not_reversed(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    fake.user_set(B, "Milk", DONE)
    fake.write_error[("update_item", A)] = HomeAssistantError("Alexa offline")
    await sync(entry)
    assert fake.open(A) == ["Milk"] and fake.done(B) == ["Milk"]
    del fake.write_error[("update_item", A)]
    await sync(entry)
    assert fake.done(A) == ["Milk"] and fake.done(B) == ["Milk"]


async def test_restart_with_a_write_not_yet_visible(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    fake.delay_adds.add(B)
    entry = await add_pair(A, B)
    await sync(entry)
    assert len(fake.writes(B)) == 1
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await sync(entry)
    await sync(entry)
    assert len(fake.writes(B)) == 1  # no second add
    assert fake.writes(A) == []  # and Milk not checked off on Alexa
    fake.reveal(B)
    await sync(entry)
    assert fake.open(B) == ["Milk"] and fake.open(A) == ["Milk"]
    assert entry.runtime_data._pending == {}


async def test_state_survives_restart(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk", "Eggs")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    fake.user_delete(A, "Milk")
    await sync(entry)
    freezer.tick(CONFIRM)
    await sync(entry)
    assert fake.done(B) == ["Milk"]


# -- several pairs, force, preview -------------------------------------------------------


async def test_pairs_are_independent(hass: HomeAssistant, fake: FakeTodo, add_pair) -> None:
    for entity_id in (A, B, A2, B2):
        fake.add_list(entity_id)
    one = await add_pair(A, B)
    two = await add_pair(A2, B2)
    fake.user_add(A, "Milk")
    fake.user_add(B2, "Paper towels")
    await hass.services.async_call(DOMAIN, "sync_now", {}, blocking=True)
    assert fake.open(B) == ["Milk"] and fake.open(A2) == ["Paper towels"]
    assert fake.open(B2) == ["Paper towels"] and fake.open(A) == ["Milk"]
    fake.user_add(A, "Eggs")
    fake.user_add(A2, "Batteries")
    await hass.services.async_call(
        DOMAIN, "sync_now", {"config_entry_id": one.entry_id}, blocking=True
    )
    assert fake.open(B) == ["Eggs", "Milk"]
    assert fake.open(B2) == ["Paper towels"]
    assert status(two).last_error is None


async def test_a_list_can_only_be_in_one_pair(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    for entity_id in (A, B, B2):
        fake.add_list(entity_id)
    await add_pair(A, B)
    second = await add_pair(A, B2)
    assert second.state is ConfigEntryState.SETUP_ERROR


async def _blocked_pairs(hass, fake, add_pair, freezer):
    for entity_id in (A, B, A2, B2):
        fake.add_list(entity_id, "Milk", "Eggs")
    one = await add_pair(A, B)
    two = await add_pair(A2, B2)
    await hass.services.async_call(DOMAIN, "sync_now", {}, blocking=True)
    for entity_id in (A, A2):
        fake.items[entity_id] = []
        fake.publish(entity_id)
    await hass.services.async_call(DOMAIN, "sync_now", {}, blocking=True)
    freezer.tick(CONFIRM)
    await hass.services.async_call(DOMAIN, "sync_now", {}, blocking=True)
    assert status(one).last_error and status(two).last_error
    assert fake.writes() == []
    return one, two


async def test_force_targets_only_the_chosen_pair(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer
) -> None:
    one, two = await _blocked_pairs(hass, fake, add_pair, freezer)
    await hass.services.async_call(
        DOMAIN, "sync_now", {"config_entry_id": one.entry_id, "force": True}, blocking=True
    )
    assert fake.done(B) == ["Eggs", "Milk"]
    assert fake.open(B2) == ["Eggs", "Milk"]
    assert fake.writes(B2) == [] and fake.writes(A2) == []
    assert status(one).last_error is None and status(two).last_error


async def test_force_needs_a_pair_and_an_admin(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer, hass_admin_user, hass_read_only_user
) -> None:
    one, _ = await _blocked_pairs(hass, fake, add_pair, freezer)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "sync_now", {"force": True}, blocking=True)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "sync_now",
            {"config_entry_id": one.entry_id, "force": True},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "sync_now", {"config_entry_id": "nope", "force": True}, blocking=True
        )
    assert fake.writes() == []
    await hass.services.async_call(
        DOMAIN,
        "sync_now",
        {"config_entry_id": one.entry_id, "force": True},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    assert fake.done(B) == ["Eggs", "Milk"]


async def test_force_never_skips_read_validation(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    hass.states.async_set(A, "unavailable")
    await hass.services.async_call(
        DOMAIN, "sync_now", {"config_entry_id": entry.entry_id, "force": True}, blocking=True
    )
    assert fake.writes() == []
    assert "not available" in status(entry).last_error


async def test_preview_of_a_new_pair_writes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, hass_storage
) -> None:
    fake.add_list(A, "Milk", "Eggs")
    fake.add_list(B, "Eggs", "Bread", done=("Milk",))
    entry = await add_pair(A, B, enabled=False)
    await sync(entry)  # periodic runs of a pair in preview mode
    response = await hass.services.async_call(
        DOMAIN, "preview", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    preview = response[entry.entry_id]
    assert preview["add_to_a"] == ["Bread"]
    assert preview["reopen_on_b"] == ["Milk"]
    assert preview["add_to_b"] == preview["complete_on_a"] == preview["complete_on_b"] == []
    assert preview["safe"] is True and preview["sync_enabled"] is False
    assert fake.writes() == []
    assert store_key(entry) not in hass_storage
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "sync_now", {"config_entry_id": entry.entry_id, "force": True}, blocking=True
        )


async def test_preview_of_a_running_pair_changes_nothing(
    hass: HomeAssistant, fake: FakeTodo, add_pair, freezer, hass_storage
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk", "Eggs")
    fake.items[A] = []
    fake.publish(A)
    await sync(entry)  # notes that the items went missing
    before = copy.deepcopy(hass_storage[store_key(entry)])
    fake.user_add(B, "Bread")
    freezer.tick(CONFIRM)
    response = await hass.services.async_call(
        DOMAIN, "preview", {}, blocking=True, return_response=True
    )
    preview = response[entry.entry_id]
    assert preview["complete_on_b"] == ["Eggs", "Milk"]
    assert preview["add_to_a"] == ["Bread"]
    assert preview["safe"] is False and "reads as empty" in preview["blocked"][0]
    assert fake.writes() == []
    assert hass_storage[store_key(entry)] == before


async def test_preview_reports_an_unreadable_list(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    entry = await add_pair(A, B, enabled=False)
    hass.states.async_set(B, "unavailable")
    response = await hass.services.async_call(
        DOMAIN, "preview", {}, blocking=True, return_response=True
    )
    assert response[entry.entry_id]["safe"] is False
    assert fake.writes() == []


async def test_sensor_shows_status_and_keeps_items_out_of_the_recorder(
    hass: HomeAssistant, fake: FakeTodo, add_pair
) -> None:
    fake.add_list(A, "Milk")
    fake.add_list(B)
    entry = await add_pair(A, B)
    await sync(entry)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_sync")
    state = hass.states.get(entity_id)
    assert state.attributes["mode"] == "sync"
    assert state.attributes["config_entry_id"] == entry.entry_id
    assert state.attributes["last_changes"] == [f"{B}: + Milk"]
    assert state.attributes["last_error"] is None
    sensor = hass.data["entity_components"]["sensor"].get_entity(entity_id)
    assert {"last_changes", "preview", "conflicts", "waiting"} <= sensor._unrecorded_attributes


async def test_removing_a_pair_deletes_its_sync_state(
    hass: HomeAssistant, fake: FakeTodo, add_pair, hass_storage
) -> None:
    entry = await _synced_pair(fake, add_pair, "Milk")
    assert store_key(entry) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(entry) not in hass_storage
    assert fake.open(A) == ["Milk"] and fake.open(B) == ["Milk"]
