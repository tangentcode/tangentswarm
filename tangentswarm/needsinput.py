"""needs_input (tangentswarm #40 item 4): a worker never asks Michal in its pane. The
question goes on the ticket, the card moves to Needs Input, Michal gets a ping in MethBot's
chat, and the worker's Claude is closed with /exit (the pane watcher sends it once the
Claude is idle; the window stays). When Michal moves the card Needs Input -> In Progress,
swarm-webhook resumes the Claude with his answer (Executor.do_answered).
"""
import contextlib
import fcntl
import hashlib
import json
import os
import time

from . import board

REPO = 'tangentcode/platform'
ROUTE_COLS = (board.IN_PROGRESS, board.AUTO_REVIEW)


def card(g, n):
    """(item id, column, closing issue numbers, title) of open PR n from the board snapshot,
    or (None, None, [], '')."""
    from . import boardcache
    snap = boardcache.snapshot(g['gql'], max_age=0)
    for it, c in boardcache.open_prs(snap):
        if c['number'] == n:
            return it['id'], boardcache.col(it), [i['number'] for i in boardcache.closes(c)], c.get('title') or ''
    return None, None, [], ''


def question_body(n, question, source):
    from .stophook import QUESTION_MARK
    return (f"{QUESTION_MARK}\nQuestion from the worker on #{n} (the card is in Needs Input; from {source}):\n\n"
            + '\n'.join('> ' + l for l in question.strip()[-1500:].splitlines()))


def route(n, question, wt=None, source='needs_input', g=None, sh=None, st=None, ping=None, now=time.time):
    """Post the question on the ticket (the first issue the PR closes, else the PR), move the
    card to Needs Input, ping Michal and mark the worker's /exit as pending. Once per
    question. Returns a one-line note."""
    from . import alerts, tick
    n, question = int(n), (question or '').strip()
    if not question:
        raise ValueError('the question is empty')
    st = st or alerts.state()
    key = f"needs-input:{n}:{hashlib.sha1(question.encode()).hexdigest()[:12]}"
    if st.done(key):
        mark_exit(wt, n, 'the question is on the ticket already', now)
        return f"#{n}: this question was already posted"
    if g is None:
        from .advance import hr_gate
        g = hr_gate()
    sh = sh or (lambda argv: tick.sh(argv, check=True).stdout)
    item, col, issues, title = card(g, n)
    if item is None:
        raise RuntimeError(f"#{n} is not an open PR on the board")
    where = issues[0] if issues else n
    sh(['gh', 'issue', 'comment', str(where), '-R', REPO, '--body', question_body(n, question, source)])
    moved = col in ROUTE_COLS
    if moved:
        board.move_card(g, item, board.NEEDS_INPUT, sh)
    st.mark(key)
    mark_exit(wt, n, 'asked Michal (needs_input)', now)
    url = f"https://github.com/{REPO}/issues/{where}"
    text = f"Needs Input: #{n} {title}: {' '.join(question.split())[:600]} ({url})"
    (ping or alerts.tell_michal)(text, key=f"needs-input-card:{n}:{key[-12:]}", pr=n, st=st)
    mark_pinged(n, now())
    return (f"#{n}: question posted on {'issue #' + str(where) if issues else 'the PR'}; "
            + (f"card {col} -> Needs Input" if moved else f"card stays in {col}") + "; Michal pinged")


# The pending exits and ping times are written by the spool process (the tool) and the daemon
# (the watcher), so they live in their own file, read and written under a lock.

def _path():
    from . import alerts
    return alerts.STATE.with_name('needs-input.json')


@contextlib.contextmanager
def _locked():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + '.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                d = {}
            d.setdefault('exit', {})
            d.setdefault('pinged', {})
            before = json.dumps(d, sort_keys=True)
            yield d
            if json.dumps(d, sort_keys=True) != before:
                tmp = path.with_name(f'.{path.name}.{os.getpid()}')
                tmp.write_text(json.dumps(d))
                os.replace(tmp, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def mark_exit(wt, n, why, now=time.time):
    """The pane watcher sends /exit to the Claude in worktree wt once it is idle."""
    if wt:
        with _locked() as d:
            d['exit'][str(wt)] = {'pr': n, 'why': why, 'at': now()}


def pending_exits():
    """{worktree: {pr, why, at}}"""
    with _locked() as d:
        return dict(d['exit'])


def clear_exit(wt):
    with _locked() as d:
        d['exit'].pop(str(wt), None)


def mark_pinged(n, t):
    with _locked() as d:
        d['pinged'][str(n)] = t


PING_QUIET = 1800  # s: a card route() just pinged for is not pinged again by the board diff


def new_cards(st, numbers, ping, titles=None, first_run_quiet=True, now=time.time):
    """Item 4c: ping Michal for each card new in Needs Input (numbers: the PRs and issues
    there now). The first look only records them. Returns the numbers pinged."""
    numbers = sorted(set(numbers))
    t = now()
    with _locked() as d:
        d['pinged'] = pinged = {k: v for k, v in d['pinged'].items() if t - float(v) < PING_QUIET}
    if 'needs_input_seen' not in st.data and first_run_quiet:
        st.data['needs_input_seen'] = numbers
        st.save()
        return []
    seen = set(st.data.get('needs_input_seen') or [])
    out = []
    for n in numbers:
        if n in seen or t - float(pinged.get(str(n), 0)) < PING_QUIET:
            continue
        title = (titles or {}).get(n, '')
        ping(f"Needs Input: #{n} {title} https://github.com/{REPO}/issues/{n}".replace('  ', ' '),
             key=f"needs-input-card:{n}:{int(t)}", pr=n, st=st)
        out.append(n)
    st.data['needs_input_seen'] = numbers
    st.save()
    return out
