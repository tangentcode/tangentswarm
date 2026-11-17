"""swarm-sanity: the sanity check (tangentswarm #43). Deterministic, no LLM.

Michal (issue #43): "check on board-change events, at server startup, and once an hour as a
backstop. No 5-minute polling." and "name this the sanity check and document exactly what it
checks each hour (the board column rules from workflow.md, plus no two Claudes in one
worktree, plus any later additions). On each finding: tell swarmbot, and open an incident
ticket on the methodology board (Project 6)."

The checks (each one has a rule id; docs/sanity-check.md is the full list):

  no-status              an open card with an empty Status
  unknown-column         a column that workflow.md does not name (one finding per column)
  pr-in-issue-column     an open PR in Inbox, Discuss, Triage, Backlog or Active
  issue-in-pr-column     an open issue in a PR column (In Progress .. Parked, Done, Canceled),
                         except Needs Input
  issue-in-needs-input   an open issue in Needs Input (#82: with no PR it goes in Triage or
                         Discuss; with a PR, the PR goes in Needs Input and the issue in Active)
  issue-not-active       an issue in Inbox, Discuss, Triage or Backlog that an open flow-column
                         PR closes (#82: an issue moves to Active as soon as its PR is made)
  open-pr-in-done        an open PR in Done (Done holds merged PRs)
  open-pr-in-canceled    an open PR in Canceled (Canceled holds closed PRs)
  not-draft              a ready PR in In Progress, Needs Input or Parked (draft PRs only)
  draft                  a draft PR in Auto Review, Ready, Staging or Human Review
  no-live-claude         an In Progress PR with no live Claude in a worktree on its branch
  human-review-over-two  more than two PRs in Human Review
  active-work2           an Active issue with the work2 label
  active-pr-count        an Active issue without exactly one open PR in a flow column
  flow-pr-active-count   a flow-column PR that does not belong to exactly one Active issue
                         (not when an issue rule above already reports its issue card)
  duplicate-claudes      two or more live Claudes in one git worktree
  unit-source            a swarm user unit (or its process) that does not import tangentswarm
                         from flow-live (methodology #84; tangentswarm.unitcheck)

Deferred (#82) is a valid column on Project 4: a holding column for tickets from older
workflow versions. A card in Deferred is never a finding. A PR in Deferred still counts as
the PR of its Active issue (active-pr-count), and an issue in Deferred still owns its PR
(flow-pr-active-count).

Project 4 is read from the shared board snapshot (boardcache), with issues and PRs, so every
rule runs there. The other boards (Project 6) are read with boards.open_prs: open PRs only,
so the issue rules do not run there. The units are read with systemctl --user; when that read
fails, it goes in failed as 'units', like a board that is not read.

A finding is reported only when it is still there SANITY_GRACE s after it was first seen
(a card move and its draft flip, or a Claude start, are not one atomic step). Then one
swarmbot alert per pass for the new findings, and one incident ticket per
pass that lists all its new findings (#82: one report per run, not one per finding;
alerts.incident: methodology, Project 6, at most INCIDENTS_PER_HOUR an hour; a capped or
failed ticket is tried again on the next pass). A finding is told and filed once only. A
finding that goes away is forgotten; if it comes back later, it is new again.

    swarm-sanity            dry run: print every finding and what --apply would do now
    swarm-sanity --apply    file the incident ticket and tell swarmbot
    swarm-sanity --json     the findings as JSON
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import board

GRACE = int(os.environ.get('SANITY_GRACE', '600'))
EVERY = int(os.environ.get('SANITY_EVERY', '3600'))
RETRY = int(os.environ.get('SANITY_RETRY', '600'))
WORK2 = 'work2'

ISSUE_COLS = (board.INBOX, board.DISCUSS, board.TRIAGE, board.BACKLOG, board.ACTIVE)
FLOW_COLS = (board.IN_PROGRESS, board.AUTO_REVIEW, board.NEEDS_INPUT, board.READY, board.STAGING,
             board.HUMAN_REVIEW)
PR_COLS = FLOW_COLS + (board.DONE, board.CANCELED, board.PARKED)
DRAFT_COLS = (board.IN_PROGRESS, board.NEEDS_INPUT, board.PARKED)
READY_COLS = (board.AUTO_REVIEW, board.READY, board.STAGING, board.HUMAN_REVIEW)
HR_MAX = 2
DEFERRED = 'Deferred'  # #82: a valid holding column of Project 4 (no option id in the config)


def valid_extra(board_no):
    """Columns that a board has besides board.COLUMNS (#82: Deferred on Project 4)."""
    return (DEFERRED,) if board_no == board.DEFAULT.number else ()

RULES = {
    'no-status': 'Each card must have a Status. Never leave the Status empty.',
    'unknown-column': 'The board columns are the ones in the Board columns table of workflow.md.',
    'pr-in-issue-column': 'Issues go in Inbox, Discuss, Triage, Backlog and Active; pull requests go in the '
                          'columns from In Progress to Parked.',
    'issue-in-pr-column': 'An issue never goes in a pull-request column. An open issue never goes in Done.',
    'issue-in-needs-input': 'An issue in Needs Input with no PR goes in Triage (a simple question) or Discuss. '
                            'When the issue has a PR, the PR goes in Needs Input and the issue in Active.',
    'issue-not-active': 'An issue moves to Active as soon as its PR is made.',
    'open-pr-in-done': 'Done holds pull requests that are merged to main.',
    'open-pr-in-canceled': 'Canceled holds closed pull requests that will not merge.',
    'not-draft': 'In Progress, Needs Input and Parked hold draft pull requests.',
    'draft': 'Auto Review holds pull requests that are marked ready; a draft does not go past In Progress.',
    'no-live-claude': 'In Progress holds draft pull requests that a worker works on now.',
    'human-review-over-two': 'Human Review holds never more than two pull requests.',
    'active-work2': 'Active holds only issues without the work2 label.',
    'active-pr-count': 'An Active issue has exactly one open linked pull request in a flow column.',
    'flow-pr-active-count': 'A pull request in a flow column belongs to exactly one Active issue.',
    'duplicate-claudes': 'One Claude per worktree.',
    'unit-source': 'Every swarm service imports tangentswarm from the flow-live checkout '
                   '(methodology #84; swarm-unit-check).',
}
UNITS = 'units'  # the failed/unread key of the unit read (the boards use their project number)


def finding(rule, text, repo=None, n=None, scope=None, board_no=None):
    where = scope or (f"{repo}#{n}" if n is not None else f"project {board_no}")
    return dict(key=f"{rule}:{where}", rule=rule, repo=repo, n=n, board=board_no, text=text)


# -- cards ------------------------------------------------------------------------------------

def _labels(node):
    return {l['name'] for l in ((node or {}).get('labels') or {}).get('nodes') or []}


def cards_from_snap(snap, board_no=None):
    """Project 4 cards from the boardcache snapshot: [{kind, repo, n, col, draft, branch,
    labels, closes: [(n, labels)]}] for the open PRs and issues."""
    from . import boardcache
    board_no = board.DEFAULT.number if board_no is None else board_no
    out = []
    for it, c in boardcache.open_prs(snap):
        out.append(dict(board=board_no, kind='pr', repo=boardcache.REPO, n=c['number'], col=boardcache.col(it),
                        draft=bool(c.get('isDraft')), branch=c.get('headRefName'), labels=_labels(c),
                        closes=[(i['number'], _labels(i)) for i in boardcache.closes(c)]))
    for it, c in boardcache.open_issues(snap):
        out.append(dict(board=board_no, kind='issue', repo=boardcache.REPO, n=c['number'], col=boardcache.col(it),
                        draft=False, branch=None, labels=_labels(c), closes=[]))
    return out


def cards_from_board(gql, b):
    """Open PR cards of another board (boards.open_prs: PRs only, no issue cards)."""
    from . import boards
    return [dict(board=b.number, kind='pr', repo=p['repo'], n=p['number'], col=col, draft=bool(p['draft']),
                 branch=p.get('branch'), labels=set(p.get('labels') or ()), closes=None)
            for col, rows in boards.open_prs(gql, b).items() for p in rows]


# -- live Claudes -----------------------------------------------------------------------------

def worktree_root(cwd):
    """The git worktree (or checkout) that holds cwd: the nearest directory with a .git, or None."""
    p = Path((cwd or '').removesuffix(' (deleted)'))
    if not p.is_absolute():
        return None
    for q in (p, *p.parents):
        if (q / '.git').exists():
            return str(q)
    return None


def _text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


GH_RE = re.compile(r'github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$')


def git_info(root):
    """(repo 'owner/name' of origin or None, branch or None) of a worktree, from its files
    (no git process)."""
    dot = Path(root) / '.git'
    gitdir = dot
    if dot.is_file():
        m = re.match(r'gitdir:\s*(.+)', _text(dot))
        if not m:
            return None, None
        gitdir = (Path(root) / m.group(1)).resolve()
    common = gitdir
    cd = _text(gitdir / 'commondir')
    if cd:
        common = (gitdir / cd).resolve()
    head = _text(gitdir / 'HEAD')
    branch = head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else None
    repo, in_origin = None, False
    for line in _text(common / 'config').splitlines():
        s = line.strip()
        if s.startswith('['):
            in_origin = s.replace(' ', '') in ('[remote"origin"]',)
        elif in_origin and s.startswith('url'):
            m = GH_RE.search(s.split('=', 1)[-1].strip())
            repo = m.group(1) if m else None
    return repo, branch


def live_claudes(proc=Path('/proc'), uid=None, info=git_info):
    """[{pid, cwd, root, repo, branch}] of our live Claude processes. A Claude started by
    another Claude (a descendant) is part of that one, not a second Claude."""
    from . import livestate, registry
    table = livestate.procs(proc, uid)
    ids = {pid for pid, p in table.items() if registry.classify_process(p['comm'], p['cmd']) == 'claude'}

    def nested(pid):
        seen, q = set(), table[pid]['ppid']
        while q in table and q not in seen:
            if q in ids:
                return True
            seen.add(q)
            q = table[q]['ppid']
        return False
    out = []
    for pid in sorted(ids):
        if nested(pid):
            continue
        try:
            cwd = os.readlink(Path(proc) / str(pid) / 'cwd')
        except OSError:
            continue
        root = worktree_root(cwd)
        repo, branch = info(root) if root else (None, None)
        out.append(dict(pid=pid, cwd=cwd, root=root, repo=repo, branch=branch))
    return out


# -- the rules --------------------------------------------------------------------------------

def check(cards, claudes, hold_issues=(), hold_prs=()):
    """[finding] for the cards and the live Claudes. hold_issues: {(repo, n)} of issues with
    a launch in flight (Active before its draft PR); hold_prs: {(repo, n)} of PRs whose Claude
    is starting now. Keyed by repo (Codex on PR #79: platform #79 must not hold tangentswarm #79)."""
    out, unknown = [], {}
    for c in cards:
        what = f"{'PR' if c['kind'] == 'pr' else 'issue'} {c['repo']}#{c['n']}"
        col = c['col']

        def add(rule, text):
            out.append(finding(rule, f"{what} {text}", c['repo'], c['n'], board_no=c['board']))
        if not col:
            add('no-status', 'has an empty Status')
            continue
        if col in valid_extra(c['board']):
            continue  # #82: never flag a card in Deferred
        if col not in board.COLUMNS:
            unknown.setdefault((c['board'], col), []).append(f"{c['repo']}#{c['n']}")
            continue
        if c['kind'] == 'issue':
            if col == board.NEEDS_INPUT:
                continue  # issue-in-needs-input, in _active_rules (it needs the PRs)
            if col not in ISSUE_COLS:
                add('issue-in-pr-column', f"is an open issue in {col}")
            continue
        if col in ISSUE_COLS:
            add('pr-in-issue-column', f"is in {col}, an issue column")
        elif col == board.DONE:
            add('open-pr-in-done', 'is open but in Done')
        elif col == board.CANCELED:
            add('open-pr-in-canceled', 'is open but in Canceled')
        elif col in DRAFT_COLS and not c['draft']:
            add('not-draft', f"is in {col} but is not a draft")
        elif col in READY_COLS and c['draft']:
            add('draft', f"is in {col} but is a draft")
    for (b, col), names in sorted(unknown.items()):
        out.append(finding('unknown-column', f"column {col} of project {b} is not in workflow.md; it holds "
                                             f"{len(names)} open card(s): {', '.join(sorted(names)[:10])}"
                                             f"{' ...' if len(names) > 10 else ''}", scope=f"project {b}:{col}",
                           board_no=b))
    out += _claude_rules(cards, claudes, hold_prs)
    out += _hr_rule(cards)
    out += _active_rules(cards, hold_issues)
    return sorted(out, key=lambda f: f['key'])


def _claude_rules(cards, claudes, hold_prs):
    out = []
    by_root = {}
    for c in claudes:
        if c.get('root'):
            by_root.setdefault(c['root'], []).append(c)
    for root, cs in sorted(by_root.items()):
        if len(cs) > 1:
            pids = ', '.join(str(c['pid']) for c in cs)
            out.append(finding('duplicate-claudes', f"{len(cs)} live Claudes in the worktree {root}: pids {pids}",
                               scope=root))
    live = {(c.get('repo'), c.get('branch')) for c in claudes if c.get('branch')}
    for c in cards:
        if c['kind'] != 'pr' or c['col'] != board.IN_PROGRESS or (c['repo'], c['n']) in hold_prs:
            continue
        if (c['repo'], c['branch']) not in live and (None, c['branch']) not in live:
            out.append(finding('no-live-claude', f"PR {c['repo']}#{c['n']} is in In Progress but no live Claude "
                                                 f"runs in a worktree on its branch {c['branch']}",
                               c['repo'], c['n'], board_no=c['board']))
    return out


def _hr_rule(cards):
    out = []
    by_board = {}
    for c in cards:
        if c['kind'] == 'pr' and c['col'] == board.HUMAN_REVIEW:
            by_board.setdefault(c['board'], []).append(c)
    for b, cs in sorted(by_board.items()):
        if len(cs) > HR_MAX:
            names = ', '.join(f"{c['repo']}#{c['n']}" for c in sorted(cs, key=lambda c: (c['repo'], c['n'])))
            out.append(finding('human-review-over-two', f"Human Review of project {b} holds {len(cs)} PRs: {names}",
                               board_no=b))
    return out


def _active_rules(cards, hold_issues):
    """The Active rules, on boards with issue data (closes is not None)."""
    out = []
    prs = [c for c in cards if c['kind'] == 'pr' and c['closes'] is not None]
    issues = {(c['board'], c['repo'], c['n']): c for c in cards if c['kind'] == 'issue'}
    active = {k for k, c in issues.items() if c['col'] == board.ACTIVE}
    owners = active | {k for k, c in issues.items() if c['col'] in valid_extra(c['board'])}  # #82 addendum
    # issue key -> PR numbers: open (any column), counted (flow or Deferred: #82 addendum), flow
    open_by_issue, counted_by_issue, flow_by_issue = {}, {}, {}
    for p in prs:
        for i, _ in p['closes']:
            k = (p['board'], p['repo'], i)
            open_by_issue.setdefault(k, []).append(p['n'])
            if p['col'] in FLOW_COLS + valid_extra(p['board']):
                counted_by_issue.setdefault(k, []).append(p['n'])
            if p['col'] in FLOW_COLS:
                flow_by_issue.setdefault(k, []).append(p['n'])
        if p['col'] not in FLOW_COLS:
            continue  # a Deferred PR counts for its issue but is never a finding
        mine = [i for i, labels in p['closes'] if WORK2 not in labels]
        if p['closes'] and not mine:
            continue  # the PR of a work2 issue: no Active card
        n_active = sum((p['board'], p['repo'], i) in owners for i in mine)
        if n_active == 0 and any((p['board'], p['repo'], i) in issues for i in mine):
            continue  # its open issue card is in the wrong column: that card's finding says so
        if n_active != 1:
            which = ', '.join(f"#{i}" for i in mine) or 'no issue (no closing link)'
            out.append(finding('flow-pr-active-count', f"PR {p['repo']}#{p['n']} in {p['col']} closes {which}; "
                                                       f"{n_active} of them in Active (must be exactly 1)",
                               p['repo'], p['n'], board_no=p['board']))
    for k, c in sorted(issues.items()):
        if c['col'] == board.NEEDS_INPUT:
            got = sorted(open_by_issue.get(k, []))
            fix = (f"its PR ({', '.join(f'#{n}' for n in got)}) goes in Needs Input and the issue in Active" if got
                   else 'with no PR it goes in Triage (a simple question for Michal) or Discuss')
            out.append(finding('issue-in-needs-input', f"issue {c['repo']}#{c['n']} is in Needs Input; {fix}",
                               c['repo'], c['n'], board_no=c['board']))
        elif c['col'] in ISSUE_COLS and c['col'] != board.ACTIVE and WORK2 not in c['labels']:
            got = sorted(flow_by_issue.get(k, []))
            if got:
                out.append(finding('issue-not-active', f"issue {c['repo']}#{c['n']} is in {c['col']} but has an open "
                                                       f"PR ({', '.join(f'#{n}' for n in got)}); it goes in Active",
                                   c['repo'], c['n'], board_no=c['board']))
    for k in sorted(active):
        c = issues[k]
        if WORK2 in c['labels']:
            out.append(finding('active-work2', f"issue {c['repo']}#{c['n']} is in Active with the work2 label",
                               c['repo'], c['n'], board_no=c['board']))
        got = counted_by_issue.get(k, [])
        if len(got) == 1 or (not got and (c['repo'], c['n']) in hold_issues):
            continue
        which = ', '.join(f"#{n}" for n in sorted(got)) or 'none'
        out.append(finding('active-pr-count', f"issue {c['repo']}#{c['n']} is in Active with {len(got)} open PRs "
                                              f"in a flow column or Deferred ({which}; must be exactly 1)",
                           c['repo'], c['n'], board_no=c['board']))
    return out


# -- report: grace, swarmbot, incidents, dedupe -----------------------------------------------

def state_path():
    from . import alerts
    return Path(alerts.STATE).with_name('sanity.json')  # read at call time: tests move STATE


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + '.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                d = {}
            d.setdefault('open', {})
            yield d
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _save(path, d):
    tmp = path.with_name(f'.{path.name}.{os.getpid()}')
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, path)


