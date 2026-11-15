"""Tests for the pure merge in custom_components/todo_sync/sync.py.

Loaded by file path so the tests need no Home Assistant installation (the
package __init__ imports homeassistant).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_PATH = Path(__file__).parents[1] / "custom_components" / "todo_sync" / "sync.py"
_spec = importlib.util.spec_from_file_location("todo_sync_merge", _PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules["todo_sync_merge"] = sync
_spec.loader.exec_module(sync)

OPEN, DONE, Item = sync.OPEN, sync.DONE, sync.Item
GRACE = 1200


def a(*names, done=()):
    return [Item(f"a-{n}", n, OPEN) for n in names] + [Item(f"a-{n}", n, DONE) for n in done]


def b(*names, done=()):
    return [Item(f"b-{n}", n, OPEN) for n in names] + [Item(f"b-{n}", n, DONE) for n in done]


def ops(result):
    return sorted((x.side, x.op, x.summary, x.status) for x in result.actions)


def run(a_items, b_items, snapshot=None, pending=None, now=1000.0):
    # confirm=0: these tests cover the merge rules, not incomplete reads.
    return sync.merge(a_items, b_items, snapshot or {}, pending or {}, now, GRACE, 0)


def settled(result, now=1000.0):
    """Snapshot after the actions were applied and are visible on both sides."""
    a_items = [Item(f"a-{k}", v["summary"], OPEN) for k, v in result.snapshot.items() if v["a"]]
    b_items = [
        Item(f"b-{k}", v["summary"], v["b"]) for k, v in result.snapshot.items() if v["b"]
    ]
    return a_items, b_items


# -- first sync ------------------------------------------------------------------


def test_first_sync_copies_alexa_items_into_empty_keep():
    r = run(a("Milch", "Brot"), [])
    assert ops(r) == [("b", "add", "Brot", OPEN), ("b", "add", "Milch", OPEN)]


def test_first_sync_merges_both_lists():
    r = run(a("Milch"), b("Eier", done=("Mehl",)))
    assert ops(r) == [("a", "add", "Eier", None), ("b", "add", "Milch", OPEN)]
    assert r.snapshot["mehl"] == {"summary": "Mehl", "a": False, "b": DONE}


def test_names_match_case_and_whitespace_insensitively():
    r = run(a("gurken"), b("Gurken "))
    assert r.actions == []


# -- steady state and no ping-pong -----------------------------------------------


def test_second_run_after_applying_is_a_no_op():
    r1 = run(a("Milch"), b("Eier"))
    a_items, b_items = settled(r1)
    r2 = run(a_items, b_items, r1.snapshot, r1.pending, now=1010)
    assert r2.actions == []


# -- changes on A (Alexa) --------------------------------------------------------


def test_item_added_on_alexa_is_added_to_keep():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run(a("Milch", "Käse"), b("Milch"), snap)
    assert ops(r) == [("b", "add", "Käse", OPEN)]


def test_item_gone_from_alexa_is_checked_off_in_keep():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run([], b("Milch"), snap)
    assert ops(r) == [("b", "set_status", "Milch", DONE)]
    assert r.snapshot["milch"] == {"summary": "Milch", "a": False, "b": DONE}


def test_item_readded_on_alexa_reopens_it_in_keep():
    snap = {"milch": {"summary": "Milch", "a": False, "b": DONE}}
    r = run(a("Milch"), b(done=("Milch",)), snap)
    assert ops(r) == [("b", "set_status", "Milch", OPEN)]


# -- changes on B (Keep) ---------------------------------------------------------


def test_item_checked_off_in_keep_is_completed_on_alexa():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run(a("Milch"), b(done=("Milch",)), snap)
    assert ops(r) == [("a", "set_status", "Milch", DONE)]


def test_item_unchecked_in_keep_is_added_to_alexa_again():
    snap = {"milch": {"summary": "Milch", "a": False, "b": DONE}}
    r = run([], b("Milch"), snap)
    assert ops(r) == [("a", "add", "Milch", None)]


def test_item_deleted_in_keep_is_removed_from_alexa():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run(a("Milch"), [], snap)
    assert ops(r) == [("a", "set_status", "Milch", DONE)]
    assert "milch" not in r.snapshot


def test_completed_item_deleted_in_keep_is_forgotten():
    snap = {"milch": {"summary": "Milch", "a": False, "b": DONE}}
    r = run([], [], snap)
    assert r.actions == [] and r.snapshot == {}


# -- conflicts -------------------------------------------------------------------


def test_both_sides_changed_keeps_the_item():
    # Checked off on Alexa (gone) while re-added... both changed: deleted in
    # Keep and re-added on Alexa -> keep it.
    snap = {"milch": {"summary": "Milch", "a": False, "b": DONE}}
    r = run(a("Milch"), [], snap)
    assert ops(r) == [("b", "add", "Milch", OPEN)]


def test_duplicate_names_in_one_list_are_ignored():
    r = run(a("Milch", "milch"), [])
    assert ops(r) == [("b", "add", "Milch", OPEN)]


# -- pending writes ----------------------------------------------------------------


def test_add_not_yet_visible_on_keep_is_not_treated_as_deletion():
    r1 = run(a("Milch"), [])
    # Keep has not picked up the add yet on the next run.
    r2 = run(a("Milch"), [], r1.snapshot, r1.pending, now=1100)
    assert r2.actions == []
    assert "b:milch" in r2.pending


def test_pending_expires_after_grace_period():
    r1 = run(a("Milch"), [])
    r2 = run(a("Milch"), [], r1.snapshot, r1.pending, now=1000 + GRACE + 1)
    # Never arrived on Keep: treated as deleted there -> removed from Alexa.
    assert ops(r2) == [("a", "set_status", "Milch", DONE)]


def test_pending_is_cleared_once_visible():
    r1 = run(a("Milch"), [])
    r2 = run(a("Milch"), b("Milch"), r1.snapshot, r1.pending, now=1100)
    assert r2.actions == [] and r2.pending == {}


def test_check_off_not_yet_visible_on_alexa_is_not_undone():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r1 = run(a("Milch"), b(done=("Milch",)), snap)
    # Alexa still shows the item on the next run.
    r2 = run(a("Milch"), b(done=("Milch",)), r1.snapshot, r1.pending, now=1050)
    assert r2.actions == []


# -- incomplete reads ------------------------------------------------------------

CONFIRM = 360


def run_c(a_items, b_items, snapshot, pending=None, now=1000.0):
    return sync.merge(a_items, b_items, snapshot, pending or {}, now, GRACE, CONFIRM)


def test_item_briefly_missing_on_alexa_is_not_checked_off():
    # The real incident: one read of the Alexa list lacked 4 of 17 items.
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r1 = run_c([], b("Milch"), snap)
    assert r1.actions == []
    assert r1.snapshot["milch"]["a"] is True
    # Back on the next read: nothing happened, the marker is gone.
    r2 = run_c(a("Milch"), b("Milch"), r1.snapshot, r1.pending, now=1100)
    assert r2.actions == [] and r2.pending == {}


def test_item_missing_for_longer_than_confirm_is_checked_off():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r1 = run_c([], b("Milch"), snap)
    r2 = run_c([], b("Milch"), r1.snapshot, r1.pending, now=1000 + CONFIRM)
    assert ops(r2) == [("b", "set_status", "Milch", DONE)]


def test_item_briefly_missing_on_keep_is_not_removed_from_alexa():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r1 = run_c(a("Milch"), [], snap)
    assert r1.actions == []
    r2 = run_c(a("Milch"), [], r1.snapshot, r1.pending, now=1000 + CONFIRM)
    assert ops(r2) == [("a", "set_status", "Milch", DONE)]


def test_explicit_check_off_in_keep_is_not_delayed():
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run_c(a("Milch"), b(done=("Milch",)), snap)
    assert ops(r) == [("a", "set_status", "Milch", DONE)]


def test_missing_on_keep_while_alexa_confirmed_gone_adds_no_duplicate():
    # Gone from Alexa for long enough, but this Keep read lacks the item:
    # Keep must not get a second "Milch".
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    pending = {"gone:a:milch": {"since": 1000 - CONFIRM}}
    r = run_c([], [], snap, pending)
    assert all(not (x.side == "b" and x.op == "add") for x in r.actions)
    assert r.snapshot["milch"]["b"] == OPEN


def test_readded_on_alexa_while_missing_on_keep_adds_no_duplicate():
    snap = {"milch": {"summary": "Milch", "a": False, "b": DONE}}
    r = run_c(a("Milch"), [], snap)
    assert all(not (x.side == "b" and x.op == "add") for x in r.actions)


def test_completed_on_alexa_is_checked_off_in_keep_at_once():
    # Still listed on Alexa, but completed: an explicit check-off, no delay.
    snap = {"milch": {"summary": "Milch", "a": True, "b": OPEN}}
    r = run_c([Item("a-Milch", "Milch", DONE)], b("Milch"), snap)
    assert ops(r) == [("b", "set_status", "Milch", DONE)]


def test_check_off_of_an_add_not_yet_visible_adds_no_duplicate():
    # Our add to Keep has not shown up yet when Milch is checked off on Alexa.
    r1 = run(a("Milch"), [])
    r2 = run([], [], r1.snapshot, r1.pending, now=1100)
    assert r2.actions == []
    # Once Keep shows the add, the check-off follows.
    r3 = run([], b("Milch"), r2.snapshot, r2.pending, now=1200)
    assert ops(r3) == [("b", "set_status", "Milch", DONE)]


# -- reading get_items responses -------------------------------------------------

import pytest  # noqa: E402

E = "todo.x"


def resp(*items):
    return {E: {"items": list(items)}}


def raw(uid, summary, status=OPEN):
    return {"uid": uid, "summary": summary, "status": status}


def test_parse_items_accepts_a_real_empty_list():
    assert sync.parse_items(resp(), E) == []


def test_parse_items_reads_items():
    got = sync.parse_items(resp(raw("1", "Milk"), raw("2", "Eggs", DONE)), E)
    assert got == [Item("1", "Milk", OPEN), Item("2", "Eggs", DONE)]


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "x",
        {},
        {"todo.other": {"items": []}},
        {E: None},
        {E: {}},
        {E: {"items": None}},
        {E: {"items": "Milk"}},
        resp("Milk"),
        resp({"summary": "Milk", "status": OPEN}),
        resp(raw("", "Milk")),
        resp(raw(1, "Milk")),
        resp({"uid": "1", "status": OPEN}),
        resp(raw("1", None)),
        resp(raw("1", "Milk", "done")),
        resp(raw("1", "Milk", None)),
        resp(raw("1", "Milk"), raw("1", "Eggs")),
    ],
)
def test_parse_items_rejects_anything_that_is_not_a_full_list(response):
    with pytest.raises(sync.InvalidRead):
        sync.parse_items(response, E)


def test_parse_items_skips_blank_names():
    assert sync.parse_items(resp(raw("1", "  "), raw("2", "Milk")), E) == [Item("2", "Milk", OPEN)]


# -- safety check ----------------------------------------------------------------


def synced(*names):
    return {n.casefold(): {"summary": n, "a": True, "b": OPEN} for n in names}


def safety(r, snap, a_items, b_items):
    return sync.check_safety(r, snap, a_items, b_items, 2, 0.5)


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_alexa_reading_empty_never_checks_off_small_lists(count):
    names = [f"Item{i}" for i in range(count)]
    snap = synced(*names)
    r = run([], b(*names), snap)
    assert len(r.actions) == count
    assert safety(r, snap, [], b(*names))


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_keep_reading_empty_never_checks_off_small_lists(count):
    names = [f"Item{i}" for i in range(count)]
    snap = synced(*names)
    r = run(a(*names), [], snap)
    assert len(r.actions) == count
    assert safety(r, snap, a(*names), [])


def test_completed_items_left_make_a_list_not_empty():
    # Alexa keeps checked-off items: one removed item of a list is normal use.
    snap = synced("Milk", "Eggs")
    a_items = a("Eggs")
    r = run(a_items, b("Milk", "Eggs"), snap)
    assert ops(r) == [("b", "set_status", "Milk", DONE)]
    assert safety(r, snap, a_items, b("Milk", "Eggs")) == []


def test_one_item_deleted_from_a_small_list_is_allowed():
    snap = synced("Milk", "Eggs", "Bread")
    r = run(a("Eggs", "Bread"), b("Milk", "Eggs", "Bread"), snap)
    assert safety(r, snap, a("Eggs", "Bread"), b("Milk", "Eggs", "Bread")) == []


def test_most_of_a_small_list_vanishing_at_once_is_refused():
    snap = synced("Milk", "Eggs", "Bread")
    r = run(a("Bread"), b("Milk", "Eggs", "Bread"), snap)
    assert safety(r, snap, a("Bread"), b("Milk", "Eggs", "Bread"))


def test_large_unexpected_deletion_is_refused():
    names = [f"Item{i}" for i in range(10)]
    snap = synced(*names)
    r = run(a(*names[:4]), b(*names), snap)
    assert len(r.actions) == 6
    assert safety(r, snap, a(*names[:4]), b(*names))


def test_a_few_deletions_in_a_large_list_are_allowed():
    names = [f"Item{i}" for i in range(10)]
    snap = synced(*names)
    r = run(a(*names[3:]), b(*names), snap)
    assert safety(r, snap, a(*names[3:]), b(*names)) == []


def test_explicit_check_offs_are_never_refused():
    names = ["Milk", "Eggs", "Bread"]
    snap = synced(*names)
    b_items = b(done=names)
    r = run(a(*names), b_items, snap)
    assert len(r.actions) == 3
    assert safety(r, snap, a(*names), b_items) == []


def test_first_sync_is_never_refused_and_never_removes():
    a_items = a("Milk", "Eggs", done=("Flour",))
    b_items = b("Eggs", "Bread", done=("Milk", "Sugar"))
    r = run(a_items, b_items, {})
    assert all(x.op in ("add", "set_status") for x in r.actions)
    assert all(x.status != DONE for x in r.actions)
    assert safety(r, {}, a_items, b_items) == []
    assert ops(r) == [("a", "add", "Bread", None), ("b", "set_status", "Milk", OPEN)]


def test_both_lists_empty_is_a_no_op():
    r = run([], [], {})
    assert r.actions == [] and r.snapshot == {} and safety(r, {}, [], []) == []


# -- duplicate names ---------------------------------------------------------------


def test_checking_off_one_of_several_open_items_is_held_back():
    snap = synced("Milk")
    b_items = [Item("b1", "Milk", OPEN), Item("b2", "milk", OPEN)]
    r = run([], b_items, snap)
    assert r.actions == []
    assert "milk" in r.held
    assert r.snapshot["milk"] == snap["milk"]  # decided again next run


def test_completed_duplicate_does_not_block_checking_off_the_open_one():
    # Alexa keeps last week's checked-off Milk next to this week's.
    snap = synced("Milk")
    a_items = [Item("old", "Milk", DONE), Item("new", "Milk", OPEN)]
    r = run(a_items, b(done=("Milk",)), snap)
    assert [(x.side, x.uid, x.status) for x in r.actions] == [("a", "new", DONE)]


def test_readding_reopens_one_of_several_completed_copies():
    snap = {"milk": {"summary": "Milk", "a": False, "b": DONE}}
    b_items = [Item("b1", "Milk", DONE), Item("b2", "Milk", DONE)]
    r = run(a("Milk"), b_items, snap)
    assert [(x.side, x.op, x.uid, x.status) for x in r.actions] == [("b", "set_status", "b1", OPEN)]


def test_open_duplicate_counts_as_open():
    b_items = [Item("b1", "Milk", DONE), Item("b2", "Milk", OPEN)]
    r = run([], b_items, {})
    assert ops(r) == [("a", "add", "Milk", None)]


# -- renames -------------------------------------------------------------------------


def test_rename_on_alexa_adds_new_name_then_checks_off_old_one():
    snap = synced("Milk")
    r1 = run_c(a("Oat milk"), b("Milk"), snap)
    assert ops(r1) == [("b", "add", "Oat milk", OPEN)]
    assert r1.waiting == ["Milk"]
    a_items, b_items = a("Oat milk"), b("Milk", "Oat milk")
    r2 = run_c(a_items, b_items, r1.snapshot, r1.pending, now=1000 + CONFIRM)
    assert ops(r2) == [("b", "set_status", "Milk", DONE)]
    assert sync.check_safety(r2, r1.snapshot, a_items, b_items, 2, 0.5) == []


def test_rename_on_keep_adds_new_name_then_checks_off_old_one():
    snap = synced("Milk")
    r1 = run_c(a("Milk"), b("Oat milk"), snap)
    assert ops(r1) == [("a", "add", "Oat milk", None)]
    r2 = run_c(a("Milk", "Oat milk"), b("Oat milk"), r1.snapshot, r1.pending, now=1000 + CONFIRM)
    assert ops(r2) == [("a", "set_status", "Milk", DONE)]


# -- re-adds, unchecks, rapid edits --------------------------------------------------


def test_unchecked_on_alexa_reopens_on_keep():
    snap = {"milk": {"summary": "Milk", "a": False, "b": DONE}}
    r = run(a("Milk"), b(done=("Milk",)), snap)
    assert ops(r) == [("b", "set_status", "Milk", OPEN)]


def test_unchecked_on_keep_reopens_completed_copy_on_alexa():
    snap = {"milk": {"summary": "Milk", "a": False, "b": DONE}}
    r = run(a(done=("Milk",)), b("Milk"), snap)
    assert ops(r) == [("a", "set_status", "Milk", OPEN)]


def test_item_added_and_checked_off_between_syncs_is_not_copied():
    r = run(a(done=("Milk",)), [], {})
    assert r.actions == []


def test_repeated_runs_without_changes_settle():
    r = run(a("Milk", "Eggs"), b("Bread"))
    for t in (1010, 1020, 1030):
        r = run(*settled(r), r.snapshot, r.pending, now=t)
        assert r.actions == []


# -- partial failures ----------------------------------------------------------------


def test_commit_keeps_old_state_for_failed_and_unattempted_writes():
    snap = synced("Milk")
    r = run(a("Milk", "Eggs"), b("Milk", "Bread"), snap)
    assert ops(r) == [("a", "add", "Bread", None), ("b", "add", "Eggs", OPEN)]
    new_snap, new_pending = sync.commit(r, snap, {}, {"bread"})
    assert new_snap["bread"] == {"summary": "Bread", "a": True, "b": OPEN}
    assert "eggs" not in new_snap
    assert "a:bread" in new_pending and "b:eggs" not in new_pending


def test_failed_write_is_retried_once_without_duplicates():
    r1 = run(a("Eggs"), b("Bread"))
    snap, pending = sync.commit(r1, {}, {}, {"bread"})  # add to Keep failed
    r2 = run(a("Eggs", "Bread"), b("Bread"), snap, pending, now=1010)
    assert ops(r2) == [("b", "add", "Eggs", OPEN)]


def test_commit_with_everything_done_is_the_merge_result():
    r = run(a("Eggs"), b("Bread"))
    assert sync.commit(r, {}, {}, {"eggs", "bread"}) == (r.snapshot, r.pending)


def test_commit_keeps_absence_markers_of_failed_keys():
    snap = synced("Milk", "Eggs")
    r = run_c(a("Eggs", "Bread"), b("Milk", "Eggs"), snap)
    _, pending = sync.commit(r, snap, {}, set())
    assert "gone:a:milk" in pending


# -- stored state and preview --------------------------------------------------------


def test_load_state_keeps_valid_and_drops_malformed_entries():
    stored = {
        "snapshot": {
            "milk": {"summary": "Milk", "a": True, "b": OPEN},
            "bad1": {"summary": "x", "a": "yes", "b": OPEN},
            "bad2": {"a": True, "b": OPEN},
            "bad3": {"summary": "x", "a": True, "b": "weird"},
            "bad4": "x",
        },
        "pending": {
            "a:milk": {"expect": False, "ts": 5.0},
            "b:milk": {"expect": DONE, "ts": 5},
            "gone:a:eggs": {"since": 1.0},
            "a:bad": {"expect": OPEN, "ts": 1},
            "b:bad": {"expect": True, "ts": 1},
            "gone:b:bad": {},
            "x:bad": {"expect": True, "ts": 1},
        },
    }
    snapshot, pending, dropped = sync.load_state(stored)
    assert list(snapshot) == ["milk"]
    assert sorted(pending) == ["a:milk", "b:milk", "gone:a:eggs"]
    assert dropped == 8


@pytest.mark.parametrize("stored", [None, [], "x", {"snapshot": [], "pending": None}])
def test_load_state_of_garbage_is_a_fresh_start(stored):
    assert sync.load_state(stored)[:2] == ({}, {})


def test_plan_groups_actions_for_people():
    snap = synced("Milk", "Eggs")
    r = run(a("Eggs", "Tea", done=("Milk",)), b("Milk", "Bread", done=("Eggs",)), snap)
    p = sync.plan(r)
    assert p["add_to_a"] == ["Bread"]
    assert p["add_to_b"] == ["Tea"]
    assert p["complete_on_a"] == ["Eggs"]
    assert p["complete_on_b"] == ["Milk"]
    assert p["reopen_on_a"] == p["reopen_on_b"] == []


# -- writes confirmed by a read right after them -------------------------------------


def test_settle_drops_writes_the_read_already_shows():
    r1 = run(a("Milk"), b("Eggs"))
    pending = sync.settle(r1.pending, "b", b("Eggs", "Milk"))
    assert "b:milk" not in pending and "a:eggs" in pending
    pending = sync.settle(pending, "a", a("Milk", "Eggs"))
    assert pending == {}


def test_settle_keeps_writes_that_have_not_shown_up():
    r1 = run(a("Milk"), [])
    assert sync.settle(r1.pending, "b", []) == r1.pending


def test_change_right_after_a_confirmed_write_is_not_hidden():
    # Milk is added to Keep and checked off there before the next run.
    r1 = run(a("Milk"), [])
    pending = sync.settle(r1.pending, "b", b("Milk"))
    r2 = run(a("Milk"), b(done=("Milk",)), r1.snapshot, pending, now=1010)
    assert ops(r2) == [("a", "set_status", "Milk", DONE)]
