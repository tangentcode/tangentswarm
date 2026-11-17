"""The swarm's project boards (Project 4, and Project 6: #45) and their Status columns, matched
by option id, never by name.

The board renames columns (Pending is now shown as Staging). Option ids stay the same,
so every reader maps a status value's optionId to the stable internal key below.
"""
import json
import os
import re
from pathlib import Path

from . import config as _config

PROJECT_ID = _config.get('project', 'id')  # private: ~/.config/tangentswarm/config.toml
STATUS_FIELD = _config.get('project', 'status_field')

# internal key -> option id
INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE = 'Inbox', 'Triage', 'Discuss', 'Backlog', 'Active'
IN_PROGRESS, AUTO_REVIEW, NEEDS_INPUT = 'In Progress', 'Auto Review', 'Needs Input'
READY, STAGING, HUMAN_REVIEW = 'Ready', 'Pending', 'Human Review'  # STAGING keeps the internal key "Pending"
DONE, CANCELED, PARKED = 'Done', 'Canceled', 'Parked'

COLUMNS = (INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE, IN_PROGRESS, AUTO_REVIEW, NEEDS_INPUT, READY, STAGING,
           HUMAN_REVIEW, DONE, CANCELED, PARKED)
OPTION = {c: _config.get('project', 'options', c) for c in COLUMNS}


class Board:
    """One project board the swarm runs (tangentswarm #45, Michal 2026-11-15 7:20 PM ET: the
    same flow on Project 4 with platform and on Project 6 with tangentswarm and
    methodology). Its node ids and option ids are private config."""

    def __init__(self, number, id, status_field, option, repos, staging):
        self.number, self.id, self.status_field = number, id, status_field
        self.option, self.repos, self.staging = dict(option), tuple(repos), bool(staging)

    def __repr__(self):
        return f"Board({self.number}, {', '.join(self.repos)})"


DEFAULT = Board(int(_config.get('project', 'number', default=4)), PROJECT_ID, STATUS_FIELD, OPTION,
                _config.get('project', 'repos', default=['tangentcode/platform']),
                _config.get('project', 'staging', default=True))
BOARDS = [DEFAULT] + [Board(int(b['number']), b['id'], b['status_field'],
                            {c: (b.get('options') or {}).get(c) for c in COLUMNS}, b.get('repos') or [],
                            b.get('staging', False))
                      for b in _config.get('boards', default=[]) or []]
REPOS = tuple(r for b in BOARDS for r in b.repos)
BY_ID = {v: k for b in BOARDS for k, v in b.option.items() if v}  # option ids are unique per board


def for_repo(repo):
    """The board whose repos include repo, or None (a repo the swarm does not run)."""
    return next((b for b in BOARDS if repo in b.repos), None)


def for_project(node_id):
    """The board with this project node id, or None."""
    return next((b for b in BOARDS if node_id and b.id == node_id), None)

# Timeline status-change events carry the column name as it was at the time.
ALIASES = {STAGING: {'Approved', 'Staging'}}


def column(value):
    """Internal key for a Status field value {name, optionId}; the name only if the id is unknown."""
    value = value or {}
    return BY_ID.get(value.get('optionId')) or value.get('name')


def event_names(*keys):
    """Every name a timeline event may use for these columns."""
    return set(keys).union(*(ALIASES.get(k, set()) for k in keys))


# Ordering (methodology#60): keep the flow full first; within that, a fast-track PR jumps
# the queue at every step. A PR is fast-track when it, or an issue it closes, has one of
# these labels.
FAST_TRACK_RE = re.compile(r'^\s*(fast[- _]?track|high[- _]?priority|priority[: /_-]*high)\s*$', re.I)
FAST_TRACK, MILESTONE, NORMAL = -1, 0, 1


def is_fast_track(labels):
    return any(FAST_TRACK_RE.match(l or '') for l in labels)


def priority_rank(fast_track, priority_milestone):
    """-1 fast-track, 0 priority milestone, 1 the rest. Lower goes first."""
    return FAST_TRACK if fast_track else (MILESTONE if priority_milestone else NORMAL)


# Only ONE fast-track ticket is active at a time (methodology PR #63). Several tickets may
# carry the label; the active one keeps the fast-track rank until it enters Human Review,
# is done or canceled, or loses the label. Then the candidate furthest along the board
# (lowest number on a tie) becomes active. A ticket is an issue or a PR: a PR counts as
# the issues it closes and itself. The swarm-promoted daemon keeps FT_STATE current; the
# other readers (swarm-tick, swarm-flow, hr-gate) read it with active_fast_track().
FT_STATE = Path(os.environ.get('SWARM_FASTTRACK_STATE',
                               str(Path.home() / '.local' / 'state' / 'swarm-fasttrack.json')))
FT_STAGES = (INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE, IN_PROGRESS, NEEDS_INPUT, AUTO_REVIEW, READY, STAGING)