def incident_what(fs, firsts):
    """The body of the one incident ticket of a pass (#82): every new finding, with its rule."""
    def stamp(t):
        return time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(t))
    rows = '\n'.join(f"{n}. {f['text']}.\n   Rule `{f['rule']}`: {RULES.get(f['rule'], '')} First seen "
                     f"{stamp(firsts[f['key']])}; finding key `{f['key']}`." for n, f in enumerate(fs, 1))
    return (f"The sanity check (tangentswarm #43) found {len(fs)} new finding(s) in one pass:\n\n{rows}\n\n"
            f"The rules are in methodology workflow.md and tangentswarm docs/sanity-check.md.")


def incident_title(fs):
    head = fs[0]['text'] + (f" (+{len(fs) - 1} more)" if len(fs) > 1 else '')
    return f"sanity: {len(fs)} finding(s): {head}"[:120]


def where(b):
    """How a failed/unread key reads in a line: 'project 6', or 'units'."""
    return f"project {b}" if isinstance(b, int) else str(b)


def report(findings, now, apply=False, alert=None, incident=None, path=None, grace=None, unread=()):
    """Act on the findings of one pass. Returns (lines, next_due): next_due is when a pass
    must run again (a finding inside its grace, or a capped ticket), else None. A dry run
    (apply False) reads the state and never writes it, wakes no one and files nothing.
    unread: boards this pass could not read; their kept findings stay as they are (not
    resolved), and the pass runs again after RETRY."""
    from . import alerts
    grace = GRACE if grace is None else grace
    path = path or state_path()
    alert = alert or (lambda text, key, pr=None: alerts.alert(text, key=key, pr=pr))
    incident = incident or alerts.incident
    lines, due = [], []
    with _locked(path) as d:
        seen = d['open']
        now_keys = {f['key'] for f in findings}
        kept = {k: r for k, r in seen.items() if k not in now_keys and r.get('board') in unread}
        for k in sorted(set(seen) - now_keys - set(kept)):
            lines.append(f"resolved: {seen[k].get('text', k)}")
        for b in sorted(unread, key=str):
            due.append(now + RETRY)
            lines.append(f"{where(b)} not read; its findings are kept and the check runs again")
        recs = {f['key']: dict(seen.get(f['key']) or {'first': now, 'told': False, 'filed': None}, text=f['text'],
                               board=f.get('board'))
                for f in findings}
        ready = []
        for f in findings:
            r = recs[f['key']]
            if now - r['first'] < grace:
                due.append(r['first'] + grace)
                lines.append(f"pending ({int(grace - (now - r['first']))} s grace): {f['text']}")
            else:
                ready.append(f)
        # #82: one ticket and one message per pass for all its new findings; each finding once
        unfiled = [f for f in ready if not recs[f['key']]['filed']]
        tell = [f for f in ready if not recs[f['key']]['told']]
        got = None
        if unfiled:
            ikey = 'sanity:' + hashlib.sha1('|'.join(f"{f['key']}@{recs[f['key']]['first']}" for f in unfiled)
                                            .encode()).hexdigest()[:16]
            if not apply:
                lines.append(f"would file one incident for {len(unfiled)} finding(s): "
                             + ' | '.join(f['text'] for f in unfiled))
            else:
                got = incident(ikey, incident_title(unfiled),
                               incident_what(unfiled, {f['key']: recs[f['key']]['first'] for f in unfiled}),
                               prevent='Pending: swarmbot diagnoses which step broke each rule and proposes a guard.')
                if got and (got == 'dup' or str(got).startswith('http')):
                    for f in unfiled:
                        recs[f['key']]['filed'] = got
                    lines.append(f"incident {got}: {len(unfiled)} finding(s)")
                else:
                    due.append(now + RETRY)
                    lines.append(f"incident not filed ({got}); retry: {len(unfiled)} finding(s)")
        if tell:
            filed = f" Incident ticket: {got}." if got and str(got).startswith('http') else ''
            text = (f"Sanity check (tangentswarm #43): {len(tell)} new finding(s): "
                    + ' | '.join(f['text'] for f in tell) + f'. One incident ticket for this pass on Project 6.{filed}')
            key = 'sanity:' + hashlib.sha1('|'.join(f"{f['key']}@{recs[f['key']]['first']}" for f in tell)
                                           .encode()).hexdigest()[:16]
            if not apply:
                lines.append(f"would tell swarmbot: {text}")
            else:
                alert(text, key, pr=next((f['n'] for f in tell if f['repo'] == 'tangentcode/platform'), None))
                for f in tell:
                    recs[f['key']]['told'] = True
                lines.append(f"told swarmbot: {text}")
        if apply:
            d['open'] = {**kept, **recs}
            d['last'] = now
            _save(path, d)
    return lines, (min(due) if due else None)


