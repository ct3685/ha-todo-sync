# To-do List Sync

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?style=for-the-badge&logo=homeassistantcommunitystore&logoColor=white)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Home Assistant 2026.10+](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2?style=for-the-badge&logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![Checks](https://img.shields.io/github/actions/workflow/status/ct3685/ha-todo-sync/validate.yml?branch=main&style=for-the-badge&label=checks)](https://github.com/ct3685/ha-todo-sync/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/ct3685/ha-todo-sync?style=for-the-badge&color=9333EA)](LICENSE)

Keeps pairs of Home Assistant to-do lists in step, in both directions - for
example the **Alexa shopping list** (Alexa Devices integration) and a
**Skylight** list ([Skylight Lists](https://github.com/ct3685/skylight_calendar)).
Any two `todo.*` entities that can add and update items work. The
integration only calls Home Assistant's `todo` actions on the two selected
entities: no Amazon or Skylight credentials, no network access, no
dependencies.

Fork of [skornehl/ha-todo-sync](https://github.com/skornehl/ha-todo-sync)
by Sebastian Kornehl (MIT), hardened for unattended Alexa ↔ Skylight use.

## How it works

Items are matched by name (case and spacing do not matter). After every
sync the integration stores the common state, so it can tell "added on one
side" from "deleted on the other side". It never deletes an item: a
deletion is mirrored as a check-off, which you can undo.

| Change | Result |
|---|---|
| Item added on either side | added on the other side (or a completed copy there is reopened) |
| Item checked off on either side | checked off on the other side |
| Item unchecked on either side | reopened on the other side |
| Item deleted on either side | checked off on the other side, after 6-10 minutes |
| Item renamed | new name added on the other side, old name checked off there after 6-10 minutes |

Put the list that may drop checked-off items in **List A** (Alexa) and the
list that keeps them in **List B** (Skylight).

### Safety

- **New pairs only preview.** Nothing is written until you switch on
  *Sync enabled* in the pair's options.
- **Reads must be complete.** A list that is unavailable, a missing or
  malformed `todo.get_items` response, an item without an id, a call that
  takes longer than 60 s, or a response whose number of open items
  differs from the entity's state stops the sync for that pair. Nothing is
  written and nothing is stored; the next run tries again.
- **Outage guard.** If a list reads as completely empty although synced
  items were on it, or if at least 2 and more than half of the synced items
  vanished from it at once, nothing is checked off on the other list. This
  covers small lists too (1-4 items). Items checked off explicitly (still
  listed, marked completed) are never blocked.
- **Missing items are confirmed.** An item that disappears only counts as
  deleted after 6 minutes; until then nothing is written for it.
- **Own writes are tracked.** A write that does not show up right away is
  not taken for a change on that list for up to 20 minutes; writes are
  re-read right after they are made, so this only applies to lists that
  really lag.
- **Failed writes are retried.** What went through is stored, the rest is
  decided again on the next run - no duplicates, nothing reversed.
- **Duplicate names.** Several *open* items with the same name on one list
  are never checked off by guesswork: that change is held back and shown
  under `conflicts` until you remove the duplicate. Completed duplicates
  (Alexa keeps last week's "Milk") are fine.
- **One list, one pair.** A list can only be in one pair; overlapping pairs
  refuse to start.
- Check-offs and reopens always target the item id, never the name.

## Setup

1. **Install:** HACS → ⋮ → *Custom repositories* →
   `https://github.com/ct3685/ha-todo-sync`, type *Integration* →
   install **To-do List Sync** → restart Home Assistant.
2. **Add a pair:** Settings → Devices & services → *Add integration* →
   *To-do List Sync*.
3. **List A:** the Alexa list, e.g. `todo.alexa_shopping_list`.
4. **List B:** the Skylight list, e.g. `todo.<frame>_grocery`.
5. **More pairs:** add the integration again for each pair, e.g. Alexa
   *Costco* ↔ Skylight *Costco*. Pairs are independent; each list can be
   in one pair only.

### Preview the first sync

The new pair starts in preview mode. Developer tools → Actions:

```yaml
action: todo_sync.preview
data:
  config_entry_id: <pair>   # pick it in the UI; omit for all pairs
```

The response (and the `preview` attribute of the pair's *Last sync*
sensor) lists `add_to_a`, `add_to_b`, `complete_on_a`, `complete_on_b`,
`reopen_on_a`, `reopen_on_b`, `remove_from_a/b` (deletions are mirrored as check-offs, so these stay empty), `conflicts`
(duplicate names), `waiting` (missing, not yet confirmed), `blocked`
(why it would not sync) and `safe`. The first sync of a pair only adds
and reopens; it never checks off or deletes anything.

### Enable syncing

Settings → Devices & services → To-do List Sync → the pair → *Configure* →
tick **Sync enabled**. The pair syncs within seconds.

### Test it

1. "Alexa, add milk to my shopping list" → Milk appears on Skylight.
2. Add bread on Skylight → Bread appears in Alexa.
3. Check off Milk in the Alexa app → completed on Skylight.
4. Check off Bread on Skylight → completed in Alexa.
5. Uncheck either → open again on both.
6. Delete an item on one side → checked off on the other after 6-10 min.
7. Rename an item → the new name appears on the other side; the old one is
   checked off there after 6-10 min.

### Delays

A sync runs 10 seconds after either list changes and every 5 minutes.
Skylight changes reach Home Assistant when Skylight Lists polls (every 2
minutes by default); Alexa changes arrive by push, usually within seconds.
So: Alexa → Skylight within ~15 s, Skylight → Alexa within ~2.5 min,
deletions 6-10 min.

### Monitoring

The pair's *Last sync* sensor shows the time of the last successful sync;
its attributes show `mode` (`sync`/`preview`), `last_error`,
`last_changes`, `conflicts`, `waiting` and the last `preview`. These
attributes are not stored in the recorder. To be notified:

```yaml
triggers:
  - trigger: template
    value_template: "{{ state_attr('sensor.<pair>_last_sync', 'last_error') is not none }}"
    for: "00:15:00"
actions:
  - action: notify.notify
    data:
      message: "To-do sync: {{ state_attr('sensor.<pair>_last_sync', 'last_error') }}"
```

### Stop syncing

Untick **Sync enabled** in the pair's options (it goes back to preview), or
disable the pair's entry. Deleting the pair also deletes its stored sync
state; the lists are not touched.

### When the outage guard blocks a real change

Clearing most of a list at once (e.g. "Alexa, clear my shopping list") looks
like an outage and is blocked (`last_error` says so). Check both lists; if
it was intended, run as an admin:

```yaml
action: todo_sync.sync_now
data:
  config_entry_id: <pair>   # required: force works on one pair only
  force: true
```

`force` only skips the outage guard; unreadable lists still stop the sync.

### Recover from a bad sync

1. Untick **Sync enabled** for the pair.
2. Fix the lists by hand: nothing is ever deleted, so wrongly checked-off
   items can be unchecked; remove unwanted duplicates.
3. Optional fresh start: delete the pair and add it again. A new pair
   forgets the old state, previews first, and its first sync only adds.
4. Run `todo_sync.preview`, then enable syncing again.

### Update safely

Before updating via HACS, untick **Sync enabled** for each pair. Update,
restart, run `todo_sync.preview`, and enable syncing again once it looks
right. Stored sync state is kept across updates and restarts.

## Limitations

- Names are identity: a rename is "new item + old item checked off", and
  two different items with the same name are one item.
- Several open items with the same name on one list block check-offs of
  that name until resolved.
- A write that times out may have gone through; the retry can then add a
  duplicate on that list.
- If Alexa's initial item fetch fails after a restart, its lists read as
  empty: the guard blocks check-offs, but items added on Skylight meanwhile
  are added to Alexa and may duplicate once Alexa's items load.
- Lists whose items have no id cannot be synced.
- Verified with automated tests against Home Assistant 2026.10.4 (pytest)
  and a real 2026.11.1 core (smoke test) with simulated lists, not against
  the live Alexa or Skylight services.

## License

MIT - see [LICENSE](LICENSE). Original work © 2026 Sebastian Kornehl.
