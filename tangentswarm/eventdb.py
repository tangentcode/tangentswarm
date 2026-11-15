"""The swarm events DB: one append-only SQLite table for everything that happens in the swarm
(tangentswarm #32, Michal 2026-11-15 2:43 PM ET). It started as the swarm-webhook event log
(1:54 PM ET); every other part now writes to it through log_event():

  source     webhook | mcp | stop-hook | launcher | slots | relay | minbot | gh | board |
             cascade | hr-gate   (the column `source`; webhook rows have 'webhook')
  actor      who acted: a worktree, a PR, the daemon, a tool name
  event      what happened (a GitHub event type, or 'move', 'launch', 'park', 'resume', ...)

The DB is ~/.local/state/swarm-flow/events.sqlite ($SWARM_EVENTS_DB). The old
webhook-events.sqlite is renamed to it once, with its rows.

Adapted from board-sync's ForwardStore (Michal 1:55 PM ET: copy it, do not write it from
scratch). Taken: the connection setup (makedirs, timeout=30, isolation_level=None,
check_same_thread=False, one threading.Lock, WAL), the columns delivery, event, action, lane,
headers, body, received, reason, lane_of() for per-PR order, and the raw headers and body,
so a delivery can be replayed from the DB. Changed: the table is `events` and append-only
(no finish/retry/prune, no status/attempts/next_at; triggers block UPDATE and DELETE);
delivery is not UNIQUE, so a redelivery gets its own row with reason 'duplicate' (the
check is a SELECT on delivery, under the lock with the insert); added received_at (ET),
stage, repo, pr, sender, sig_ok, decisions, done and error; synchronous=FULL.

Rows: stage 'received' for every inbound request, written before the server replies
(rejects and duplicates too); stage 'handled' when the worker thread has run the actions;
stage 'replay' for `swarm-webhook replay`. done is a JSON list of tags (see tags()) and
notes is the executor's own lines.
"""
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_DIR = Path.home() / '.local' / 'state' / 'swarm-flow'
PATH = os.environ.get('SWARM_EVENTS_DB') or os.environ.get('SWARM_WEBHOOK_DB') or str(_DIR / 'events.sqlite')
OLD_PATH = str(_DIR / 'webhook-events.sqlite')  # before tangentswarm #32
SOURCE = os.environ.get('SWARM_EVENT_SOURCE', '')  # this process's default source (set_source)
ET = ZoneInfo('America/New_York')
KEEP_HEADERS = ('X-GitHub-Event', 'X-GitHub-Delivery', 'X-GitHub-Hook-ID', 'X-GitHub-Hook-Installation-Target-ID',
                'X-GitHub-Hook-Installation-Target-Type', 'X-Hub-Signature-256', 'Content-Type', 'User-Agent')

# (regex on an executor line, tag). The first match wins; a line with no match gets no tag.
TAGS = [
    (r'relayed to its worker', 'relayed to Claude'),
    (r'^#\d+ -> (.+)$', 'moved card to {0}'),
    (r'woke swarmbot', 'woke swarmbot'),
    (r'exception already sent', 'wake deduped'),
    (r'queued the MinBot check', 'queued MinBot check'),
    (r'^cascade ', 'queued cascade'),
    (r'posted "@codex review"', 'requested Codex review'),
    (r'^skip', 'skipped'),
]


def tags(notes):
    out = []
    for line in notes or []:
        for rx, tag in TAGS:
            m = re.search(rx, line)
            if m:
                t = tag.format(*m.groups())
                if t not in out:
                    out.append(t)
                break
    return out


def lane_of(event, payload, item_id=None):
    """Ordering lane: events in one lane are handled strictly in arrival order (board-sync)."""
    if item_id:
        return f"item:{item_id}"
    repo = (payload.get("repository") or {}).get("full_name") or "global"
    num = (payload.get("pull_request") or payload.get("issue") or {}).get("number")
    if not num:
        run = payload.get("workflow_run") or payload.get("check_suite") or {}
        prs = run.get("pull_requests") or []
        if prs and isinstance(prs[0], dict) and prs[0].get("number"):
            num = prs[0]["number"]
    if num:
        return f"{repo}#{num}"
    if event == "push":
        return f"{repo}@{payload.get('ref') or ''}"
    if event in ("workflow_run", "check_suite"):
        sha = (payload.get("workflow_run") or payload.get("check_suite") or {}).get("head_sha") or ""
        return f"{repo}@{sha}" if sha else repo
    return repo


def meta(event, payload):
    """(action, repo, pr, sender, lane) of a GitHub payload; empty for no payload."""
    p = payload if isinstance(payload, dict) else {}
    if not p:
        return None, None, None, None, None
    lane = lane_of(event, p)
    m = re.search(r'#(\d+)$', lane)
    return (p.get('action'), (p.get('repository') or {}).get('full_name'), int(m.group(1)) if m else None,
            (p.get('sender') or {}).get('login'), lane)


