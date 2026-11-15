"""Glue between the pure merge (sync.py) and Home Assistant's todo entities."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ABSENCE_CONFIRM_SECONDS,
    CONF_LIST_A,
    CONF_LIST_B,
    CONF_SYNC_ENABLED,
    DEBOUNCE_SECONDS,
    DOMAIN,
    MASS_CHANGE_MIN,
    MASS_CHANGE_SHARE,
    PENDING_GRACE_SECONDS,
    SCAN_INTERVAL_MINUTES,
    SERVICE_TIMEOUT_SECONDS,
    STORAGE_VERSION,
)
from .sync import (
    DONE,
    OPEN,
    Action,
    InvalidRead,
    Item,
    MergeResult,
    check_safety,
    commit,
    load_state,
    merge,
    parse_items,
    plan,
    settle,
)

_LOGGER = logging.getLogger(__name__)


class SyncSkipped(Exception):
    """A sync was not attempted or stopped (a list is unavailable, a read or
    write failed, or the result looked unsafe)."""


@dataclass
class SyncStatus:
    last_sync: datetime | None = None
    last_attempt: datetime | None = None
    last_error: str | None = None
    last_actions: list[str] = field(default_factory=list)
    items_a: int = 0
    items_b: int = 0
    conflicts: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    preview: dict[str, Any] | None = None


class TodoSyncManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.list_a: str = entry.data[CONF_LIST_A]
        self.list_b: str = entry.data[CONF_LIST_B]
        self.sync_enabled: bool = entry.options.get(CONF_SYNC_ENABLED, True)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}"
        )
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self.status = SyncStatus()
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_sync,
        )

    async def async_setup(self) -> None:
        self._snapshot, self._pending, dropped = load_state(await self._store.async_load())
        if dropped:
            _LOGGER.warning(
                "Ignored %s malformed stored sync entries for %s <-> %s",
                dropped, self.list_a, self.list_b,
            )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.list_a, self.list_b], self._handle_state_change
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._handle_interval,
                timedelta(minutes=SCAN_INTERVAL_MINUTES),
                name=f"{DOMAIN} {self.entry.entry_id}",
            )
        )
        # The source integrations are still loading before Home Assistant
        # has started; their lists may not be complete yet.
        self._unsubs.append(async_at_started(self.hass, self._handle_started))

    async def async_shutdown(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._debouncer.async_shutdown()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self._debouncer.async_call())

    async def _handle_started(self, _hass: HomeAssistant) -> None:
        await self._debouncer.async_call()

    async def _handle_interval(self, _now: datetime) -> None:
        await self._debouncer.async_call()

    async def async_sync(self, force: bool = False) -> None:
        """force skips the mass-change guard for this pair only (service
        sync_now with force: true). It never skips read validation."""
        async with self._lock:
            self.status.last_attempt = dt_util.utcnow()
            try:
                if self.sync_enabled:
                    await self._async_sync_locked(force)
                else:
                    self.status.preview = await self._async_preview_locked()
            except SyncSkipped as err:
                self.status.last_error = str(err)
                _LOGGER.warning("Sync %s <-> %s skipped: %s", self.list_a, self.list_b, err)
            except Exception as err:  # noqa: BLE001 - surfaced on the sensor
                self.status.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("Sync %s <-> %s failed", self.list_a, self.list_b)
            else:
                if self.sync_enabled:
                    self.status.last_error = None
                    self.status.last_sync = self.status.last_attempt
                else:
                    blocked = self.status.preview["blocked"]
                    self.status.last_error = "; ".join(blocked) if blocked else None
        self._notify()

    async def async_preview(self) -> dict[str, Any]:
        """What a sync would do now. Never writes and never stores state."""
        async with self._lock:
            self.status.preview = await self._async_preview_locked()
        self._notify()
        return self.status.preview

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def _async_preview_locked(self) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "list_a": self.list_a,
            "list_b": self.list_b,
            "sync_enabled": self.sync_enabled,
            "generated": dt_util.utcnow().isoformat(),
        }
        try:
            a_items, b_items = await self._async_read()
        except SyncSkipped as err:
            return {**preview, "safe": False, "blocked": [str(err)]}
        result = self._merge(a_items, b_items)
        blocked = self._check_safety(result, a_items, b_items)
        return {
            **preview,
            **plan(result),
            "conflicts": self._name_lists(list(result.held.values())),
            "waiting": result.waiting,
            "blocked": blocked,
            "safe": not blocked,
        }

    async def _async_sync_locked(self, force: bool) -> None:
        a_items, b_items = await self._async_read()
        result = self._merge(a_items, b_items)
        self.status.conflicts = self._name_lists(list(result.held.values()))
        self.status.waiting = result.waiting
        if result.held:
            _LOGGER.warning(
                "Sync %s <-> %s: %s item(s) held back, see the sensor's conflicts",
                self.list_a, self.list_b, len(result.held),
            )
        if not force and (reasons := self._check_safety(result, a_items, b_items)):
            raise SyncSkipped(
                "; ".join(reasons) + ". Nothing was changed; if this is intended, run "
                "todo_sync.sync_now with force: true for this pair."
            )

        done_keys: set[str] = set()
        done: list[str] = []
        try:
            for action in result.actions:
                await self._async_apply(action)
                done_keys.add(action.key)
                done.append(_describe(action, self.list_a, self.list_b))
                _LOGGER.debug("todo_sync: %s", done[-1])
        finally:
            # Also after a failed write: what went through is remembered,
            # the rest is decided again on the next run.
            self._snapshot, self._pending = commit(
                result, self._snapshot, self._pending, done_keys
            )
            for side in sorted({x.side for x in result.actions if x.key in done_keys}):
                try:
                    items = await self._async_get_items(self.list_a if side == "a" else self.list_b)
                except SyncSkipped:
                    continue
                self._pending = settle(self._pending, side, items)
            await self._store.async_save({"snapshot": self._snapshot, "pending": self._pending})
            if done:
                self.status.last_actions = done

    def _merge(self, a_items: list[Item], b_items: list[Item]) -> MergeResult:
        return merge(
            a_items,
            b_items,
            self._snapshot,
            self._pending,
            dt_util.utcnow().timestamp(),
            PENDING_GRACE_SECONDS,
            ABSENCE_CONFIRM_SECONDS,
        )

    def _check_safety(
        self, result: MergeResult, a_items: list[Item], b_items: list[Item]
    ) -> list[str]:
        return self._name_lists(
            check_safety(
                result, self._snapshot, a_items, b_items, MASS_CHANGE_MIN, MASS_CHANGE_SHARE
            )
        )

    def _name_lists(self, messages: list[str]) -> list[str]:
        return [m.replace("list A", self.list_a).replace("list B", self.list_b) for m in messages]

    async def _async_read(self) -> tuple[list[Item], list[Item]]:
        a_items = await self._async_get_items(self.list_a)
        b_items = await self._async_get_items(self.list_b)
        self.status.items_a = sum(1 for i in a_items if i.status == OPEN)
        self.status.items_b = sum(1 for i in b_items if i.status == OPEN)
        return a_items, b_items

    def _available_state(self, entity_id: str) -> State:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise SyncSkipped(f"{entity_id} is not available")
        return state

    async def _async_get_items(self, entity_id: str) -> list[Item]:
        self._available_state(entity_id)
        response = await self._async_call(
            "get_items", entity_id, {"status": [OPEN, DONE]}, return_response=True
        )
        try:
            items = parse_items(response, entity_id)
        except InvalidRead as err:
            raise SyncSkipped(f"Unusable read: {err}") from None
        # A to-do entity's state is its number of open items. Both come
        # from the same data, so a mismatch means the read is not the list
        # the entity currently shows.
        state = self._available_state(entity_id)
        open_count = sum(1 for i in items if i.status == OPEN)
        if state.state.isdigit() and int(state.state) != open_count:
            raise SyncSkipped(
                f"{entity_id} returned {open_count} open items but reports "
                f"{state.state}; waiting for a consistent read"
            )
        return items

    async def _async_apply(self, action: Action) -> None:
        entity_id = self.list_a if action.side == "a" else self.list_b
        self._available_state(entity_id)
        if action.op == "add":
            await self._async_call("add_item", entity_id, {"item": action.summary})
        elif not action.uid:
            # Never fall back to the name: it may match another item.
            raise SyncSkipped(f"{entity_id}: item without an id, not changed")
        elif action.op == "set_status":
            await self._async_call(
                "update_item", entity_id, {"item": action.uid, "status": action.status}
            )
        elif action.op == "remove":
            await self._async_call("remove_item", entity_id, {"item": [action.uid]})

    async def _async_call(
        self,
        service: str,
        entity_id: str,
        data: dict[str, Any],
        return_response: bool = False,
    ) -> Any:
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT_SECONDS):
                return await self.hass.services.async_call(
                    "todo",
                    service,
                    data,
                    target={"entity_id": entity_id},
                    blocking=True,
                    return_response=return_response,
                )
        except TimeoutError:
            raise SyncSkipped(f"todo.{service} on {entity_id} timed out") from None
        except HomeAssistantError as err:
            raise SyncSkipped(f"todo.{service} on {entity_id} failed: {err}") from None


def _describe(action: Action, list_a: str, list_b: str) -> str:
    where = list_a if action.side == "a" else list_b
    if action.op == "add":
        return f"{where}: + {action.summary}"
    if action.op == "remove":
        return f"{where}: - {action.summary}"
    mark = "✓" if action.status == DONE else "○"
    return f"{where}: {mark} {action.summary}"
