"""Alerts of the pane watcher and watchdog (tangentswarm #40): swarmbot wakes for a
diagnosis, incident tickets (methodology #81) and event-log rows; and the two MethBot wakes
of Project 6 (tangentswarm #19).

MethBot (Michal 2026-11-16: "The webhook should only receive those two specific things")
gets one POST to the URL in ALERT_URL_FILE (with an optional bearer key in ALERT_KEY_FILE)
for exactly two events, and only for the Project 6 repos (METHBOT_REPOS):
  done         a worker reports done through the worker MCP (once per PR and head sha)
  needs-input  a worker asks a question through the worker MCP needs_input tool
Everything else the watchdog finds (a Codex outage, a stalled promote, a new Needs Input
card on any board) is an alert(): it wakes swarmbot and never POSTs to the MethBot URL.
Every alert, wake, MethBot POST and incident is also a row in the events DB (source 'watch').
"""
import hashlib
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
METHBOT_KINDS = ('done', 'needs-input')


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


def tell_methbot(kind, repo, pr, head=None, question=None, st=None, post=None, now=time.time):
    """Wake MethBot for one of its two events (tangentswarm #19), only for a Project 6 repo.
    kind 'done' needs the full head sha and is sent once per repo, PR and head; kind
    'needs-input' needs the question and is sent once per repo, PR and question. Returns
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
    else:
        question = (question or '').strip()
        if not question:
            return 'needs-input needs the question'
        once = f"methbot:needs-input:{repo}#{pr}:{hashlib.sha1(question.encode()).hexdigest()[:12]}"
        payload['question'] = question
        text = f"{repo}#{pr} needs input: {question}"[:2000]
    st = st or state()
    if st.done(once):
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
    if st.done(once):
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
    most INCIDENTS_PER_HOUR an hour. Returns the issue URL, 'dup', 'capped' or 'failed: ...'."""
    st = st or state()
    once = f"incident:{key}"
    if st.done(once):
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
