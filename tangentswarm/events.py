"""GitHub events instead of timers (tangentswarm #31).

The relay on the host appends every GitHub webhook event to EVENTS (one JSON line each:
pull_request, issue_comment, pull_request_review). Two timers still sat in front of work
that already had an event: mn-workflow.timer (a pass 5 minutes after the last one turns a
Codex finding into a draft and relays it) and the Codex-result poll before the MinBot check.

The swarm-promoted daemon reads the new lines every second (only the file size, until it
grows) and, for the platform repo:

  pull_request_review (any reviewer), or a Codex comment on a PR
      -> start mn-workflow.service now (no-block). A pass already running: start it again
         once that pass ends, so the event is never lost. mn-workflow's own flock prevents
         overlap; its 5-minute timer stays as a slow backstop.
  a Codex review, or a Codex comment on a PR (a review summary, a receipt)
      -> queue `swarm-advance --minbot N` for the daemon's drain: a PR in Auto Review that
         Codex just cleared gets the MinBot check at once (it does nothing for a PR outside
         Auto Review).

The read offset is kept in OFFSET as "inode:offset". With no offset yet, reading starts at the
end of the file (old events are not replayed); a new inode (rotation) is read from the start.
"""
import json
import os
import subprocess
from pathlib import Path

HOME = Path.home()
EVENTS = Path(os.environ.get('SWARM_GITHUB_EVENTS', str(HOME / 'relay' / 'github-events.jsonl')))
STATE_DIR = Path(os.environ.get('SWARM_FLOW_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'pull.json'))).parent
OFFSET = Path(os.environ.get('SWARM_EVENTS_OFFSET', str(STATE_DIR / 'events.offset')))
REPO = 'tangentcode/platform'
CODEX = 'chatgpt-codex-connector'
MN_UNIT = os.environ.get('SWARM_MN_UNIT', 'mn-workflow.service')


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _inode(path):
    try:
        return path.stat().st_ino
    except OSError:
        return 0


def _offset(path, events):
    """The saved read offset ("inode:offset"). No offset yet: the end of the file (old
    events are not replayed). Another inode (the file was rotated): 0."""
    try:
        ino, _, off = path.read_text().strip().partition(':')
        return int(off) if int(ino) == _inode(events) else 0
    except (OSError, ValueError):
        return _size(events)


def pending(events=None, offset=None):
    """True when the events file has grown past the saved offset (a stat, no read)."""
    events, offset = events or EVENTS, offset or OFFSET
    return _size(events) > _offset(offset, events)


def new_records(events=None, offset=None, save=True):
    """The event records appended since the saved offset; moves the offset to the end of the
    last complete line. A file that shrank (rotated) is read from the start."""
    events, offset = events or EVENTS, offset or OFFSET
    start, size = _offset(offset, events), _size(events)
    if size < start:
        start = 0
    if size == start:
        if save and not offset.exists():
            _save(offset, f"{_inode(events)}:{start}")
        return []
    with open(events, 'rb') as f:
        f.seek(start)
        data = f.read(size - start)
    end = data.rfind(b'\n') + 1  # a line still being written waits for the next read
    out = []
    for raw in data[:end].splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    if save:
        _save(offset, f"{_inode(events)}:{start + end}")
    return out


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(str(value))
    os.replace(tmp, path)


def is_codex(login):
    return (login or '').startswith(CODEX)


def actions(rec):
    """[(kind, PR number, why)] for one event record: kind 'mn' or 'minbot'."""
    if rec.get('repo') != REPO:
        return []
    ev = rec.get('event')
    if ev == 'pull_request_review' and rec.get('pr'):
        n = rec['pr']
        why = f"pull_request_review on platform#{n} by {rec.get('reviewer')}"
        return [('mn', n, why)] + ([('minbot', n, why)] if is_codex(rec.get('reviewer')) else [])
    if ev == 'issue_comment' and rec.get('is_pr') and is_codex(rec.get('commenter')) and rec.get('issue'):
        n = rec['issue']
        why = f"Codex comment on platform#{n}"
        return [('mn', n, why), ('minbot', n, why)]
    return []


class Events:
    """The daemon's event reader. poll() returns the lines to log."""

    def __init__(self, dry=False, run=subprocess.run, enqueue=None, events=None, offset=None):
        self.dry, self.run = dry, run
        self.enqueue = enqueue
        self.events, self.offset = events, offset
        self.mn_pending = None  # why: an mn-workflow pass is wanted once the running one ends

    def mn_active(self):
        r = self.run(['systemctl', '--user', 'is-active', MN_UNIT], capture_output=True, text=True, timeout=30)
        return (r.stdout or '').strip() in ('active', 'activating', 'reloading')

    def start_mn(self, why):
        if self.mn_active():
            self.mn_pending = why
            return f"{why}: {MN_UNIT} is running; it starts again when this pass ends"
        self.mn_pending = None
        if self.dry:
            return f"{why}: would start {MN_UNIT}"
        r = self.run(['systemctl', '--user', 'start', '--no-block', MN_UNIT], capture_output=True, text=True,
                     timeout=30)
        return f"{why}: started {MN_UNIT}" + ('' if r.returncode == 0 else f" (rc={r.returncode}: "
                                                                          f"{(r.stderr or '').strip()[:120]})")

    def queue_minbot(self, n, why):
        from . import gatelock
        from .promoted import BIN
        if self.dry:
            return f"{why}: would queue swarm-advance --minbot {n}"
        try:
            (self.enqueue or gatelock.enqueue)([str(BIN / 'swarm-advance'), '--minbot', str(n)], by='events')
            return f"{why}: queued swarm-advance --minbot {n}"
        except (OSError, ValueError) as e:
            return f"{why}: swarm-advance --minbot {n} NOT queued ({e})"

    def poll(self):
        lines = []
        mn, minbot = None, {}
        for rec in new_records(self.events, self.offset, save=not self.dry):
            for kind, n, why in actions(rec):
                if kind == 'mn':
                    mn = mn or why
                else:
                    minbot.setdefault(n, why)
        if mn or self.mn_pending:
            line = self.start_mn(mn or self.mn_pending)
            if mn or self.mn_pending is None:  # do not repeat "is running" every second
                lines.append(line)
        for n, why in minbot.items():
            lines.append(self.queue_minbot(n, why))
        return lines