def ft_candidates(items):
    """{ticket number: stage index} for fast-track tickets that may be active.
    items: [{number, kind ('Issue'|'PullRequest'), state, col, labels, closes: [{number, labels}]}]."""
    out = {}
    # Codex on PR #26: an issue card stays in Active while its PR moves on, so the issue was
    # still a candidate after its PR reached Human Review. A ticket whose PR is past Staging
    # (Human Review) is released, with the issues that PR closes.
    released = set()
    for it in items:
        if it.get('state') == 'OPEN' and it.get('kind') == 'PullRequest' and it.get('col') == HUMAN_REVIEW:
            released.add(it['number'])
            released.update(c['number'] for c in it.get('closes') or ())
    for it in items:
        if it.get('state') != 'OPEN' or it.get('col') not in FT_STAGES:
            continue
        stage = FT_STAGES.index(it['col'])
        tickets = [it['number']] if is_fast_track(it.get('labels') or ()) else []
        tickets += [c['number'] for c in it.get('closes') or () if is_fast_track(c.get('labels') or ())]
        for t in tickets:
            if t not in released:
                out[t] = max(out.get(t, -1), stage)
    return out


def pick_active(cands, prev):
    """prev while it is still a candidate; else the furthest stage, then the lowest number."""
    if prev in cands:
        return prev
    if not cands:
        return None
    return min(cands, key=lambda t: (-cands[t], t))


def active_fast_track(path=None):
    """The active fast-track ticket number from FT_STATE, or None."""
    try:
        v = json.loads((path or FT_STATE).read_text()).get('active')
        return int(v) if v is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def update_active(items, path=None):
    """Recompute and save the active fast-track ticket. Returns (active, previous)."""
    path = path or FT_STATE
    prev = active_fast_track(path)
    cands = ft_candidates(items)
    act = pick_active(cands, prev)
    if act != prev or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'active': act, 'candidates': sorted(cands)}))
        os.replace(tmp, path)
    return act, prev


def pick_key(item, active=None):
    """The ONE pick order of the swarm (tangentswarm #35, Michal 2026-11-15 6:09 PM ET): every
    picker (launch, fill Ready -> Staging, promote Staging -> Human Review, the MinBot check,
    swarm-tick) sorts by this key. A pure function of {number, ft, labels, closes, due,
    milestone}:
      (1) fast-track first, by the lowest number (tangentswarm #65, Michal 2026-11-16 3:17 PM
          ET): item_fast_track (a fast-track label on it or an issue it closes, or the
          active fast-track ticket);
      (2) then the milestone due date (YYYY-MM-DD), soonest first; a milestone without a due
          date after every dated one; no milestone last;
      (3) then the lowest number."""
    if item_fast_track(item, active):  # Codex on PR #66: fast-track by the number alone
        return (0, 0, '', item['number'])
    due = (item.get('due') or '')[:10]
    ms = 0 if due else 1 if item.get('milestone') else 2
    return (1, ms, due, item['number'])


def item_fast_track(item, active=None):
    """The ONE fast-track test of every picker (tangentswarm #65): item['ft'] (a builder set
    it from the PR's labels and its closing issues' labels), rank FAST_TRACK, a label that
    FAST_TRACK_RE matches on the item (item['labels']) or on an issue it closes (closes as
    {number, labels} dicts), or the active fast-track ticket (this item or an issue it closes)."""
    closes = item.get('closes') or ()
    nums = [c.get('number') if isinstance(c, dict) else c for c in closes]
    names = list(item.get('labels') or ())
    names += [l for c in closes if isinstance(c, dict) for l in c.get('labels') or ()]
    return bool(item.get('ft') or item.get('rank') == FAST_TRACK or is_fast_track(str(l) for l in names)
                or is_active_fast_track(item.get('number'), nums, active))


def soonest_due(milestones):
    """(due YYYY-MM-DD or '', title or '') of the soonest dated milestone in [{title, dueOn}],
    else the first undated title. A PR's milestones are its own and its closing issues'."""
    dated = sorted(((m.get('dueOn') or '')[:10], m.get('title') or '') for m in milestones if m and m.get('dueOn'))
    if dated:
        return dated[0]
    titles = [m.get('title') for m in milestones if m and m.get('title')]
    return '', (titles[0] if titles else '')


def is_active_fast_track(number, closes=(), active=None):
    """True if PR/issue `number`, or an issue it closes, is the active fast-track ticket."""
    return active is not None and (number == active or active in set(closes))


HR_GATE_COLS = {IN_PROGRESS, AUTO_REVIEW, STAGING, HUMAN_REVIEW, DONE, BACKLOG}  # hr-gate's OPT names


