"""One batched Project 4 board query, shared by every reader (Michal 2026-11-14 8:50 PM ET).

Before, each reader scanned the whole board (~330 items, 4 pages of 100) with its own query:
promoted.read_board 48 points, tick.board_prs 48, slots 8, hr-gate list_board_items 4,
flow-status 13. One dry cascade pass used 108 points, and the swarm-promoted daemon scanned
the board every 30 s (about 48 points a poll, 5760 an hour: more than the 5000-point
quota of the minbot App installation).

Now one query reads only the OPEN items, filtered on the server (items(query:"is:pr
is:open") and "is:issue is:open"), with small connection sizes. The answer (the snapshot)
is kept in PATH and reused by every process while it is younger than MAX_AGE seconds:

  promoted.read_board    columns of open PRs, fast-track candidates
  Flow.columns           tick.board_prs shape (the cascade)
  slots.backlog_issues   Backlog issues without an open PR (the launch picker)
  flow-status            open PRs with their status-change timeline
  hr-gate                list_board_items rows

hr-gate move() calls invalidate(), so a reader after a card move reads the board again.
A move made outside the swarm (by hand) shows within MAX_AGE seconds.
Per-PR details are cached per head SHA and updatedAt (detail()), so an unchanged PR is not
read again.
"""
import json
import os
import time
from pathlib import Path

from . import board

PATH = Path(os.environ.get('SWARM_BOARD_SNAPSHOT', str(Path.home() / '.cache' / 'swarm-board-snapshot.json')))
DETAILS = Path(os.environ.get('SWARM_BOARD_DETAILS', str(Path.home() / '.cache' / 'swarm-pr-details.json')))
MAX_AGE = float(os.environ.get('SWARM_BOARD_MAX_AGE', '60'))
REPO = 'tangentcode/platform'

STATUS = '{...on ProjectV2ItemFieldSingleSelectValue{name optionId}}'
PR_FIELDS = """number state title isDraft headRefName headRefOid updatedAt body repository{nameWithOwner}
 milestone{title dueOn} labels(first:15){nodes{name}}
 closingIssuesReferences(first:5){nodes{number repository{nameWithOwner} milestone{title dueOn} labels(first:10){nodes{name}}}}
 timelineItems(last:30,itemTypes:[PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]){nodes{__typename
   ...on ProjectV2ItemStatusChangedEvent{createdAt status}}}"""
ISSUE_FIELDS = 'number title state repository{nameWithOwner} milestone{title dueOn} labels(first:10){nodes{name}}'


def _items(alias, filt, fields, kind, first, cursor_var):
    return (f'{alias}: items(first:{first}, after:${cursor_var}, query:"{filt}"){{pageInfo{{hasNextPage endCursor}}'
            f' nodes{{id fieldValueByName(name:"Status"){STATUS} content{{__typename ...on {kind}{{{fields}}}}}}}}}')


PRS = ('prs', 'is:pr is:open', PR_FIELDS, 'PullRequest', 60, 'pc')
ISSUES = ('issues', 'is:issue is:open', ISSUE_FIELDS, 'Issue', 100, 'ic')


def query(*parts):
    """One request for the given parts (PRS, ISSUES); each has its own cursor variable."""
    vars_ = ','.join(f'${p[5]}:String' for p in parts)
    body = ' '.join(_items(*p) for p in parts)
    return f'query({vars_}){{organization(login:"tangentcode"){{projectV2(number:4){{{body}}}}}}}'


def fetch(gql):
    """{'prs': [node], 'issues': [node]}: every open item, both kinds in one request (more
    requests only when a kind has more than one page)."""
    out = {'prs': [], 'issues': []}
    todo = {'prs': (PRS, None), 'issues': (ISSUES, None)}
    while todo:
        parts = [p for p, _ in todo.values()]
        data = gql(query(*parts), **{p[5]: c for p, c in todo.values()})['organization']['projectV2']
        nxt = {}
        for p in parts:
            page = data[p[0]]
            out[p[0]] += page['nodes']
            if page['pageInfo']['hasNextPage']:
                nxt[p[0]] = (p, page['pageInfo']['endCursor'])
        todo = nxt
    return out


