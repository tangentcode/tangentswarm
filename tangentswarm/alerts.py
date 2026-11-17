"""Alerts of the pane watcher and watchdog (tangentswarm #40): swarmbot wakes for a
diagnosis, incident tickets (methodology #81) and event-log rows; and the two MethBot wakes
of Project 6 (tangentswarm #19).

MethBot (Michal 2026-11-16: "The webhook should only receive those two specific things")
gets one POST to the URL in ALERT_URL_FILE (with an optional bearer key in ALERT_KEY_FILE)
for these events, and only for the Project 6 repos (METHBOT_REPOS):
  needs-input  a worker asks a question through the worker MCP needs_input tool
  exception    a done report or MERGE-READY comment that the swarm could not merge, with the
               reasons (tangentswarm #89: the merge itself has no AI; once per PR and head,
               and once more per PR and head for a cleanup step that failed after the merge)
  pull         after such a merge, the next Project 6 Backlog issues to start (once per merge)
  done         kept for callers of #19; the worker MCP no longer sends it (p6merge merges)
Everything else the watchdog finds (a Codex outage, a stalled promote, a new Needs Input
card on any board) is an alert(): it wakes swarmbot and never POSTs to the MethBot URL.
Every alert, wake, MethBot POST and incident is also a row in the events DB (source 'watch').
"""
import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

