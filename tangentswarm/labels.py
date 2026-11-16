"""swarm-label-sync: labels and milestones on tangentcode/platform PRs (Michal 2026-11-14 7:21-7:23 PM ET).

Rules:
  - Bots never invent labels. The bot labels MinBot-passed and swarm are retired (RETIRED):
    no tool writes them, and no gate needs them.
      hr-gate condition 4 reads a MinBot pass COMMENT that names the head (or the -vN tip).
      "Assigned to the swarm" (active-gate) is the Project 4 card in Active or a later column
      (assigned_by_card), matched by option id.
  - A PR inherits the labels (fast-track, priority, ...) and the milestone of every issue it
    fixes (closingIssuesReferences). Only labels that a HUMAN put on the issue are copied:
    the last LabeledEvent for the label must come from a User that is not a bot account.
    Sync only adds. It never removes a label and never replaces a milestone.

    swarm-label-sync N [N...] [--apply]     sync these PRs (dry run without --apply)
    swarm-label-sync --sweep [--apply]      every open PR in tangentcode/platform
    swarm-label-sync event NAME [--apply] < JSON
                                            webhook: a pull_request opened, reopened,
                                            ready_for_review or edited (body changed: the
                                            closing links may have changed) syncs that PR
Writes go through minbot (the MinBot app token). minbot has no milestone command, so a
milestone is reported ("milestone: needs ...") and not written.
Exit 0 = done, 2 = error.
"""
import argparse
import json
import os
import sys

from . import board, proc

REPO = 'tangentcode/platform'
MINBOT = os.environ.get('SWARM_MINBOT', 'minbot')
RETIRED = {'minbot-passed', 'swarm'}  # bot-made labels: never copied, never written
BOT_LOGINS = {'tangentcode-minbot', 'github-actions', 'chatgpt-codex-connector'}
# Active, In Progress and every later column: the swarm owns the issue (option ids only).
ASSIGNED_OPTIONS = {board.OPTION[c] for c in (board.ACTIVE, board.IN_PROGRESS, board.AUTO_REVIEW,
                                               board.NEEDS_INPUT, board.READY, board.STAGING,
                                               board.HUMAN_REVIEW)}
PROJECT_NUMBER = 4

ISSUE_FIELDS = """number labels(first:50){nodes{name}} milestone{number title}
 timelineItems(last:100, itemTypes:[LABELED_EVENT]){nodes{...on LabeledEvent{actor{__typename login} label{name}}}}"""
PR_FIELDS = f"""number state labels(first:50){{nodes{{name}}}} milestone{{number title}}
 closingIssuesReferences(first:10){{nodes{{{ISSUE_FIELDS}}}}}"""


def assigned_by_card(project_items):
    """(ok, why): the issue's Project 4 card is in Active or a later column (option id)."""
    for it in (project_items or {}).get('nodes') or []:
        if ((it.get('project') or {}).get('number')) != PROJECT_NUMBER:
            continue
        val = it.get('fieldValueByName') or {}
        col = board.column(val)
        if val.get('optionId') in ASSIGNED_OPTIONS:
            return True, f"Project 4 card in {col}"
        return False, f"Project 4 card in {col or 'no column'} (needs Active or later)"
    return False, 'no Project 4 card'


def is_bot(actor):
    login = (actor or {}).get('login') or ''
    return ((actor or {}).get('__typename') != 'User' or login.endswith('[bot]')
            or login.lower() in BOT_LOGINS)


def human_labels(issue):
    """Labels on the issue now whose last LabeledEvent came from a human (not RETIRED)."""
    now = {l['name'] for l in (issue.get('labels') or {}).get('nodes') or []}
    last = {}
    for e in (issue.get('timelineItems') or {}).get('nodes') or []:
        name = (e.get('label') or {}).get('name')
        if name:
            last[name] = e.get('actor')
    return sorted(n for n in now if n in last and not is_bot(last[n]) and n.lower() not in RETIRED)


