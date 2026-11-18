"""The stall notice (tangentswarm #146, Michal 2026-11-18 2:17 PM ET: "when something like this
happens, you need to tell me right away, explain what's happening that is preventing the flow").

When the fill cannot keep Human Review at its target, the cascade (Advance.stall) sends one
plain-English notice that names the blocked PR and why, for example "#580 (the top Ready PR)
blocks it: it conflicts with main in web/a.ts" or "Ready is empty". A log line alone is not
enough.

The notice is one JSON POST {source, kind: 'stall', pr, text, at} to the URL in STALL_URL_FILE
(SWARM_STALL_URL_FILE; default ~/.config/tangentswarm/stall-url), with an optional bearer key
in STALL_KEY_FILE (SWARM_STALL_KEY_FILE), and webhook.post_wake's retries. With no URL it
wakes swarmbot instead (alerts.wake_swarmbot), so the notice is never only a log line. It is
sent once per stall (key: the PR, its head and the kind of block); the key is marked only
after a delivered POST, so a failed one is tried again on the next pass. When Human Review
is full again, clear() forgets the key, so the next stall is told again. Each notice is also
an events DB row (kind 'stall').
"""
import json
import os
import re
import time
from pathlib import Path

CONF = Path.home() / '.config' / 'tangentswarm'
STALL_URL_FILE = Path(os.environ.get('SWARM_STALL_URL_FILE', str(CONF / 'stall-url')))
STALL_KEY_FILE = Path(os.environ.get('SWARM_STALL_KEY_FILE', str(CONF / 'stall-key')))


def state_path():
    from . import alerts
    return Path(alerts.STATE).parent / 'stall.json'  # read at call time: tests move STATE


def _load():
    try:
        d = json.loads(state_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(d))
    os.replace(tmp, path)


def key(pr, head, why):
    """One key per stall: the PR, its head and the kind of block (the text up to the first
    ';', '(' or ':'), so a changing detail (a CI job name) does not send it again."""
    kind = re.split(r'[;(:]', why or '', maxsplit=1)[0].strip()
    return f"#{pr or '-'}@{(head or '')[:12]}:{kind}"


def clear():
    """Human Review is full again: the next stall is told again."""
    if _load().get('key'):
        _save({})


def _url_set():
    try:
        return bool(STALL_URL_FILE.read_text().strip())
    except OSError:
        return False


def notify(text, k, pr=None, post=None, wake=None, now=time.time):
    """Send the stall notice text once for key k. Returns a short note for the log line."""
    from . import alerts
    if _load().get('key') == k:
        return 'notice already sent'
    t = now()
    if _url_set():
        from . import webhook
        payload = {'source': 'swarm', 'kind': 'stall', 'pr': pr, 'text': text, 'at': alerts.et_stamp(t)}
        ok = (post or webhook.post_wake)(payload, url_file=STALL_URL_FILE, key_file=STALL_KEY_FILE,
                                         background=False)
        if ok is False:
            alerts.record('stall-failed', text, pr=pr, key=k, now=t)
            return 'the POST to the stall webhook failed; the next pass tries again'
        alerts.record('stall', text, pr=pr, key=k, now=t)
        note = 'sent to the stall webhook'
    else:  # wake_swarmbot writes the events DB row (kind 'stall')
        (wake or alerts.wake_swarmbot)(text, f"stall:{k}", pr=pr, kind='stall', now=t)
        note = f'no stall webhook ({STALL_URL_FILE}): swarmbot woken'
    _save({'key': k, 'at': t, 'text': text})
    return note
