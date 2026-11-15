"""Constants for To-do List Sync."""
from __future__ import annotations

DOMAIN = "todo_sync"

CONF_LIST_A = "list_a"
CONF_LIST_B = "list_b"
# Option: write to the lists. New pairs start without it (preview only);
# entries created before the option existed keep syncing.
CONF_SYNC_ENABLED = "sync_enabled"

# Coalesces the burst of state changes a single edit causes (the Alexa
# shopping list integration rewrites the whole list after each change).
DEBOUNCE_SECONDS = 10
# Safety net next to the state-change trigger.
SCAN_INTERVAL_MINUTES = 5
# How long a write of ours may take to show up on a side before the side's
# own value counts again (Google Keep Sync polls every 15 minutes by default).
PENDING_GRACE_SECONDS = 20 * 60
# An item missing from a list only counts as checked off or deleted after
# this long: a single read can be incomplete (seen with the Alexa shopping
# list add-on), and its own sync cycle is 3 minutes.
ABSENCE_CONFIRM_SECONDS = 6 * 60
# Refuse a sync that would check off more than this share of the synced
# items (and at least MASS_CHANGE_MIN of them) because they went missing on
# the other side - a login or API hiccup must not wipe the other list. A
# side that reads as completely empty is refused regardless of the count.
MASS_CHANGE_SHARE = 0.5
MASS_CHANGE_MIN = 2
# Upper bound for one todo service call; a hanging integration must not
# block the pair forever.
SERVICE_TIMEOUT_SECONDS = 60

STORAGE_VERSION = 1
SERVICE_SYNC_NOW = "sync_now"
SERVICE_PREVIEW = "preview"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_FORCE = "force"