class EventStore:
    """Append-only SQLite log of hook deliveries and what was done with them."""

    def __init__(self, path=None, old=None):
        path = str(path or PATH)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            migrate(old or (OLD_PATH if path == str(_DIR / 'events.sqlite') else None), path)
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, delivery TEXT, event TEXT, action TEXT, lane TEXT,
                headers TEXT, body BLOB, received REAL, received_at TEXT, reason TEXT,
                stage TEXT NOT NULL, repo TEXT, pr INTEGER, sender TEXT, sig_ok INTEGER,
                decisions TEXT NOT NULL DEFAULT '[]', done TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '[]', error TEXT)""")
            have = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
            if 'source' not in have:  # tangentswarm #32: the old rows are all webhook rows
                self._db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'webhook'")
            if 'actor' not in have:
                self._db.execute("ALTER TABLE events ADD COLUMN actor TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS ev_source ON events(source, id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS ev_delivery ON events(delivery)")
            self._db.execute("CREATE INDEX IF NOT EXISTS ev_lane ON events(lane, id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS ev_pr ON events(pr, id)")
            for verb in ('UPDATE', 'DELETE'):
                self._db.execute(f"CREATE TRIGGER IF NOT EXISTS events_no_{verb.lower()} BEFORE {verb} ON events "
                                 "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END")

    def _insert(self, row):
        cols = ', '.join(row)
        cur = self._db.execute(f"INSERT INTO events({cols}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))
        return cur.lastrowid

    @staticmethod
    def _row(stage, delivery, event, payload, headers=None, body=None, sig_ok=None, reason=None,
             decisions=(), done=(), notes=(), error=None, pr=None, now=None):
        action, repo, ppr, sender, lane = meta(event, payload)
        now = time.time() if now is None else now
        return dict(delivery=delivery, event=event, action=action, lane=lane,
                    headers=None if headers is None else json.dumps(
                        {k: v for k, v in dict(headers).items() if k in KEEP_HEADERS and v}),
                    body=body, received=now,
                    received_at=datetime.fromtimestamp(now, ET).isoformat(timespec='seconds'),
                    reason=reason, stage=stage, repo=repo, pr=pr if pr is not None else ppr, sender=sender,
                    sig_ok=None if sig_ok is None else int(bool(sig_ok)),
                    decisions=json.dumps(list(decisions)), done=json.dumps(list(done) + tags(notes)),
                    notes=json.dumps(list(notes)), error=error)

    def add_received(self, delivery, event, payload, headers, body, sig_ok, reason=None, decisions=(),
                     error=None, now=None):
        """The 'received' row of one inbound request. Returns (row id, duplicate): a delivery
        id already received with a good signature gets reason 'duplicate' (GitHub redelivery)."""
        with self._lock:
            dup = False
            if sig_ok and not reason and delivery:
                dup = self._db.execute("SELECT 1 FROM events WHERE delivery=? AND stage='received' AND sig_ok=1 "
                                       "AND reason IS NULL LIMIT 1", (delivery,)).fetchone() is not None
            reason = 'duplicate' if dup else reason
            done = {'duplicate': ['dropped as duplicate'], 'bad signature': ['rejected bad signature']}.get(
                reason, ['error'] if error else (['queued'] if not reason else []))
            rid = self._insert(self._row('received', delivery, event, payload, headers, body, sig_ok, reason,
                                         () if dup else decisions, done, error=error, now=now))
            return rid, dup

    def add(self, stage, delivery, event, payload=None, decisions=(), notes=(), error=None, pr=None, now=None):
        """A 'handled' or 'replay' row (no raw headers or body: the 'received' row has them)."""
        with self._lock:
            return self._insert(self._row(stage, delivery, event, payload, decisions=decisions,
                                          done=['error'] if error else [], notes=notes, error=error, pr=pr, now=now))

    def add_event(self, source, event, action=None, pr=None, actor=None, decisions=(), notes=(), error=None,
                  detail=None, repo=None, now=None):
        """One row of any source (stage 'event'): detail is a JSON-able dict kept in body."""
        now = time.time() if now is None else now
        row = dict(delivery=None, event=event, action=action, lane=None, headers=None,
                   body=None if detail is None else json.dumps(detail, default=str)[:20000],
                   received=now, received_at=datetime.fromtimestamp(now, ET).isoformat(timespec='seconds'),
                   reason=None, stage='event', repo=repo, pr=pr, sender=None, sig_ok=None,
                   decisions=json.dumps(list(decisions)), done=json.dumps((['error'] if error else []) + tags(notes)),
                   notes=json.dumps([str(x)[:2000] for x in notes]), error=error, source=source, actor=actor)
        with self._lock:
            return self._insert(row)

    def handled(self, delivery):
        """True when a 'handled' row exists for the delivery."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM events WHERE delivery=? AND stage='handled' LIMIT 1",
                                    (delivery,)).fetchone() is not None

    def unhandled(self, since, before):
        """[(delivery, event, body)] of good, queued 'received' rows with received in
        [since, before) and no 'handled' row: deliveries that may have been lost between the
        receipt and the inbox (Codex on PR #26)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT delivery, event, body FROM events r WHERE stage='received' AND sig_ok=1 AND reason IS NULL "
                "AND received >= ? AND received < ? AND NOT EXISTS (SELECT 1 FROM events h WHERE "
                "h.delivery=r.delivery AND h.stage='handled') ORDER BY id", (since, before)).fetchall()
        return [(r['delivery'], r['event'], r['body']) for r in rows]

    def body_of(self, delivery):
        """(event, payload) of the first good 'received' row of a delivery, or None (replay)."""
        with self._lock:
            r = self._db.execute("SELECT event, body FROM events WHERE delivery=? AND stage='received' AND sig_ok=1 "
                                 "AND reason IS NULL ORDER BY id LIMIT 1", (delivery,)).fetchone()
        return (r['event'], json.loads(r['body'])) if r else None

    def count(self):
        with self._lock:
            return self._db.execute("SELECT count(*) FROM events").fetchone()[0]

    def rows(self, where='1', args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(f"SELECT * FROM events WHERE {where} ORDER BY id", args)]


WAKE_BACKOFF = 10          # board-sync FORWARD_BACKOFF: first retry delay, doubled each attempt
WAKE_BACKOFF_MAX = 900
WAKE_MAX_ATTEMPTS = 5


def backoff(attempts, retry_after=None, base=WAKE_BACKOFF, cap=WAKE_BACKOFF_MAX):
    """board-sync's backoff(): the wait before the next attempt."""
    return max(retry_after or 0, min(cap, base * 2 ** (attempts - 1)))