def plan(pr):
    """(labels to add, milestone to set or None, notes) for one PR. Only adds."""
    have = {l['name'] for l in (pr.get('labels') or {}).get('nodes') or []}
    add, notes, miles = [], [], []
    for issue in (pr.get('closingIssuesReferences') or {}).get('nodes') or []:
        for name in human_labels(issue):
            if name not in have and name not in add:
                add.append(name)
        m = issue.get('milestone')
        if m and m['number'] not in [x['number'] for x in miles]:
            miles.append(m)
    milestone = None
    if miles and not pr.get('milestone'):
        milestone = miles[0]
        if len(miles) > 1:
            notes.append('issues have milestones ' + ', '.join(m['title'] for m in miles)
                         + f"; using {milestone['title']}")
    elif miles and pr['milestone']['number'] not in [m['number'] for m in miles]:
        notes.append(f"keeps its milestone {pr['milestone']['title']} (never replaced)")
    return add, milestone, notes


def event_pr(name, payload):
    """The PR number a webhook event should sync, or None."""
    if name != 'pull_request':
        return None
    act = payload.get('action')
    if act in ('opened', 'reopened', 'ready_for_review') or (
            act == 'edited' and 'body' in (payload.get('changes') or {})):
        return (payload.get('pull_request') or {}).get('number')
    return None


class Sync:
    def __init__(self, apply=False, out=print, gql=None, run=None):
        self.apply, self.out = apply, out
        self.gql = gql or self._gql
        self.run = run or self._run

    @staticmethod
    def _gql(q):
        r = proc.sh('gh', 'api', 'graphql', '-f', f'query={q}', who='swarm-label-sync')
        return json.loads(r.stdout)['data']

    def _run(self, *args):
        argv = [MINBOT, *(['--apply'] if self.apply else []), *map(str, args)]
        r = proc.sh(*argv, check=False, who='swarm-label-sync')
        if r.returncode:
            raise RuntimeError(f"minbot {' '.join(map(str, args[:3]))} failed: {(r.stderr or r.stdout).strip()[:200]}")

    def prs(self, numbers):
        owner, name = REPO.split('/')
        parts = ' '.join(f'p{int(n)}: pullRequest(number:{int(n)}){{{PR_FIELDS}}}' for n in numbers)
        data = self.gql(f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
        return [v for v in data.values() if v]

    def open_prs(self):
        owner, name = REPO.split('/')
        out, cursor = [], None
        while True:
            after = f', after:"{cursor}"' if cursor else ''
            q = (f'query{{repository(owner:"{owner}",name:"{name}"){{pullRequests(states:OPEN, first:25{after})'
                 f'{{pageInfo{{hasNextPage endCursor}} nodes{{{PR_FIELDS}}}}}}}}}')
            page = self.gql(q)['repository']['pullRequests']
            out += page['nodes']
            if not page['pageInfo']['hasNextPage']:
                return out
            cursor = page['pageInfo']['endCursor']

    def sync(self, pr):
        n = pr['number']
        add, milestone, notes = plan(pr)
        verb = 'added' if self.apply else 'would add'
        if add:
            self.run('label', 'add', n, *add, '--repo', REPO)
            self.out(f"#{n}: {verb} {', '.join(add)}")
        if milestone:
            self.out(f"#{n}: milestone: needs {milestone['title']} (minbot has no milestone command; not written)")
        for note in notes:
            self.out(f"#{n}: {note}")
        return bool(add or milestone)

    def main(self, numbers=None, sweep=False):
        prs = self.open_prs() if sweep else self.prs(numbers)
        changed = sum(self.sync(p) for p in prs if p.get('state', 'OPEN') == 'OPEN')
        self.out(f"label-sync: {len(prs)} PR(s) read, {changed} need a change"
                 + ('' if self.apply else ' (dry run)'))
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-label-sync', description=__doc__.split('\n', 1)[0])
    ap.add_argument('args', nargs='*', help="PR numbers, or: event NAME")
    ap.add_argument('--sweep', action='store_true', help='every open PR in tangentcode/platform')
    ap.add_argument('--apply', action='store_true', help='write (default: dry run)')
    a = ap.parse_args(argv)
    try:
        if a.args[:1] == ['event']:
            n = event_pr(a.args[1] if len(a.args) > 1 else '', json.load(sys.stdin))
            if not n:
                print('skip: this event does not change the closing links')
                return 0
            return Sync(apply=a.apply).main([n])
        nums = [int(x) for x in a.args if x.isdigit()]
        if not nums and not a.sweep:
            ap.error('give PR numbers or --sweep')
        return Sync(apply=a.apply).main(nums, sweep=a.sweep)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        print(f"swarm-label-sync error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
