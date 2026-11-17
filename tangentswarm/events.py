"""GitHub events instead of timers (tangentswarm #31).

The relay on the host appends every GitHub webhook event to EVENTS (one JSON line each:
pull_request, issue_comment, pull_request_review). Two timers still sat in front of work
that already had an event: mn-workflow.timer (a pass 5 minutes after the last one turns a
Codex finding into a draft and relays it) and the Codex-result poll before the MinBot check.

The swarm-promoted daemon reads the new lines every second (only the file size, until it
grows) and:

  a submitted pull_request_review (any reviewer), or a Codex comment on a PR, in a repo that
  mn-workflow runs for (the "repos" of MN_CONFIG; the platform repo if it cannot be read)
      -> start mn-workflow.service now (no-block). A pass already running: start it again
         once that pass ends, so the event is never lost. That wanted pass is kept in
         MN_PENDING, so a daemon restart does not lose it either. mn-workflow's own flock
         prevents overlap; its 5-minute timer stays as a slow backstop.

For the platform repo only (swarm-webhook queues the other repos' MinBot checks itself):
  pull_request edited (a body edit, such as added screenshots) on a PR whose pull waits or
  is held -> a cascade pass now (re-runs its gate), and the MinBot check.
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
MN_CONFIG = Path(os.environ.get('SWARM_MN_CONFIG', str(HOME / 'ver' / 'mn-workflow' / 'config.json')))
MN_PENDING = Path(os.environ.get('SWARM_MN_PENDING', str(STATE_DIR / 'mn-pending')))
REVIEW_ACTIONS = ('submitted', None, '')  # an old record without "action" is a submit


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
    """True when the events file size differs from the saved offset (a stat, no read):
    grown, or truncated in place (Codex on PR #26: a copy-truncate keeps the inode, and a
    smaller file was never read again until it grew past the old size). The first call
    saves the current end as the offset, so later events are seen."""
    events, offset = events or EVENTS, offset or OFFSET
    if not offset.exists():
        _save(offset, f"{_inode(events)}:{_size(events)}")
        return False
    return _size(events) != _offset(offset, events)


def new_records(events=None, offset=None, save=True):
    """The event records appended since the saved offset; moves the offset to the end of the
    last complete line. A file that shrank (rotated) is read from the start."""
    events, offset = events or EVENTS, offset or OFFSET
    start, size = _offset(offset, events), _size(events)
    if size < start:
        start = 0
    if size == start:
        if save and (not offset.exists() or _offset(offset, events) != start):  # e.g. truncated to empty
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


def mn_repos(path=None):
    """The repos mn-workflow runs for, lower case: the keys of "repos" in its config. The
    platform repo alone when the config cannot be read."""
    try:
        repos = json.loads((path or MN_CONFIG).read_text()).get('repos') or {}
        out = {str(r).lower() for r in repos}
    except (OSError, ValueError, AttributeError):
        out = set()
    return out or {REPO}


def _short(repo):
    return 'platform' if repo == REPO else repo


def actions(rec, repos=None):
    """[(kind, PR number, why)] for one event record: kind 'mn', 'minbot' or 'gate'.
    repos: the repos mn-workflow runs for (mn_repos())."""
    repo = rec.get('repo') or ''
    ev = rec.get('event')
    mn_ok = repo.lower() in (repos if repos is not None else mn_repos())
    if repo != REPO and not mn_ok:
        return []
    platform = repo == REPO
    if ev == 'pull_request_review' and rec.get('pr'):
        if rec.get('action') not in REVIEW_ACTIONS:  # edited or dismissed: no new finding
            return []
        n = rec['pr']
        why = f"pull_request_review on {_short(repo)}#{n} by {rec.get('reviewer')}"
        return ([('mn', n, why)] if mn_ok else []) + \
            ([('minbot', n, why)] if platform and is_codex(rec.get('reviewer')) else [])
    if not platform:
        if ev == 'issue_comment' and rec.get('is_pr') and is_codex(rec.get('commenter')) and rec.get('issue') \
                and rec.get('action') in ('created', None, ''):
            return [('mn', rec['issue'], f"Codex comment on {repo}#{rec['issue']}")]
        return []
    if ev == 'pull_request' and rec.get('action') == 'edited' and rec.get('pr'):
        n = rec['pr']
        why = f"pull_request edited on platform#{n} (a body edit: screenshots?)"
        return [('gate', n, why), ('minbot', n, why)]
    if ev == 'issue_comment' and rec.get('is_pr') and is_codex(rec.get('commenter')) and rec.get('issue') \
            and rec.get('action') in ('created', None, ''):  # an edited comment is no new result
        n = rec['issue']
        why = f"Codex comment on platform#{n}"
        return ([('mn', n, why)] if mn_ok else []) + [('minbot', n, why)]
    return []


class Events:
    """The daemon's event reader. poll() returns the lines to log."""

    def __init__(self, dry=False, run=subprocess.run, enqueue=None, events=None, offset=None, cascade=None,
                 pending_file=None, mn_config=None):
        self.dry, self.run = dry, run
        self.cascade = cascade
        self.enqueue = enqueue
        self.events, self.offset = events, offset
        self.pending_file = pending_file or MN_PENDING
        self.mn_config = mn_config
        try:  # a pass wanted before a daemon restart is still wanted
            self._mn_pending = self.pending_file.read_text().strip() or None
        except OSError:
            self._mn_pending = None

    @property
    def mn_pending(self):
        """why: an mn-workflow pass is wanted once the running one ends (kept in pending_file)."""
        return self._mn_pending

    @mn_pending.setter
    def mn_pending(self, why):
        if why == self._mn_pending:
            return
        self._mn_pending = why
        if self.dry:
            return
        try:
            if why:
                _save(self.pending_file, why)
            else:
                self.pending_file.unlink(missing_ok=True)
        except OSError:
            pass  # the in-memory flag still works; the timer is the backstop

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
        mn, minbot, gate = None, {}, {}
        recs = new_records(self.events, self.offset, save=not self.dry)
        repos = mn_repos(self.mn_config) if recs else set()
        for rec in recs:
            for kind, n, why in actions(rec, repos):
                if kind == 'mn':
                    mn = mn or why
                elif kind == 'gate':
                    gate.setdefault(n, why)
                else:
                    minbot.setdefault(n, why)
        if mn or self.mn_pending:
            line = self.start_mn(mn or self.mn_pending)
            if mn or self.mn_pending is None:  # do not repeat "is running" every second
                lines.append(line)
        for n, why in minbot.items():
            lines.append(self.queue_minbot(n, why))
        for n, why in gate.items():
            line = self.queue_gate(n, why)
            if line:
                lines.append(line)
        return lines

    def queue_gate(self, n, why):
        """A body edit on a PR whose pull waits (SCREENSHOTS_NEEDED) or is held (NEEDS_WORKER):
        a hold that is not a conflict ends (tangentswarm #101), and a cascade pass re-runs its
        gate now (MethBot 2026-11-15: #519 got its images at 2:48
        AM ET, and nothing re-ran the gate for 35 min)."""
        from . import advance, flow
        hd = flow.held()
        if n not in flow.pulled_prs() and n not in hd:
            return None
        if self.dry:
            free = n in hd and not flow.conflict_hold(hd[n])
            return f"{why}: would {'release the hold and ' if free else ''}queue a cascade pass"
        # tangentswarm #101: a body edit releases a hold that is not a conflict (platform #516)
        free = flow.release_on_edit(n) is not None
        advance.flag_rerun(by=f"event: {why}")
        return f"{why}: {'hold released; ' if free else ''}cascade {(self.cascade or advance.queue_cascade)()}"