_store = None
_store_lock = threading.Lock()


def store():
    """The process's EventStore (one connection, one lock)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = EventStore()
        return _store


def migrate(old, new):
    """Rename the old webhook-events DB (and its -wal and -shm files) to the new path once,
    under a lock, when the new one does not exist yet."""
    if not old or os.path.exists(new) or not os.path.exists(old):
        return False
    import fcntl
    with open(os.path.join(os.path.dirname(new), '.events-migrate.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if os.path.exists(new) or not os.path.exists(old):
            return False
        for suffix in ('-wal', '-shm', ''):  # the main file last: a reader never sees it without its WAL
            if os.path.exists(old + suffix):
                os.replace(old + suffix, new + suffix)
    return True


def set_source(source):
    """The default source of this process's log_event() rows (each entry point sets it)."""
    global SOURCE
    SOURCE = source


def log_event(event, source=None, action=None, pr=None, actor=None, decisions=(), notes=(), error=None,
              detail=None, repo=None):
    """Append one event row. Never raises: logging must not break the action it records."""
    try:
        return store().add_event(source or SOURCE or 'other', event, action=action, pr=pr, actor=actor,
                                 decisions=decisions, notes=notes, error=error, detail=detail, repo=repo)
    except Exception:
        return None


_GH_WRITES = {('pr', 'ready'), ('pr', 'comment'), ('pr', 'edit'), ('pr', 'create'), ('pr', 'close'),
              ('pr', 'merge'), ('pr', 'reopen'), ('issue', 'close'), ('issue', 'comment'), ('issue', 'edit'),
              ('issue', 'create'), ('issue', 'reopen'), ('project', 'item-add'), ('label', 'create'),
              ('run', 'rerun')}


def command_kind(argv):
    """'minbot' for a minbot --apply call, 'gh' for a gh call that writes, else None (reads
    are not logged: they are many and change nothing). A card move (gh project item-edit)
    is logged by board.move_card with its PR number, not here."""
    argv = [str(a) for a in argv]
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base == 'minbot' or base == os.path.basename(os.environ.get('SWARM_MINBOT', 'minbot')):
        return 'minbot' if '--apply' in argv else None
    if base != 'gh' or len(argv) < 3:
        return None
    if tuple(argv[1:3]) in _GH_WRITES:
        return 'gh'
    if argv[1] == 'api':
        i = argv.index('-X') + 1 if '-X' in argv else argv.index('--method') + 1 if '--method' in argv else 0
        if i and i < len(argv) and argv[i].upper() in ('POST', 'PATCH', 'PUT', 'DELETE'):
            return 'gh'
    return None


def log_command(argv, rc, who='', output=''):
    """Log a minbot or gh write (see command_kind). Never raises."""
    kind = command_kind(argv)
    if not kind:
        return None
    argv = [str(a) for a in argv]
    pr = None
    for a in argv[1:]:
        if a.isdigit() and 0 < int(a) < 1_000_000:
            pr = int(a)
            break
    words = [a for a in argv[1:] if not a.startswith('-')][:3]
    return log_event('command', source=kind, action=' '.join(words), pr=pr, actor=who or SOURCE or None,
                     notes=[' '.join(a if len(a) < 200 else a[:200] + '...' for a in argv)[:1500]],
                     error=None if rc == 0 else f'rc={rc}: {str(output or "")[:300]}')
