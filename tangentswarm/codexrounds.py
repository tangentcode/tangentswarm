"""Codex review rounds (tangentswarm #40 item 7): no cap; every request and result is a row
in the event DB, so a weekly review can list the PRs with the most rounds and why.

ask() is the one code path that posts "@codex review" (tangentswarm #63). It is
idempotent on (repo, PR, head SHA), under an interprocess lock, so the tick, the Stop hook,
the worker MCP and the webhook cannot post two asks for one head:
  - skip when Codex already answered the head (a review, a receipt or a completed summary)
    or a Codex review of the head is running;
  - skip when an ask for the head is less than ASK_WAIT s old and still pending;
  - ask again (a logged retry, not a new round) only after a Codex "Something went wrong"
    reply to the last ask, or when the last ask is ASK_WAIT s old with no answer.
An ask is known from the events DB (a round or retry row with the head), the shared
once-key in the tick state, or a person's "@codex review" on GitHub after the head commit.

    swarm-codex-rounds [--days 7] [--top 10]   the PRs with the most rounds, with each
                                               round's head and result
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime

REPO = 'tangentcode/platform'
ROUND = 'codex-round'
RETRY = 'codex-retry'
RESULT = 'codex-result'
ASK = '@codex review'
CODEX = 'chatgpt-codex-connector'
ASK_WAIT = int(os.environ.get('SWARM_CODEX_ASK_WAIT', '1800'))  # the watchdog's CODEX_SILENT
ERROR_RE = re.compile(r'something went wrong', re.I)


def _store():
    from . import eventdb
    return eventdb.store()


def rounds(n, store=None):
    """The request rows of PR n, oldest first."""
    return (store or _store()).rows("stage='event' AND event=? AND pr=?", (ROUND, int(n)))


def once_key(n, head, repo=REPO):
    """The shared once-per-head key in the tick state (platform keeps its old form)."""
    return f"codex-request:{n}@{head}" if repo == REPO else f"codex-request:{repo}#{n}@{head}"


def _login(node):
    return ((node or {}).get('author') or {}).get('login') or ''


def _ts(iso):
    try:
        return datetime.fromisoformat((iso or '').replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def asks_logged(n, head, repo=REPO, store=None):
    """The times of the logged asks (rounds and retries) of PR n for head, oldest first. A
    row with no repo is a platform row (before tangentswarm #63)."""
    out = []
    try:
        rows = (store or _store()).rows("stage='event' AND event IN (?, ?) AND pr=?", (ROUND, RETRY, int(n)))
    except Exception:
        return out
    for r in rows:
        if (r.get('repo') or REPO) != repo:
            continue
        try:
            d = json.loads(r['body'] or '{}')
        except ValueError:
            continue
        if d.get('head') == head:
            out.append(r['received'])
    return out


def asks_on_github(full):
    """The times of the "@codex review" comments by a person after the head commit."""
    nodes = ((full.get('commits') or {}).get('nodes')) or []
    head_at = _ts((nodes[0].get('commit') or {}).get('committedDate')) if nodes else None
    out = []
    for c in ((full.get('comments') or {}).get('nodes')) or []:
        if (c.get('body') or '').strip().lower() != ASK or _login(c).endswith('[bot]'):
            continue
        t = _ts(c.get('createdAt'))
        if t is not None and head_at is not None and t > head_at:
            out.append(t)
    return out


def answered(full):
    """True when Codex gave a result for the exact head (a review, a receipt naming the
    head or a completed summary of the head)."""
    from . import tick
    head = full.get('headRefOid') or ''
    try:
        return bool(head) and tick.codex_reviewed_head(dict(full, reviews=full.get('reviews') or {'nodes': []},
                                                             comments=full.get('comments') or {'nodes': []}))
    except (KeyError, TypeError):
        return False


def running(full):
    """True when a Codex review-state summary of the head says the review is running."""
    from . import codexsummary
    head = full.get('headRefOid') or ''
    for c in ((full.get('comments') or {}).get('nodes')) or []:
        if not _login(c).startswith(CODEX):
            continue
        st = codexsummary.state(c.get('body'))
        if st and st.get('commit_sha') == head and st.get('status') == 'running':
            return True
    return False


def errored_after(full, t):
    """True when Codex replied "Something went wrong" after time t."""
    for c in ((full.get('comments') or {}).get('nodes')) or []:
        if _login(c).startswith(CODEX) and ERROR_RE.search(c.get('body') or '') and (_ts(c.get('createdAt')) or 0) > t:
            return True
    return False


def decide(full, last, now, wait=ASK_WAIT):
    """(kind, why) for an ask on full's head; last is the time of the latest earlier ask
    for the head, or None. kind: 'ask' (first ask), 'retry' or 'skip'. Pure."""
    if answered(full):
        return 'skip', 'Codex already answered this head'
    if running(full):
        return 'skip', 'a Codex review of this head is running'
    if last is None:
        return 'ask', 'first ask for this head'
    if errored_after(full, last):
        return 'retry', 'Codex replied "Something went wrong"'
    age = now - last
    if age >= wait:
        return 'retry', f"no Codex answer for {int(age // 60)} min"
    return 'skip', f"an ask for this head is {int(age // 60)} min old and still pending"


def _state_time(state, key):
    """The time the once-key was marked, 0.0 when it has no readable time, else None."""
    data = getattr(state, 'data', None)
    if not isinstance(data, dict):
        return 0.0 if state is not None and state.done(key) else None
    v = (data.get('done') or {}).get(key)
    if not v:
        return None
    t = _ts(v) if isinstance(v, str) else None
    return t if t is not None else 0.0  # a key with no readable time is an old ask


def _merge_once_keys(state):
    """Add the once-keys on disk to state's in-memory keys. Only the 'done' keys: the
    caller's other unsaved state (the tick's screen hashes) is kept as it is."""
    data, path = getattr(state, 'data', None), getattr(state, 'path', None)
    if not isinstance(data, dict) or path is None:
        return
    try:
        disk = json.loads(path.read_text()).get('done') or {}
    except (OSError, ValueError, AttributeError):
        return
    mine = data.setdefault('done', {})
    for k, v in disk.items():
        mine.setdefault(k, v)


class _lock:
    """The interprocess lock around one decide-post-record (all posters share it)."""

    def __enter__(self):
        from . import tick
        path = tick.STATE_DIR / 'codex-ask.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, 'a')
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def ask(n, head, by, full, repo=REPO, sh=None, state=None, store=None, now=None, dry=False, wait=ASK_WAIT,
        retry_only=False):
    """The one way to post "@codex review" (tangentswarm #63). full is the PR read (head,
    commits, comments, reviews). Under the lock: read the earlier asks, decide(), and on
    'ask' or 'retry' post, mark the once-key and log the row with the head. A dry run
    decides only. retry_only: never a first ask (a Codex reply can only lead to a retry).
    Returns {'posted', 'kind', 'why', 'round'}."""
    from . import tick
    if full.get('headRefOid') and full['headRefOid'] != head:
        return {'posted': False, 'kind': 'skip', 'why': f"the PR head is {full['headRefOid'][:8]}, not {head[:8]}",
                'round': None}
    if (full.get('state') or 'OPEN') != 'OPEN':  # Codex on PR #92: never ask on a closed or merged PR
        return {'posted': False, 'kind': 'skip', 'why': f"the PR is {full['state'].lower()}", 'round': None}
    if state is None:
        state = tick.State(tick.STATE_DIR / 'state.json')
    with _lock():
        now = time.time() if now is None else now
        key = once_key(n, head, repo)
        _merge_once_keys(state)  # another process may have asked meanwhile
        logged = asks_logged(n, head, repo, store)
        t = None if logged else _state_time(state, key)  # the key alone: an ask before #63
        times = logged + asks_on_github(full) + ([t] if t is not None else [])
        kind, why = decide(full, max(times) if times else None, now, wait)
        if retry_only and kind == 'ask':
            kind, why = 'skip', 'no earlier ask for this head to retry'
        if kind == 'skip' or dry:
            return {'posted': False, 'kind': kind, 'why': why, 'round': None}
        sh = sh or (lambda argv: tick.sh(argv, check=True))
        sh(['gh', 'pr', 'comment', str(n), '-R', repo, '--body', ASK])
        state.mark(key)
        if kind == 'ask':
            k = log_round(n, head, by, store=store, now=now, repo=repo)
        else:
            k = log_retry(n, head, by, why, store=store, now=now, repo=repo)
        return {'posted': True, 'kind': kind, 'why': why, 'round': k}


def log_round(n, head, by, store=None, now=None, repo=REPO):
    """Log one "@codex review" request of PR n for head as the next round. Returns its number."""
    store = store or _store()
    k = len([r for r in rounds(n, store) if (r.get('repo') or REPO) == repo]) + 1
    try:
        store.add_event('codex', ROUND, action=f"round {k}", pr=int(n), actor=by, repo=repo,
                        notes=[f"round {k} on {(head or '')[:8]}"], detail={'head': head, 'round': k, 'by': by}, now=now)
    except Exception:
        pass
    return k


def log_retry(n, head, by, why, store=None, now=None, repo=REPO):
    """Log a re-ask of the same head: not a new round. Returns the head's round number."""
    store = store or _store()
    k = len([r for r in rounds(n, store) if (r.get('repo') or REPO) == repo])
    try:
        store.add_event('codex', RETRY, action=f"retry round {k}", pr=int(n), actor=by, repo=repo,
                        notes=[f"retry on {(head or '')[:8]}: {why}"],
                        detail={'head': head, 'round': k, 'by': by, 'why': why}, now=now)
    except Exception:
        pass
    return k


def result(n, head, status, detail='', store=None, now=None):
    """Log Codex's answer for a head (clean or findings, with the detail)."""
    try:
        (store or _store()).add_event('codex', RESULT, action=status, pr=int(n), notes=[detail or status],
                                      detail={'head': head, 'status': status, 'detail': detail}, now=now)
    except Exception:
        pass


def report(days=7, top=10, store=None, now=None):
    """[{pr, rounds, results: [(round, head, status, detail)]}] for the PRs with the most
    rounds in the last `days` days, most rounds first. The results after round 1 show what
    round 1 did not catch."""
    now = time.time() if now is None else now
    store = store or _store()
    since = now - days * 86400
    by_pr = {}
    for r in store.rows("stage='event' AND event IN (?, ?) AND received >= ?", (ROUND, RESULT, since)):
        d = json.loads(r['body'] or '{}')
        by_pr.setdefault(r['pr'], []).append((r['event'], d))
    out = []
    for n, rows in by_pr.items():
        reqs = [d for s, d in rows if s == ROUND]
        res = {d.get('head'): d for s, d in rows if s == RESULT}
        out.append({'pr': n, 'rounds': len(reqs),
                    'results': [(i + 1, d.get('head'), (res.get(d.get('head')) or {}).get('status', 'no answer'),
                                 (res.get(d.get('head')) or {}).get('detail', '')) for i, d in enumerate(reqs)]})
    return sorted(out, key=lambda x: (-x['rounds'], x['pr']))[:top]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-codex-rounds', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--days', type=float, default=7)
    ap.add_argument('--top', type=int, default=10)
    a = ap.parse_args(argv)
    for row in report(a.days, a.top):
        print(f"#{row['pr']}: {row['rounds']} round(s)")
        for k, head, status, detail in row['results']:
            print(f"  round {k} {(head or '?')[:8]}: {status}" + (f" ({detail})" if detail else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
