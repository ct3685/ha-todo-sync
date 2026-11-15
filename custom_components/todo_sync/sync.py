"""Pure three-way merge of two to-do lists. No Home Assistant imports, so it
can be tested on its own.

Side A may be a list that cannot hold completed items (the Alexa shopping
list: checking an item off there removes it). For such a side, "absent"
and "completed" mean the same thing, so A is modelled as a set of open
items only. Side B (e.g. Google Keep) keeps completed items.

The merge compares both lists with the state after the last successful
sync (the snapshot). That is what tells "added on one side" apart from
"removed on the other side", which a plain two-way diff cannot do.

Writes to a side are not always visible immediately on the next read (a
remote service polled every few minutes). Every write is therefore
remembered as *pending* with the value it should produce; until the side
shows that value or the grace period ends, the expected value is used
instead of the stale one, so a sync never undoes its own writes.

Reads can also be incomplete for a moment (the Alexa shopping list add-on
once returned 13 of 17 items). An item that disappears from a side is
therefore only treated as checked off or deleted once it has been missing
for `confirm` seconds; until then it counts as still there.

Several items with the same name on one side are one item for the merge
(an open one wins). Checking such an item off would have to pick one of
several open items, so that write is held back and reported instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

OPEN = "needs_action"
DONE = "completed"

_SPACES = re.compile(r"\s+")


class InvalidRead(Exception):
    """A get_items response that cannot be trusted to be the whole list."""


def item_key(summary: str) -> str:
    """Items are matched by name, case- and whitespace-insensitively."""
    return _SPACES.sub(" ", summary).strip().casefold()


@dataclass(frozen=True)
class Item:
    uid: str | None
    summary: str
    status: str


@dataclass(frozen=True)
class Action:
    side: str  # "a" | "b"
    op: str  # "add" | "set_status" | "remove"
    key: str
    summary: str
    uid: str | None = None
    status: str | None = None
    # Caused by the item missing on the other side (not by an explicit
    # check-off there): what an outage or an incomplete read looks like.
    via_absence: bool = False


@dataclass
class MergeResult:
    actions: list[Action] = field(default_factory=list)
    snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Keys whose change is held back: their old snapshot entry is kept so
    # the next run decides them again.
    held: dict[str, str] = field(default_factory=dict)
    # Summaries missing on one side but not yet confirmed gone.
    waiting: list[str] = field(default_factory=list)


def parse_items(response: Any, entity_id: str) -> list[Item]:
    """Items of a todo.get_items response, or InvalidRead.

    Anything other than the documented shape is an error, never an empty
    list: a missing item looks exactly like a deleted one to the merge.
    """
    if not isinstance(response, dict):
        raise InvalidRead(f"{entity_id}: no response")
    entry = response.get(entity_id)
    if not isinstance(entry, dict):
        raise InvalidRead(f"{entity_id}: missing from the response")
    raw = entry.get("items")
    if not isinstance(raw, list):
        raise InvalidRead(f"{entity_id}: response has no item list")
    items: list[Item] = []
    uids: set[str] = set()
    for obj in raw:
        if not isinstance(obj, dict):
            raise InvalidRead(f"{entity_id}: malformed item")
        uid, summary, status = obj.get("uid"), obj.get("summary"), obj.get("status")
        if not isinstance(uid, str) or not uid:
            raise InvalidRead(f"{entity_id}: item without an id")
        if uid in uids:
            raise InvalidRead(f"{entity_id}: item id listed twice")
        if not isinstance(summary, str):
            raise InvalidRead(f"{entity_id}: item without a name")
        if status not in (OPEN, DONE):
            raise InvalidRead(f"{entity_id}: item with unknown status")
        uids.add(uid)
        # A blank name can never match a synced item, so skipping it cannot
        # look like a deletion.
        if item_key(summary):
            items.append(Item(uid, summary, status))
    return items


def load_state(stored: Any) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    """Snapshot and pending from storage, dropping malformed entries.

    Returns the number of dropped entries. A dropped snapshot entry makes
    its item look new, which only ever copies it, never removes it.
    """
    stored = stored if isinstance(stored, dict) else {}
    raw_snapshot = stored.get("snapshot")
    raw_pending = stored.get("pending")
    raw_snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
    raw_pending = raw_pending if isinstance(raw_pending, dict) else {}
    snapshot = {
        k: v
        for k, v in raw_snapshot.items()
        if isinstance(v, dict)
        and isinstance(v.get("summary"), str)
        and isinstance(v.get("a"), bool)
        and v.get("b") in (OPEN, DONE, None)
    }

    def valid_pending(p: Any, v: Any) -> bool:
        if not isinstance(p, str) or not isinstance(v, dict):
            return False
        if p.startswith("gone:"):
            return isinstance(v.get("since"), (int, float))
        return p[:2] in ("a:", "b:") and isinstance(v.get("ts"), (int, float)) and (
            v.get("expect") in ((True, False) if p[0] == "a" else (OPEN, DONE, None))
        )

    pending = {p: v for p, v in raw_pending.items() if valid_pending(p, v)}
    dropped = len(raw_snapshot) - len(snapshot) + len(raw_pending) - len(pending)
    return snapshot, pending, dropped


def index(items: list[Item]) -> dict[str, Item]:
    """One item per key: the first open one, else the first one."""
    out: dict[str, Item] = {}
    for item in items:
        key = item_key(item.summary)
        if key and (key not in out or (item.status == OPEN and out[key].status != OPEN)):
            out[key] = item
    return out


def ambiguous(items: list[Item]) -> set[str]:
    """Keys with more than one open item: unclear which one to check off."""
    seen: set[str] = set()
    out: set[str] = set()
    for item in items:
        if item.status == OPEN:
            key = item_key(item.summary)
            (out if key in seen else seen).add(key)
    return out


def merge(
    a_items: list[Item],
    b_items: list[Item],
    snapshot: dict[str, dict[str, Any]],
    pending: dict[str, dict[str, Any]],
    now: float,
    grace: float,
    confirm: float,
) -> MergeResult:
    """Compute the actions that bring A and B in line with each other.

    snapshot: key -> {"summary", "a": bool (open on A), "b": OPEN|DONE|None}
    pending: "<side>:<key>" -> {"expect": value, "ts": float}, plus
             "gone:<side>:<key>" -> {"since": float} for items that went
             missing and are not yet confirmed gone
    """
    a_all = index(a_items)
    b_idx = index(b_items)
    a_open = {k: v for k, v in a_all.items() if v.status == OPEN}
    dupes = {"a": ambiguous(a_items), "b": ambiguous(b_items)}

    result = MergeResult()
    still_pending: dict[str, dict[str, Any]] = {}

    def effective(side: str, key: str, actual: Any) -> Any:
        """Actual value, unless a recent write of ours has not arrived yet."""
        entry = pending.get(f"{side}:{key}")
        if entry is None or entry["expect"] == actual or now - entry["ts"] > grace:
            return actual
        still_pending[f"{side}:{key}"] = entry
        return entry["expect"]

    def confirmed_gone(side: str, key: str) -> bool:
        """True once the item has been missing on `side` for `confirm` seconds."""
        marker = f"gone:{side}:{key}"
        since = pending.get(marker, {}).get("since", now)
        if now - since >= confirm:
            return True
        still_pending[marker] = {"since": since}
        return False

    def keep_old(key: str, reason: str | None) -> None:
        """Leave `key` as it was; the next run decides it again."""
        if reason:
            result.held[key] = reason
        if key in snapshot:
            result.snapshot[key] = snapshot[key]
        for p in (f"a:{key}", f"b:{key}"):
            if p in pending:
                still_pending[p] = pending[p]

    for key in sorted(set(a_all) | set(b_idx) | set(snapshot)):
        a_item = a_all.get(key)
        b_item = b_idx.get(key)
        a = effective("a", key, key in a_open)
        b = effective("b", key, b_item.status if b_item else None)
        s = snapshot.get(key)
        # Assumed values stand in for a read that may be incomplete. They
        # must never trigger a write to that side: there is no item to
        # update, and adding one would create a duplicate once the read is
        # complete again.
        a_assumed = b_assumed = False
        if s is not None:
            # Only an item that is really missing; one that is still there
            # but completed was checked off explicitly and syncs at once.
            if s["a"] and a_item is None and not a and not confirmed_gone("a", key):
                a, a_assumed = True, True
            if s["b"] is not None and b is None and not confirmed_gone("b", key):
                b, b_assumed = s["b"], True
        summary = (
            (a_item.summary if a_item else None)
            or (b_item.summary if b_item else None)
            or (s or {}).get("summary", key)
        )
        if a_assumed or b_assumed:
            result.waiting.append(summary)

        a_t, b_t = _decide(a, b, s)
        # Defer changes to a side whose read is in doubt; the next run
        # decides again with a complete read or a confirmed absence.
        if a_assumed and a_t != a:
            a_t = a
        if b_assumed and b_t != b:
            b_t = b

        actions: list[Action] = []
        if a_t != a:
            if a_t:
                if a_item is not None:  # present but completed (transient)
                    actions.append(Action("a", "set_status", key, summary, a_item.uid, OPEN))
                else:
                    actions.append(Action("a", "add", key, summary))
            elif a_item is not None:
                actions.append(
                    Action("a", "set_status", key, summary, a_item.uid, DONE, via_absence=b_item is None)
                )

        if b_t != b:
            if b_t is None:
                if b_item is not None:
                    actions.append(
                        Action("b", "remove", key, summary, b_item.uid, via_absence=a_item is None)
                    )
            elif b_item is not None:
                actions.append(
                    Action(
                        "b", "set_status", key, summary, b_item.uid, b_t,
                        via_absence=b_t == DONE and a_item is None,
                    )
                )
            elif b_t == OPEN:
                actions.append(Action("b", "add", key, summary, status=OPEN))
            else:
                # To be checked off on B, but our add has not shown up there
                # yet: adding it again would duplicate it.
                keep_old(key, None)
                continue

        held_on = [x.side for x in actions if _is_loss(x) and key in dupes[x.side]]
        if held_on:
            # Several open items share this name: checking off one of them
            # could hit the wrong one.
            keep_old(key, f"{summary}: several open items with this name on list {held_on[0].upper()}")
            continue

        result.actions.extend(actions)
        if a_t != a:
            still_pending[f"a:{key}"] = {"expect": a_t, "ts": now}
        if b_t != b:
            still_pending[f"b:{key}"] = {"expect": b_t, "ts": now}

        if a_t or b_t is not None:
            result.snapshot[key] = {"summary": summary, "a": a_t, "b": b_t}

    result.pending = still_pending
    return result


def _is_loss(action: Action) -> bool:
    return action.op == "remove" or (action.op == "set_status" and action.status == DONE)


def check_safety(
    result: MergeResult,
    snapshot: dict[str, dict[str, Any]],
    a_items: list[Item],
    b_items: list[Item],
    min_count: int,
    share: float,
) -> list[str]:
    """Reasons not to apply `result`; empty when it looks like normal use.

    Only items checked off because they went *missing* on the other side
    count: an item that is still listed and marked completed was checked
    off explicitly. A side that reads as completely empty although items
    were synced before never removes anything from the other side, however
    small the list is.
    """
    reasons: list[str] = []
    for side, items, other in (("a", a_items, "b"), ("b", b_items, "a")):
        known = sum(
            1 for s in snapshot.values() if (s["a"] if side == "a" else s["b"] is not None)
        )
        lost = sum(1 for x in result.actions if x.side == other and x.via_absence and _is_loss(x))
        if not known or not lost:
            continue
        name = f"list {side.upper()}"
        if not items:
            reasons.append(
                f"{name} reads as empty although {known} synced item(s) were on it; "
                f"{lost} item(s) would be checked off on list {other.upper()}"
            )
        elif lost >= min_count and lost > known * share:
            reasons.append(
                f"{lost} of {known} synced item(s) disappeared from {name} at once; "
                f"they would be checked off on list {other.upper()}"
            )
    return reasons


def plan(result: MergeResult) -> dict[str, list[str]]:
    """The actions of `result` grouped for people to read (preview)."""
    out: dict[str, list[str]] = {
        f"{verb}_{side}": []
        for side in ("a", "b")
        for verb in ("add_to", "complete_on", "reopen_on", "remove_from")
    }
    for x in result.actions:
        if x.op == "add":
            verb = "add_to"
        elif x.op == "remove":
            verb = "remove_from"
        else:
            verb = "complete_on" if x.status == DONE else "reopen_on"
        out[f"{verb}_{x.side}"].append(x.summary)
    return out


def commit(
    result: MergeResult,
    snapshot: dict[str, dict[str, Any]],
    pending: dict[str, dict[str, Any]],
    done: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Snapshot and pending to store when only the actions for `done` keys
    went through.

    A key whose write failed or was not attempted keeps its previous
    snapshot entry and pending writes, so the next run decides it again
    instead of taking an unwritten change as done. Absence markers are
    observations, not writes, and are always kept.
    """
    open_keys = {x.key for x in result.actions} - done
    new_snapshot = {k: v for k, v in result.snapshot.items() if k not in open_keys}
    new_pending = {
        p: v
        for p, v in result.pending.items()
        if p.startswith("gone:") or p[2:] not in open_keys
    }
    for key in open_keys:
        if key in snapshot:
            new_snapshot[key] = snapshot[key]
        for p in (f"a:{key}", f"b:{key}"):
            if p in pending:
                new_pending[p] = pending[p]
    return new_snapshot, new_pending


