"""Codex review rounds (tangentswarm #40 item 7): no cap; every request and result is a row
in the event DB, so a weekly review can list the PRs with the most rounds and why.

    swarm-codex-rounds [--days 7] [--top 10]   the PRs with the most rounds, with each
                                               round's head and result
"""
import argparse
import json
import sys
import time

REPO = 'tangentcode/platform'
ROUND = 'codex-round'
RESULT = 'codex-result'


def _store():
    from . import eventdb
    return eventdb.store()


def rounds(n, store=None):
    """The request rows of PR n, oldest first."""
    return (store or _store()).rows("stage=? AND pr=?", (ROUND, int(n)))


def request(n, head, by, sh=None, state=None, store=None, now=None):
    """Post "@codex review" on PR n for head, mark the once-per-head key and log the round.
    There is no cap: a PR is reviewed again on each new head until Codex is clean.
    Returns the round number."""
    from . import tick
    sh = sh or (lambda argv: tick.sh(argv, check=True))
    sh(['gh', 'pr', 'comment', str(n), '-R', REPO, '--body', '@codex review'])
    if state is not None:
        state.mark(f"codex-request:{n}@{head}")
    return log_round(n, head, by, store=store, now=now)


def log_round(n, head, by, store=None, now=None):
    """Log one "@codex review" request of PR n for head as the next round. Returns its number."""
    store = store or _store()
    k = len(rounds(n, store)) + 1
    try:
        store.add(ROUND, f"codex-round-{n}-{(head or '')[:12]}", ROUND, decisions=[f"round {k}"],
                  notes=[json.dumps({'head': head, 'round': k, 'by': by})], pr=int(n), now=now)
    except Exception:
        pass
    return k


def result(n, head, status, detail='', store=None, now=None):
    """Log Codex's answer for a head (clean or findings, with the detail)."""
    try:
        (store or _store()).add(RESULT, f"codex-result-{n}-{(head or '')[:12]}", RESULT, decisions=[status],
                                notes=[json.dumps({'head': head, 'status': status, 'detail': detail})],
                                pr=int(n), now=now)
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
    for r in store.rows("stage IN (?, ?) AND received >= ?", (ROUND, RESULT, since)):
        d = json.loads((json.loads(r['notes']) or ['{}'])[0])
        by_pr.setdefault(r['pr'], []).append((r['stage'], d))
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
