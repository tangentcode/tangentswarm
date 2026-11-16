"""Alerts of the pane watcher and watchdog (tangentswarm #40): Michal in MethBot's chat,
swarmbot wakes for a diagnosis, incident tickets (methodology #81) and event-log rows.

Michal gets a message only for the cases #40 names (a Codex outage, a stalled promote, a
new Needs Input card). Each message is one POST to the URL in ALERT_URL_FILE (with an
optional bearer key in ALERT_KEY_FILE), deduped by key. With no URL the message is logged
only. Every alert, wake and incident is also a row in the events DB (source 'watch').
"""
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


def state():
    """The watcher's state file (tick.State: once-keys merged under a lock)."""
    from . import tick
    return tick.State(STATE)


def record(kind, text, pr=None, key=None, now=None):
    """One events DB row (source 'watch', event kind). Never raises."""
    from . import eventdb
    try:
        eventdb.store().add_event('watch', kind, action=kind, pr=pr, notes=[text],
                                  detail={'key': key} if key else None, now=now)
    except Exception:
        pass


def tell_michal(text, key, pr=None, st=None, post=None, now=time.time):
    """Send one message to Michal in MethBot's chat, once per key. Returns True when sent
    (or queued for the POST), False when the key was sent before."""
    from . import webhook
    st = st or state()
    once = f"michal:{key}"
    if st.done(once):
        return False
    st.mark(once)
    payload = {'source': 'swarm-watch', 'kind': 'alert', 'to': 'michal', 'key': key, 'pr': pr, 'text': text,
               'at': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now()))}
    record('alert', text, pr=pr, key=key, now=now())
    (post or (lambda p: webhook.post_wake(p, url_file=ALERT_URL_FILE, key_file=ALERT_KEY_FILE)))(payload)
    return True


def wake_swarmbot(why, key, pr=None, detail=None, ex=None):
    """Wake swarmbot to diagnose (the webhook's deduped, rate-limited wake), with detail
    (process facts, a pane capture) in the payload."""
    from . import webhook
    ex = ex or webhook.Executor()
    record('wake', why, pr=pr, key=key)
    ex.wake(pr or 0, why, key=key, extra={'detail': detail} if detail else None)
    return ex.done[-1] if ex.done else ''


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