def snapshot(gql, max_age=None, path=None, now=time.time):
    """The board snapshot: from PATH when younger than max_age seconds, else read now."""
    path = path or PATH
    max_age = MAX_AGE if max_age is None else max_age
    try:
        d = json.loads(path.read_text())
        if max_age > 0 and now() - float(d['at']) < max_age:
            return d
    except (OSError, ValueError, KeyError, TypeError):
        pass
    d = dict(fetch(gql), at=now())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}')
        tmp.write_text(json.dumps(d))
        os.replace(tmp, path)
    except OSError:
        pass
    return d


def invalidate(path=None):
    try:
        (path or PATH).unlink()
    except OSError:
        pass


# -- views ------------------------------------------------------------------------------

def _names(node):
    return [l['name'] for l in ((node or {}).get('labels') or {}).get('nodes') or []]


def open_prs(snap):
    """[(item node, PR content)] for the open platform PRs."""
    return [(it, it['content']) for it in snap['prs']
            if (it.get('content') or {}).get('__typename') == 'PullRequest'
            and (it['content'].get('repository') or {}).get('nameWithOwner') == REPO
            and it['content'].get('state') == 'OPEN']


def open_issues(snap):
    return [(it, it['content']) for it in snap['issues']
            if (it.get('content') or {}).get('__typename') == 'Issue'
            and (it['content'].get('repository') or {}).get('nameWithOwner') == REPO
            and it['content'].get('state') == 'OPEN']


def col(it):
    return board.column(it.get('fieldValueByName'))


def pr_due(c):
    """(due, milestone title) of a PR: the soonest dated milestone of the PR and the issues it
    closes (board.soonest_due)."""
    return board.soonest_due([c.get('milestone') or {}] + [i.get('milestone') or {} for i in closes(c)])


def pr_labels(c):
    """The labels of a PR and of the issues it closes in REPO (tangentswarm #65: a fast-track
    label on either makes the PR fast-track in board.pick_key)."""
    return _names(c) + [l for i in closes(c) for l in _names(i)]


def closes(c):
    """The issues a PR closes in REPO. A cross-repo "Fixes o/r#17" is another repo's issue 17,
    never this repo's (Codex on PR #26); a node without its repository is kept."""
    return [i for i in ((c.get('closingIssuesReferences') or {}).get('nodes')) or []
            if (i.get('repository') or {}).get('nameWithOwner', REPO) == REPO]


def item_rows(snap):
    """hr-gate list_board_items rows {status, number, state, typename} (open items only;
    hr-gate keeps only OPEN PRs from them). status: hr-gate's internal name."""
    rows = []
    for it, c in open_prs(snap) + open_issues(snap):
        name = col(it)
        rows.append({'status': 'Pending' if name == board.STAGING else name, 'number': c['number'],
                     'state': c['state'], 'typename': c['__typename']})
    return rows


# -- per-PR details, per head SHA and updatedAt ------------------------------------------------

def _details(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def detail_get(n, head, updated, path=None):
    """The kept value for PR n at this head SHA and updatedAt, or None."""
    if not (head and updated):
        return None
    return _details(path or DETAILS).get(f"{n}:{head}:{updated}")


def detail_put(n, head, updated, value, path=None, keep=500):
    if not (head and updated):
        return
    path = path or DETAILS
    d = {k: x for k, x in _details(path).items() if not k.startswith(f"{n}:")}
    d[f"{n}:{head}:{updated}"] = value
    while len(d) > keep:
        del d[next(iter(d))]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}')
        tmp.write_text(json.dumps(d))
        os.replace(tmp, path)
    except OSError:
        pass


def detail(n, head, updated, fn, path=None):
    """fn() -> a JSON value about PR n, kept while its head SHA and updatedAt are the same
    (from the snapshot). A new push, comment, review or label changes updatedAt."""
    hit = detail_get(n, head, updated, path)
    if hit is not None:
        return hit
    v = fn()
    detail_put(n, head, updated, v, path)
    return v


def keys(snap):
    """{PR number: (head SHA, updatedAt)} for the open PRs in the snapshot."""
    return {c['number']: (c.get('headRefOid'), c.get('updatedAt')) for _, c in open_prs(snap)}
