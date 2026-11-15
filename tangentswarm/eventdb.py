"""swarm-webhook event log in SQLite (Michal 2026-11-15 1:54 PM ET).

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

PATH = os.environ.get('SWARM_WEBHOOK_DB', str(Path.home() / '.local' / 'state' / 'swarm-flow' /
                                              'webhook-events.sqlite'))
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

    def __init__(self, path=None):
        path = str(path or PATH)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
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