def settle(
    pending: dict[str, dict[str, Any]], side: str, items: list[Item]
) -> dict[str, dict[str, Any]]:
    """Drop the pending writes on `side` that `items`, a read right after
    the writes, already shows. Only writes that are really delayed keep
    standing in for the side's value; the others would hide the next
    change made there."""
    idx = index(items)

    def actual(key: str) -> Any:
        item = idx.get(key)
        if side == "a":
            return item is not None and item.status == OPEN
        return item.status if item else None

    return {
        p: v
        for p, v in pending.items()
        if not (p.startswith(f"{side}:") and v["expect"] == actual(p[2:]))
    }


def _decide(a: bool, b: str | None, s: dict[str, Any] | None) -> tuple[bool, str | None]:
    """Target (open on A, status on B) for one item."""
    if s is None:
        # Unknown so far: whatever exists anywhere is new and is copied over.
        if a:
            return True, OPEN  # also reopens an old completed copy on B
        if b == OPEN:
            return True, OPEN
        return False, b  # completed on B only, or nothing at all

    a_changed = a != s["a"]
    b_changed = b != s["b"]

    if not a_changed and not b_changed:
        return a, b

    if a_changed and not b_changed:
        if a:  # (re)added on A
            return True, OPEN
        # checked off or removed on A: check it off on B, keep it there
        return False, (DONE if b is not None else None)

    if b_changed and not a_changed:
        if b == OPEN:  # added or unchecked on B
            return True, OPEN
        return False, b  # checked off (DONE) or deleted (None) on B

    # Both changed since the last sync: keep the item if either side wants it.
    if a or b == OPEN:
        return True, OPEN
    return False, b