# -- triggers ---------------------------------------------------------------------------------

def poke_path():
    from . import alerts
    return Path(alerts.STATE).with_name('sanity-poke')


def poke():
    """A board-change event (the webhook's projects_v2_item): the watchdog runs the sanity
    check on its next minute. Never raises."""
    try:
        p = poke_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass


def poked_at():
    try:
        return poke_path().stat().st_mtime
    except OSError:
        return 0.0


def fingerprint(snap):
    """A hash of every open card's Status and draft flag in the Project 4 snapshot: a change
    is a board change (a move made by hand shows here without a webhook)."""
    rows = sorted((it.get('id') or '', board.column(it.get('fieldValueByName')) or '',
                   bool((it.get('content') or {}).get('isDraft')))
                  for it in (snap.get('prs') or []) + (snap.get('issues') or []))
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()


def unit_findings(results):
    """unit-source findings from unitcheck.check results."""
    return [finding('unit-source', f"{r['unit']} does not run from flow-live: " + '; '.join(r['problems']),
                    scope=r['unit'], board_no=UNITS)
            for r in results if not r['ok']]


def gather(g, snap, now, claudes=None, failed=None, units=None):
    """Every card (Project 4 from the snapshot, the other boards read now), the live Claudes,
    and the holds; then the findings. A board whose read fails is skipped, and its number and
    error go in failed ({board: error}), so the other checks still run (Codex on PR #79).
    units: unitcheck.check results; None reads them now (a failed read goes in failed[UNITS])."""
    from . import boardcache, slots, watchdog
    cards = cards_from_snap(snap)
    for b in board.BOARDS[1:]:
        try:
            cards += cards_from_board(g['gql'], b)
        except Exception as e:
            if failed is None:
                raise
            failed[b.number] = f"{type(e).__name__}: {str(e)[:200]}"
    st = slots.load()
    # the launch records hold platform numbers only (slots, webhook.Executor.do_closed)
    hold_issues = {(boardcache.REPO, n) for n in watchdog.launching_issues(st, now)}
    hold_prs = {(boardcache.REPO, int(k)) for k in (st.get('unstarted') or {}) if str(k).isdigit()}
    if units is None:
        from . import unitcheck
        try:
            units = unitcheck.check()
        except Exception as e:
            if failed is None:
                raise
            failed[UNITS] = f"{type(e).__name__}: {str(e)[:200]}"
            units = []
    found = check(cards, live_claudes() if claudes is None else claudes, hold_issues, hold_prs)
    return found + unit_findings(units)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-sanity', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--apply', action='store_true', help='file the incident ticket and tell swarmbot')
    ap.add_argument('--json', action='store_true', help='print the findings as JSON')
    a = ap.parse_args(argv)
    from . import boardcache
    from .advance import hr_gate
    g = hr_gate()
    now = time.time()
    failed = {}
    found = gather(g, boardcache.snapshot(g['gql']), now, failed=failed)
    for b, err in sorted(failed.items(), key=str):
        print(f"{where(b)} not read: {err}", file=sys.stderr)
    if a.json:
        print(json.dumps(found, indent=1))
        return 0
    for f in found:
        print(f"{f['rule']}: {f['text']}")
    print(f"{len(found)} finding(s)")
    lines, due = report(found, now, apply=a.apply, unread=set(failed))
    for line in lines:
        print(line)
    if a.apply:
        from . import webhook
        webhook.drain_posts()  # the wake POST threads die with this process
    return 0


if __name__ == '__main__':
    sys.exit(main())