def move_card(g, item, col, sh=None, n=None, b=None, repo=None, kind='pr'):
    """Move a card to column col, by option id. hr-gate's move() knows only its own
    OPT names (no Ready, Needs Input, Triage...): platform #537/#540/#542/#543 at 11:03 AM ET
    2026-11-15 got their MinBot pass comment, then KeyError 'Ready', and stayed in Auto
    Review. Every mover goes through here. b: the card's board (default: Project 4, the
    only one hr-gate knows).

    tangentswarm #95: In Progress PRs are drafts. A move of PR n into In Progress also
    converts the PR to a draft (make_draft). repo: the PR's repo (default: the board's
    only repo). kind: 'issue' for an issue card, which has no draft state."""
    from . import eventdb
    try:
        _move(g, item, col, sh, b)
    except Exception as e:  # Codex on PR #33: the row says what happened, not what was tried
        eventdb.log_event('move', source='board', action=col, pr=n, actor=eventdb.SOURCE or None,
                          detail={'item': item, 'col': col, 'board': (b or DEFAULT).number},
                          error=f'{type(e).__name__}: {str(e)[:300]}')
        raise
    eventdb.log_event('move', source='board', action=col, pr=n, actor=eventdb.SOURCE or None,
                      detail={'item': item, 'col': col, 'board': (b or DEFAULT).number})  # tangentswarm #32
    if col == IN_PROGRESS and n is not None and kind == 'pr':
        make_draft(n, repo or _only_repo(b), sh)


def _only_repo(b):
    repos = (b or DEFAULT).repos
    return repos[0] if len(repos) == 1 else None


def make_draft(n, repo, sh=None):
    """Convert PR n of repo to a draft (tangentswarm #95: the pull sent platform #447 from
    Staging to In Progress and left it ready for review; the sanity check flagged it).
    gh does nothing for a PR that is a draft already. Never raises: the card has moved,
    and a failed conversion is a logged event that the sanity check reports. Returns
    True if the gh call and the label removal ran without an error.

    tangentswarm #134: a draft is work in progress, never merge-ready. The same step
    removes merge-ready and pre-merging (platform #528 was rejected into In Progress,
    became a draft and kept merge-ready). It is done here so no caller can forget it,
    and also when the gh call fails: the card is in In Progress either way."""
    ok = _draft(n, repo, sh)
    if repo:
        ok = _drop_premerge_labels(n, repo) and ok
    return ok


def _drop_premerge_labels(n, repo):
    from . import eventdb, flow
    try:
        errs = flow.drop_premerge_labels(n, repo)
    except Exception as e:
        errs = [f'{type(e).__name__}: {str(e)[:300]}']
    if errs:
        eventdb.log_event('draft', source='board', action='drop-labels', pr=n, actor=eventdb.SOURCE or None,
                          repo=repo, error='; '.join(errs)[:300])
        return False
    return True


def _draft(n, repo, sh=None):
    from . import eventdb
    if not repo:
        eventdb.log_event('draft', source='board', action='undo-ready', pr=n, actor=eventdb.SOURCE or None,
                          error='no repo for the PR: the board has more than one repo')
        return False
    argv = ['gh', 'pr', 'ready', str(n), '-R', repo, '--undo']
    try:
        if sh:
            r = sh(argv)
            rc = getattr(r, 'returncode', 0)
            if rc:
                raise RuntimeError(f"rc={rc}: {(getattr(r, 'stderr', '') or getattr(r, 'stdout', '') or '').strip()[:200]}")
        else:
            from . import tick
            tick.sh(argv, check=True)
    except Exception as e:
        eventdb.log_event('draft', source='board', action='undo-ready', pr=n, actor=eventdb.SOURCE or None,
                          repo=repo, error=f'{type(e).__name__}: {str(e)[:300]}')
        return False
    eventdb.log_event('draft', source='board', action='undo-ready', pr=n, actor=eventdb.SOURCE or None, repo=repo)
    return True


def _move(g, item, col, sh=None, b=None):
    if b is not None and b is not DEFAULT:
        argv = ['gh', 'project', 'item-edit', '--id', item, '--project-id', b.id,
                '--field-id', b.status_field, '--single-select-option-id', b.option[col]]
        if sh:
            sh(argv)
        else:
            from . import tick
            tick.sh(argv, check=True)
        return
    from . import boardcache
    if 'move' in g and col in (g.get('OPT') or HR_GATE_COLS):
        with boardcache.hold():  # hr-gate's move() calls invalidate(): we patch the cache instead
            g['move'](item, col)
        boardcache.moved(item, col)  # tangentswarm #116: the cache shows the move, no board read
        return
    argv = ['gh', 'project', 'item-edit', '--id', item, '--project-id', g['project_id'](),
            '--field-id', g['STATUS_FIELD'], '--single-select-option-id', OPTION[col]]
    if sh:
        sh(argv)
    else:
        from . import tick
        tick.sh(argv, check=True)
    for name in ('board',):
        if g.get('cache_drop'):
            g['cache_drop'](name)
    if g.get('cache_drop_prefix'):
        g['cache_drop_prefix']('pr:')
    boardcache.moved(item, col)  # tangentswarm #116: the cache shows the move, no board read
