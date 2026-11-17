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

tangentswarm #116 (Michal 2026-11-17): the snapshot is a cache that the events keep current.
swarm-webhook patches it from every delivery (apply_event: a Status move, a merge or close,
a new head, labels, draft state); board.move_card patches it after each swarm move (moved).
So a mover decides from the cache and acts at once. A reader reads the board again only
when the cache is dirty (an event it cannot follow, such as a new card or hr-gate's own
move through invalidate) or older than SYNC_EVERY (30 min): the sync. The sync logs every
difference it finds (SYNC_LOG and the events DB): those are events the cache missed.
Per-PR details are cached per head SHA and updatedAt (detail()), so an unchanged PR is not
read again.
"""
import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from . import board

PATH = Path(os.environ.get('SWARM_BOARD_SNAPSHOT', str(Path.home() / '.cache' / 'swarm-board-snapshot.json')))
DETAILS = Path(os.environ.get('SWARM_BOARD_DETAILS', str(Path.home() / '.cache' / 'swarm-pr-details.json')))
# tangentswarm #116: events keep the cache current (apply_event, moved), so a reader uses it
# for up to the sync period; the sync reads the board again and logs what the events missed.
SYNC_EVERY = float(os.environ.get('SWARM_BOARD_SYNC', '1800'))
MAX_AGE = float(os.environ.get('SWARM_BOARD_MAX_AGE', str(SYNC_EVERY)))
SYNC_LOG = Path(os.environ.get('SWARM_BOARD_SYNC_LOG',
                               str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'board-sync.log')))
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


def _load(path):
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) and 'prs' in d and 'issues' in d else None
    except (OSError, ValueError):
        return None


def _write(path, d):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}')
        tmp.write_text(json.dumps(d))
        os.replace(tmp, path)
    except OSError:
        pass


@contextlib.contextmanager
def _locked(path):
    """One writer at a time (the webhook's patches, a sync, a card move in another process)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path.with_name(f'.{path.name}.lock'), 'a')
    except OSError:
        yield
        return
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def snapshot(gql, max_age=None, path=None, now=time.time):
    """The board snapshot. tangentswarm #116: the cache is kept current by events (patch,
    apply_event, moved), so it is used while it is younger than max_age seconds (default
    MAX_AGE, the sync period) and not marked dirty. Else the board is read again (a sync):
    patches made while the read ran are applied on top of it, and every difference from the
    cache is logged (log_sync): those are the events the cache missed."""
    path = path or PATH
    max_age = MAX_AGE if max_age is None else max_age
    d = _load(path)
    try:
        if d is not None and not d.get('dirty') and max_age > 0 and now() - float(d['at']) < max_age:
            return d
    except (KeyError, TypeError, ValueError):
        pass
    why = ('first read' if d is None else 'dirty' if d.get('dirty') else
           'read now' if max_age <= 0 else 'sync')
    t0 = now()
    fresh = dict(fetch(gql), at=t0)
    with _locked(path):
        old = _load(path)
        late = [p for p in (old or {}).get('patches') or [] if float(p.get('t') or 0) >= t0]
        for p in late:  # an event that arrived while we read: it may be newer than the read
            _apply(fresh, p)
        fresh['patches'] = late
        fresh['gone'] = (old or {}).get('gone') or []
        if old and old.get('dirty') and float(old['dirty']) >= t0:
            fresh['dirty'] = old['dirty']
        missed = diff(old, fresh) if old else []
        _write(path, fresh)
    if missed:
        log_sync(why, missed, now())
    return fresh


def invalidate(path=None):
    """Read the board again at the next snapshot() (a move made outside board.move_card, such
    as hr-gate's own move). The patches stay: a read that is running keeps them."""
    if _hold.depth:  # board.move_card patches the cache itself (moved)
        return
    patch({'op': 'dirty'}, path)


class _Hold(threading.local):
    depth = 0


_hold = _Hold()


@contextlib.contextmanager
def hold():
    """invalidate() does nothing inside (board._move: hr-gate's move() calls it, then we patch)."""
    _hold.depth += 1
    try:
        yield
    finally:
        _hold.depth -= 1


# -- patches: the cache follows the events (tangentswarm #116) ------------------------------

KEEP_PATCHES = 200


def _content(d, kind, n):
    key = 'prs' if kind == 'PullRequest' else 'issues'
    for it in d.get(key) or []:
        c = it.get('content') or {}
        if c.get('number') == n and (c.get('repository') or {}).get('nameWithOwner', REPO) == REPO:
            yield it, c


def _forget(d, items):
    """Keep the ids of items that left the open board: a later move of one is not unknown."""
    d['gone'] = ((d.get('gone') or []) + [i for i in items if i])[-KEEP_PATCHES:]


def _apply(d, op):
    """Apply one patch to snapshot d. False: its target is not in the cache (read again)."""
    kind = op.get('op')
    if kind == 'col':
        hit = False
        for it in (d.get('prs') or []) + (d.get('issues') or []):
            if it.get('id') == op['item']:
                it['fieldValueByName'] = ({'name': op.get('name'), 'optionId': op.get('optionId')}
                                          if op.get('name') or op.get('optionId') else None)
                hit = True
        return hit or op['item'] in (d.get('gone') or [])  # a move of a card that just closed
    if kind == 'gone':
        key = 'prs' if op.get('kind', 'PullRequest') == 'PullRequest' else 'issues'
        drop = [it for it, _ in _content(d, op.get('kind', 'PullRequest'), op['number'])]
        d[key] = [it for it in d.get(key) or [] if not any(it is x for x in drop)]
        _forget(d, [it.get('id') for it in drop])
        return True
    if kind == 'item-gone':
        for key in ('prs', 'issues'):
            d[key] = [it for it in d.get(key) or [] if it.get('id') != op['item']]
        _forget(d, [op['item']])
        return True
    if kind == 'pr':
        for _, c in _content(d, 'PullRequest', op['number']):
            c.update(op.get('set') or {})
        return True  # a PR that is not in the cache: its own card event brings it
    return False  # 'dirty'


def patch(op, path=None, now=time.time):
    """Apply op to the kept snapshot under its lock. Returns True if the cache follows it;
    False marks the cache dirty (the next snapshot() reads the board). No cache: nothing."""
    path = path or PATH
    with _locked(path):
        d = _load(path)
        if d is None:
            return False
        op = dict(op, t=now())
        ok = _apply(d, op)
        if not ok:
            d['dirty'] = op['t']
        d['patches'] = ((d.get('patches') or []) + [op])[-KEEP_PATCHES:]
        _write(path, d)
    return ok


def moved(item, col, path=None, now=time.time):
    """A card move the swarm made: the cache shows it at once (no board read)."""
    return patch({'op': 'col', 'item': item, 'name': col, 'optionId': board.OPTION.get(col)}, path, now)


def _labels(names):
    return {'nodes': [{'name': x} for x in names]}


def ops_for(event, p):
    """The patches for one webhook delivery (tangentswarm #116). [] when it changes nothing
    the cache holds. {'op': 'dirty'} when the cache cannot follow it (a new item, a new
    milestone): the next read takes the board again."""
    p = p or {}
    action = p.get('action') or ''
    if event == 'projects_v2_item':
        item = p.get('projects_v2_item') or {}
        if item.get('project_node_id') != board.DEFAULT.id:
            return []
        if action == 'edited':
            ch = (p.get('changes') or {}).get('field_value') or {}
            if not ch or (ch.get('field_node_id') and ch.get('field_node_id') != board.DEFAULT.status_field):
                return []
            to = ch.get('to') or {}
            return [{'op': 'col', 'item': item.get('node_id'), 'name': board.column(
                {'optionId': to.get('id'), 'name': to.get('name')}), 'optionId': to.get('id')}]
        if action in ('deleted', 'archived'):
            return [{'op': 'item-gone', 'item': item.get('node_id')}]
        if action in ('created', 'restored', 'converted'):
            return [{'op': 'dirty'}]
        return []
    if ((p.get('repository') or {}).get('full_name') or '') != REPO:
        return []
    if event == 'pull_request':
        pr = p.get('pull_request') or {}
        n, upd = pr.get('number'), pr.get('updated_at')
        if not n:
            return []
        if action == 'closed':
            return [{'op': 'gone', 'kind': 'PullRequest', 'number': n}]
        if action in ('opened', 'reopened', 'milestoned', 'demilestoned'):
            return [{'op': 'dirty'}]
        put = {'updatedAt': upd} if upd else {}
        if action == 'synchronize':
            put['headRefOid'] = (pr.get('head') or {}).get('sha')
        elif action in ('converted_to_draft', 'ready_for_review'):
            put['isDraft'] = action == 'converted_to_draft'
        elif action in ('labeled', 'unlabeled'):
            put['labels'] = _labels([x.get('name') for x in pr.get('labels') or [] if x.get('name')])
        elif action == 'edited':
            put.update(body=pr.get('body') or '', title=pr.get('title') or '')
            ch = p.get('changes')
            if not isinstance(ch, dict) or set(ch) - {'title'}:
                # Codex on PR #117: a body edit can add or drop "Fixes #N", and the cache cannot
                # derive closingIssuesReferences (and its labels and milestones) from the body.
                # A base change is not in the cache either. Only a title-only edit is followed.
                return [{'op': 'pr', 'number': n, 'set': put}, {'op': 'dirty'}]
        return [{'op': 'pr', 'number': n, 'set': put}] if put else []
    if event == 'issues':
        i = p.get('issue') or {}
        if action == 'closed':
            return [{'op': 'gone', 'kind': 'Issue', 'number': i.get('number')}]
        if action in ('opened', 'reopened', 'transferred', 'milestoned', 'demilestoned', 'labeled', 'unlabeled'):
            return [{'op': 'dirty'}]  # an issue's labels and milestone change a PR's pick order
        return []
    if event == 'issue_comment':
        i = p.get('issue') or {}
        if 'pull_request' in i and i.get('state') == 'open' and i.get('updated_at'):
            return [{'op': 'pr', 'number': i.get('number'), 'set': {'updatedAt': i['updated_at']}}]
        return []
    if event == 'pull_request_review':
        pr = p.get('pull_request') or {}
        if pr.get('updated_at'):
            return [{'op': 'pr', 'number': pr.get('number'), 'set': {'updatedAt': pr['updated_at']}}]
    return []


def apply_event(event, payload, path=None, now=time.time):
    """Patch the cache from one webhook delivery. Returns the ops applied. Never raises."""
    try:
        ops = ops_for(event, payload)
        for op in ops:
            patch(op, path, now)
        return ops
    except Exception:
        return []


# -- the sync: what the cache missed ---------------------------------------------------------

def _rows(d):
    """{item id: (kind, number, column, head, draft, labels)} of a snapshot."""
    out = {}
    for key in ('prs', 'issues'):
        for it in d.get(key) or []:
            c = it.get('content') or {}
            if (c.get('repository') or {}).get('nameWithOwner', REPO) != REPO or c.get('state', 'OPEN') != 'OPEN':
                continue
            out[it.get('id')] = (c.get('__typename'), c.get('number'), col(it), c.get('headRefOid'),
                                 bool(c.get('isDraft')), tuple(sorted(_names(c))))
    return out


def diff(old, new):
    """Lines that say how the cache (old) differs from the board just read (new)."""
    a, b = _rows(old or {}), _rows(new or {})
    out = []
    for item in sorted(set(a) | set(b), key=lambda i: ((a.get(i) or b.get(i))[1] or 0, str(i))):
        x, y = a.get(item), b.get(item)
        if x is None:
            out.append(f"#{y[1]} is on the board in {y[2]}, not in the cache")
            continue
        if y is None:
            out.append(f"#{x[1]} ({x[2]}) left the open board")
            continue
        n = x[1]
        if x[2] != y[2]:
            out.append(f"#{n} column {x[2]} -> {y[2]}")
        if x[3] != y[3]:
            out.append(f"#{n} head {(x[3] or '-')[:8]} -> {(y[3] or '-')[:8]}")
        if x[4] != y[4]:
            out.append(f"#{n} draft {x[4]} -> {y[4]}")
        if x[5] != y[5]:
            out.append(f"#{n} labels {', '.join(x[5]) or '-'} -> {', '.join(y[5]) or '-'}")
    return out


def log_sync(why, lines, now=None):
    """One line per difference in SYNC_LOG and one events-DB row. Never raises."""
    now = time.time() if now is None else now
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now))
    try:
        SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SYNC_LOG, 'a') as f:
            for line in lines:
                f.write(f"{stamp}\t{why}\t{line}\n")
    except OSError:
        pass
    try:
        from . import eventdb
        eventdb.log_event('board-sync', source='board', action=why, notes=list(lines)[:50],
                          detail={'missed': len(lines)})
    except Exception:
        pass


def sync_due(path=None, now=time.time, every=None):
    """True when the kept snapshot is older than the sync period (or dirty, or missing)."""
    every = SYNC_EVERY if every is None else every
    d = _load(path or PATH)
    try:
        return d is None or bool(d.get('dirty')) or now() - float(d['at']) >= every
    except (KeyError, TypeError, ValueError):
        return True


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


def milestones(c):
    """The milestone titles of a PR and of the issues it closes (tangentswarm #116: they pick
    the PR's priority staging site, gatechecks.priority_site)."""
    out = [(m or {}).get('title') for m in [c.get('milestone')] + [i.get('milestone') for i in closes(c)]]
    return [t for t in out if t]


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
