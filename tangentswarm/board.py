"""Project 4 Status columns, matched by option id, never by name.

The board renames columns (Pending is now shown as Staging). Option ids stay the same,
so every reader maps a status value's optionId to the stable internal key below.
"""
PROJECT_ID = 'PVT_example'
STATUS_FIELD = 'PVTSSF_example'

# internal key -> option id
INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE = 'Inbox', 'Triage', 'Discuss', 'Backlog', 'Active'
IN_PROGRESS, AUTO_REVIEW, NEEDS_INPUT = 'In Progress', 'Auto Review', 'Needs Input'
READY, STAGING, HUMAN_REVIEW = 'Ready', 'Pending', 'Human Review'  # STAGING keeps the internal key "Pending"
DONE, CANCELED, PARKED = 'Done', 'Canceled', 'Parked'

OPTION = {
    INBOX: 'opt-inbox', TRIAGE: 'opt-triage', DISCUSS: 'opt-discuss', BACKLOG: 'opt-backlog', ACTIVE: 'opt-active',
    IN_PROGRESS: 'opt-in-progress', AUTO_REVIEW: 'opt-auto-review', NEEDS_INPUT: 'opt-needs-input', READY: 'opt-ready',
    STAGING: 'opt-staging', HUMAN_REVIEW: 'opt-human-review', DONE: 'opt-done', CANCELED: 'opt-canceled', PARKED: 'opt-parked',
}
BY_ID = {v: k for k, v in OPTION.items()}

# Timeline status-change events carry the column name as it was at the time.
ALIASES = {STAGING: {'Approved', 'Staging'}}


def column(value):
    """Internal key for a Status field value {name, optionId}; the name only if the id is unknown."""
    value = value or {}
    return BY_ID.get(value.get('optionId')) or value.get('name')


def event_names(*keys):
    """Every name a timeline event may use for these columns."""
    return set(keys).union(*(ALIASES.get(k, set()) for k in keys))
