"""GraphQL points and rate limits (Michal 2026-11-14 8:50 PM ET).

At about 8:50 PM ET the tangentcode-minbot App installation used its whole GraphQL quota
(5000 points an hour): cascade pass 2, flow-status and minbot card moves all failed on
"API rate limit exceeded". Every GraphQL read goes through hr-gate's gql(), which calls
call() here:

  points   each query also asks for rateLimit{cost remaining resetAt}. The cost is added
           to TALLY (this process) and one line goes to LOG (tab-separated: utc time,
           program, query shape, cost, remaining, resetAt). `python -m tangentswarm.gqlmeter`
           sums the log per program and per query shape.
  limit    a rate-limit error raises RateLimited with the reset time: from the answer when
           the last query showed remaining 0, else from `gh api rate_limit` (a REST read,
           free of GraphQL points). There is no retry and no fallback to another token.

    python -m tangentswarm.gqlmeter [--since MINUTES] [--passes]   points per program (or per
                                       cascade pass) and query shape
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PASS_ENV = 'SWARM_GQL_PASS'  # a cascade pass id; child processes (hr-gate) inherit it
LOG = Path(os.environ.get('SWARM_GQL_LOG', str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'gql.log')))
RATE_RE = re.compile(r'(?:API )?rate limit (?:already )?exceeded|secondary rate limit', re.I)
TALLY = {'calls': 0, 'points': 0, 'remaining': None, 'reset': None, 'by': {}}


class RateLimited(RuntimeError):
    """GraphQL (or REST) quota used up. reset: epoch seconds when it comes back, or None."""

    def __init__(self, msg, reset=None):
        super().__init__(msg)
        self.reset = reset

    def when(self):
        return when(self.reset)


def when(reset):
    if not reset:
        return 'an unknown time'
    try:
        from zoneinfo import ZoneInfo
        t = datetime.fromtimestamp(reset, ZoneInfo('America/New_York')).strftime('%H:%M:%S ET')
    except Exception:
        t = datetime.fromtimestamp(reset).strftime('%H:%M:%S')
    return f"{t} (in {max(0, int(reset - time.time()))}s)"


def is_rate_limit(text):
    return bool(RATE_RE.search(text or ''))


def limited(e):
    """True for RateLimited, or any error whose text is a GitHub rate-limit error (a minbot
    write, a tool's output). Sets e.reset (the reset epoch, or None) when it can."""
    if isinstance(e, RateLimited):
        return True
    if is_rate_limit(str(e)):
        try:
            e.reset = getattr(e, 'reset', None) or reset_time()
        except AttributeError:
            pass
        return True
    return False


def inject(q):
    """q with rateLimit{cost remaining resetAt} as the first top-level field (not mutations)."""
    s = q.lstrip()
    if s.startswith('mutation') or 'rateLimit' in q:
        return q
    i = q.find('{')
    return q if i < 0 else q[:i + 1] + 'rateLimit{cost remaining resetAt} ' + q[i + 1:]


def shape(q):
    """A short name for a query: its top-level fields after the variables, for the log."""
    s = re.sub(r'\s+', ' ', q)
    s = re.sub(r'^\s*query\s*(\([^)]*\))?\s*', '', s)
    s = re.sub(r'\([^)]*\)', '', s)      # arguments
    s = re.sub(r'[^A-Za-z0-9{} ]', '', s)
    parts = re.findall(r'[A-Za-z0-9]+', s.split('nodes')[0] if 'nodes' in s else s)
    parts = [p for p in parts if p not in ('rateLimit', 'cost', 'remaining', 'resetAt', 'pageInfo',
                                          'hasNextPage', 'endCursor')]
    import hashlib
    return '.'.join(parts)[:40] + '#' + hashlib.sha1(q.encode()).hexdigest()[:5]


def _epoch(iso):
    try:
        return datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def reset_time(run=subprocess.run):
    """Epoch seconds of the GraphQL quota reset, from `gh api rate_limit` (REST; through the
    ghshim it reads the minbot installation's limits). None if unknown."""
    if TALLY['remaining'] == 0 and TALLY['reset']:
        return TALLY['reset']
    try:
        r = run(['gh', 'api', 'rate_limit'], capture_output=True, text=True, timeout=30)
        res = json.loads(r.stdout)['resources']
        g = res.get('graphql') or {}
        return float(g['reset']) if 'reset' in g else None
    except Exception:
        return None


def record(q, rl, prog=None, log=None):
    cost = int((rl or {}).get('cost') or 0)
    k = shape(q)
    TALLY['calls'] += 1
    TALLY['points'] += cost
    TALLY['remaining'] = (rl or {}).get('remaining', TALLY['remaining'])
    TALLY['reset'] = _epoch((rl or {}).get('resetAt')) or TALLY['reset']
    c = TALLY['by'].setdefault(k, [0, 0])
    c[0] += 1
    c[1] += cost
    try:
        path = log or LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as f:
            f.write('\t'.join([datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                               prog or os.path.basename(sys.argv[0] or 'python'), k, str(cost),
                               str((rl or {}).get('remaining', '')), str((rl or {}).get('resetAt', '')),
                               str(os.getpid()), os.environ.get(PASS_ENV, '')]) + '\n')
    except OSError:
        pass


def call(sh, q, variables):
    """Run one GraphQL query through sh (hr-gate's sh: gh api graphql). Returns data, with
    rateLimit taken out and counted. Raises RateLimited on a quota error."""
    args = ['gh', 'api', 'graphql', '-f', f'query={inject(q)}']
    for k, val in variables.items():
        args += ['-F', f'{k}=null' if val is None else f'{k}={val}']
    r = sh(*args, check=False)
    if r.returncode:
        err = (r.stderr or '') + (r.stdout or '')
        if is_rate_limit(err):
            reset = reset_time()
            raise RateLimited(f"GraphQL rate limit: {err.strip()[:160]}; resets at {when(reset)}", reset)
        raise RuntimeError(f"gh api graphql...: {err.strip()[:200]}")
    data = json.loads(r.stdout)['data']
    rl = data.pop('rateLimit', None) if isinstance(data, dict) else None
    record(q, rl)
    return data


def since_mark(mark):
    """(calls, points) since mark = dict(TALLY) taken earlier."""
    return TALLY['calls'] - mark['calls'], TALLY['points'] - mark['points']


def summary(lines, since=None, by_pass=False):
    """{program (or pass id): {shape: [calls, points]}} from log lines newer than since."""
    out = {}
    for line in lines:
        f = line.split('\t')
        if len(f) < 4:
            continue
        t = _epoch(f[0])
        if since and (t or 0) < since:
            continue
        key = (f[7] if len(f) > 7 else '') if by_pass else f[1]
        if by_pass and not key:
            continue
        c = out.setdefault(key, {}).setdefault(f[2], [0, 0])
        c[0] += 1
        c[1] += int(f[3] or 0)
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='python -m tangentswarm.gqlmeter', description=__doc__.split('\n')[0])
    ap.add_argument('--since', type=float, default=60, help='minutes back (default 60)')
    ap.add_argument('--passes', action='store_true', help='per cascade pass (with its hr-gate children)')
    a = ap.parse_args(argv)
    try:
        lines = LOG.read_text().splitlines()
    except OSError:
        lines = []
    s = summary(lines, time.time() - a.since * 60, by_pass=a.passes)
    total = 0
    for prog, by in sorted(s.items(), key=lambda kv: -sum(v[1] for v in kv[1].values())):
        pts = sum(v[1] for v in by.values())
        total += pts
        print(f"{prog}: {pts} points in {sum(v[0] for v in by.values())} queries")
        for k, (n, p) in sorted(by.items(), key=lambda kv: -kv[1][1]):
            print(f"  {p:6} pts {n:5} x {k}")
    print(f"total: {total} points in the last {a.since:g} min")
    return 0


if __name__ == '__main__':
    sys.exit(main())