HOME = Path.home()
CONF = HOME / '.config' / 'tangentswarm'
ALERT_URL_FILE = Path(os.environ.get('SWARM_ALERT_URL_FILE', str(CONF / 'methbot-alert-url')))
ALERT_KEY_FILE = Path(os.environ.get('SWARM_ALERT_KEY_FILE', str(CONF / 'methbot-alert-key')))
STATE = Path(os.environ.get('SWARM_WATCH_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'watch.json')))
INCIDENT_REPO = 'tangentcode/methodology'
INCIDENT_PROJECT = '6'
INCIDENTS_PER_HOUR = int(os.environ.get('SWARM_INCIDENTS_PER_HOUR', '6'))
METHBOT_REPOS = ('tangentcode/tangentswarm', 'tangentcode/methodology')  # Project 6
METHBOT_KINDS = ('done', 'needs-input', 'exception', 'pull')


def state():
    """The watcher's state file (tick.State: once-keys merged under a lock)."""
    from . import tick
    return tick.State(STATE)


def record(kind, text, pr=None, key=None, now=None, repo=None):
    """One events DB row (source 'watch', event kind). Never raises."""
    from . import eventdb
    try:
        eventdb.store().add_event('watch', kind, action=kind, pr=pr, notes=[text],
                                  detail={'key': key} if key else None, repo=repo, now=now)
    except Exception:
        pass


@contextlib.contextmanager
def once_lock(once):
    """An interprocess lock of one once-key (Codex on PR #71): the check, the action and the
    mark of a once-key run under it, so two processes (two spool serves of the same report,
    or swarm-watch --once beside the daemon) cannot both act before either marks it."""
    locks = Path(STATE).parent / 'watch-locks'  # read at call time: tests move STATE
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / (hashlib.sha1(once.encode()).hexdigest()[:20] + '.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def done_now(st, once):
    """st.done(once), with the state file read again: another process may have marked it
    after st was loaded. Call it under once_lock."""
    if st.done(once):
        return True
    path = getattr(st, 'path', None)
    if path is None:
        return False
    try:
        disk = (json.loads(Path(path).read_text()).get('done') or {})
    except (OSError, ValueError, AttributeError):
        return False
    if once in disk:
        st.data['done'][once] = disk[once]
        return True
    return False


def et_stamp(t):
    """Epoch seconds t as an ISO time in US Eastern (the 'at' of a MethBot POST)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo('America/New_York')).isoformat(timespec='seconds')
    except Exception:
        return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t))


def methbot_post(payload):
    """The real POST to MethBot's URL with its bearer key: synchronous, with webhook.post_wake's
    retries. A worker MCP report runs in a short-lived spool serve process, so a POST in a
    daemon thread died with it (Codex on PR #71). True on a 2xx; False with no URL or after
    the last failed try."""
    from . import webhook
    return webhook.post_wake(payload, url_file=ALERT_URL_FILE, key_file=ALERT_KEY_FILE, background=False)


def tell_methbot(kind, repo, pr, head=None, question=None, st=None, post=None, now=time.time, reason=None,
                 key=''):
    """Wake MethBot for one of its events (tangentswarm #19, #89), only for a Project 6 repo.
    kind 'done' needs the full head sha and is sent once per repo, PR and head; kind
    'needs-input' needs the question and is sent once per repo, PR and question; kinds
    'exception' and 'pull' need the full head sha and the reason text, and are sent once per
    repo, PR, head and key (key '' for the merge exception and the pull). Returns
    True when MethBot took it, else the reason it was not sent. The once-key is marked only
    after a delivered POST, so a report repeated after a failed POST tries again (Codex on
    PR #71). post answers False for a failed POST (anything else is delivered)."""
    if kind not in METHBOT_KINDS:
        return f"not a MethBot event: {kind!r}"
    if repo not in METHBOT_REPOS:
        return f"{repo or 'no repo'} is not a Project 6 repo: MethBot is not woken"
    pr = int(pr)
    payload = {'source': 'swarm', 'kind': kind, 'repo': repo, 'pr': pr}
    if kind == 'done':
        if len(head or '') != 40:
            return f"done needs the full head sha (got {head!r})"
        once = f"methbot:done:{repo}#{pr}@{head}"
        payload['head'] = head
        text = f"{repo}#{pr} done at {head[:8]}"
    elif kind in ('exception', 'pull'):
        if len(head or '') != 40:
            return f"{kind} needs the full head sha (got {head!r})"
        reason = (reason or '').strip()
        if not reason:
            return f"{kind} needs the reason"
        once = f"methbot:{kind}:{repo}#{pr}@{head}" + (f":{key}" if key else '')
        payload.update(head=head, reason=reason[:4000])
        text = f"{repo}#{pr} {kind} at {head[:8]}: {reason}"[:2000]
    else:
        question = (question or '').strip()
        if not question:
            return 'needs-input needs the question'
        once = f"methbot:needs-input:{repo}#{pr}:{hashlib.sha1(question.encode()).hexdigest()[:12]}"
        payload['question'] = question
        text = f"{repo}#{pr} needs input: {question}"[:2000]
    st = st or state()
    with once_lock(once):  # a second report waits here, then finds the key marked
        if done_now(st, once):
            return 'already sent'
        t = now()
        payload['at'] = et_stamp(t)
        if (post or methbot_post)(payload) is False:
            record(f'methbot-{kind}-failed', text, pr=pr, key=once, now=t, repo=repo)
            return 'the POST to MethBot failed (no URL, or every try failed; see the webhook log)'
        st.mark(once)
    record(f'methbot-{kind}', text, pr=pr, key=once, now=t, repo=repo)
    return True


def alert(text, key, pr=None, st=None, post=None, now=time.time):
    """A watchdog alert (a Codex outage, a stalled promote, a new Needs Input card): a
    swarmbot wake, once per key, logged as an 'alert' row. It never POSTs to the MethBot URL
    (tangentswarm #19). Returns True when sent (or queued), False when the key was sent
    before."""
    st = st or state()
    once = f"alert:{key}"
    with once_lock(once):
        if done_now(st, once):
            return False
        st.mark(once)
    wake_swarmbot(text, once, pr=pr, post=post, kind='alert', now=now())
    return True


def wake_swarmbot(why, key, pr=None, detail=None, post=None, dry=False, kind='wake', now=None):
    """Wake swarmbot to diagnose (the webhook's deduped, rate-limited wake), with detail
    (process facts, a pane capture) in the payload. Logged as one events DB row of kind
    kind. Returns the wake's note line."""
    from . import webhook
    record(kind, why, pr=pr, key=key, now=now)
    return webhook.wake_exception(pr or 0, why, key=key, dry=dry, post=post, source='swarm-watch',
                                  extra={'detail': detail} if detail else None)


def incident_body(what, why, fix, prevent=None):
    """The incident ticket (methodology #81): what happened, why, how it was fixed, how to
    prevent it. The watcher fills what it knows; swarmbot fills the diagnosis."""
    return (f"Incident report (methodology #81), opened by the pane watcher (tangentswarm #40).\n\n"
            f"## What happened\n{what}\n\n## Why\n{why or 'Pending: swarmbot diagnoses.'}\n\n"
            f"## Fix\n{fix or 'Pending.'}\n\n## Prevention\n{prevent or 'Pending: swarmbot proposes it.'}\n")


def incident(key, title, what, why=None, fix=None, prevent=None, st=None, sh=None, now=time.time):
    """Open one incident ticket on the methodology board (Project 6), once per key and at
    most INCIDENTS_PER_HOUR an hour. Returns the issue URL, 'dup', 'capped' or 'failed: ...'.
    The check, the gh calls and the mark run under once_lock (Codex on PR #71)."""
    st = st or state()
    once = f"incident:{key}"
    with once_lock(once):
        return _incident(once, key, title, what, why, fix, prevent, st, sh, now)


def _incident(once, key, title, what, why, fix, prevent, st, sh, now):
    if done_now(st, once):
        return 'dup'
    t = now()
    recent = [x for x in st.data.get('incident_times', []) if t - x < 3600]
    if len(recent) >= INCIDENTS_PER_HOUR:
        record('incident-capped', f"{title}: {what}"[:500], key=key, now=t)
        return 'capped'
    if sh is None:
        from . import tick
        sh = lambda argv: tick.sh(argv, check=True).stdout  # noqa: E731
    stamp = time.strftime('%Y-%m-%d %-I:%M %p ET', time.localtime(t))
    try:
        url = sh(['gh', 'issue', 'create', '--repo', INCIDENT_REPO, '--title', f"Incident {stamp}: {title}",
                  '--body', incident_body(what, why, fix, prevent)]).strip().splitlines()[-1]
        sh(['gh', 'project', 'item-add', INCIDENT_PROJECT, '--owner', 'tangentcode', '--url', url])
    except Exception as e:
        record('incident-failed', f"{title}: {type(e).__name__}: {e}"[:500], key=key, now=t)
        return f"failed: {type(e).__name__}: {str(e)[:160]}"
    st.data['incident_times'] = recent + [t]
    st.mark(once)
    record('incident', f"{title}: {url}", key=key, now=t)
    return url
