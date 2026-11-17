"""Project 6 launcher: start a Backlog worker with no AI (tangentswarm #98).

Before, the Project 6 Backlog pull (p6merge.pull, tangentswarm #89) sent its picks to MethBot
in one 'pull' POST, and an LLM run started each worker by hand. swarm-start-scoped and
tangentswarm.launcher serve platform only (Project 4, mn- windows, platform worktrees). Now
the pull queues one spool request per pick (op 'p6launch'); the swarm-promoted daemon serves
each in its own process, outside swarm-workers.slice, and runs these steps:

  1. branch <slug> (launcher.slug of the title; <slug>-<N> when another PR has that name)
     from origin/main, with one empty "Start work on <repo name> #N" commit, pushed
  2. a draft PR (base main, body "Fixes #N")
  3. the PR card on Project 6 in In Progress; the issue card Backlog -> Active
  4. the brief file BRIEF_DIR/<code>-<PR>-<slug>.md
  5. the worktree ~/ver/<repo name>.<slug> (worktrees.ensure_worktree)
  6. the window <code>-<PR>-<slug> (swarm for tangentswarm, meth for methodology) in the
     agents session: a bash shell in the worktree, then Claude in its own scope in
     swarm-workers.slice, then the brief line when its prompt is ready

Each step is safe to run again: an existing branch, open PR, card, brief, worktree or window
is reused. A window that already runs an agent, or a worktree with a live Claude, starts
nothing. Memory admission (launches.admits) is checked first: when it holds, nothing is
made and the issue stays in Backlog (Deferred). A failed or deferred launch that came from
a pull is one MethBot exception (once per merge and issue), so a person can follow it.
"""
import json
import re
import subprocess
from pathlib import Path

from . import board

REPOS = ('tangentcode/tangentswarm', 'tangentcode/methodology')
WINDOW_CODE = {'tangentcode/tangentswarm': 'swarm', 'tangentcode/methodology': 'meth'}
CHECKS = {'tangentcode/tangentswarm': 'the full suite: ~/.venvs/swarm-tmux/bin/python -m pytest',
          'tangentcode/methodology': "the repo's own checks (see its README); they must pass"}

ITEM_Q = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
  issueOrPullRequest(number:$n){
   ...on Issue{state closedByPullRequestsReferences(first:20, includeClosedPrs:false){
     nodes{number state headRefName repository{nameWithOwner}}}
    projectItems(first:20){nodes{id project{id}
    fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}
   ...on PullRequest{state projectItems(first:20){nodes{id project{id}
    fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}}}}"""


class Deferred(Exception):
    """Memory admission holds the start: nothing was made, and the issue stays in Backlog."""


def brief_dir():
    from . import brief
    return brief.BRIEF_DIR


def brief_text(repo, n, title, branch, pr, wt):
    name = repo.split('/')[1]
    return (
        f"# {name} PR #{pr} / {name} issue #{n}: {title}\n\n"
        f"You are in {wt} on branch {branch}. The draft PR is #{pr} "
        f"(https://github.com/{repo}/pull/{pr}). You own it.\n\n"
        f"1. Read the spec: gh issue view {n} --repo {repo} --comments. Implement exactly what it asks, "
        "with tests for each rule and no network in tests. First check whether main already does it; "
        "if so, report that with evidence through needs_input and stop. Other workers build other "
        "Project 6 issues in parallel PRs; keep your change focused, and rebase on main if one merges "
        "first.\n"
        f"2. Checks: {CHECKS[repo]}. They must pass.\n"
        "3. Commit and push. No force-push of shared history and never merge main into the branch: "
        f"rebase instead, which is fine before it is merged. Run `gh pr ready {pr} -R {repo}`, then "
        "comment `@codex review` ONCE per head. Loop on Codex findings until it is clean. If Codex goes "
        "past about 3 rounds without converging, call needs_input instead of continuing. Keep token use "
        "low: no polling loops. When you fix a finding, fix every place with the same bug and add a test "
        "where code is involved. Reply on and resolve each Codex review thread you address.\n"
        "4. When Codex is clean on the current head, report done through the swarm MCP (handoff), and "
        "post a PR comment whose first line is exactly `MERGE-READY <full head sha>`. Print MERGE-READY "
        "as the last line too, with the PR URL, the check result, and a short summary. Do not merge.\n\n"
        "If you find the work is already done on main or the PR is irrelevant, say so with evidence "
        "through the swarm MCP needs_input tool and stop.\n\n"
        "Rules: no deploys or restarts, and never kill a process you did not start. Do not edit the live "
        "services or ~/bin. Card moves are done from outside. Do not ask questions in the pane. Ask "
        "questions only through the swarm MCP needs_input tool. Write plain, short sentences.\n")


def start_claude(window, wt, brief_path):
    """Start the worker in window (swarm-start-scoped's way, for any repo): an idle bash shell
    in wt, Claude typed into it in its own scope in swarm-workers.slice (fresh, no
    --continue), then the brief line once its prompt is ready. Returns {window, pane, unit}
    and maybe brief_error; raises when nothing could start."""
    import sys
    from . import launches
    sys.path.insert(0, str(launches.BIN))
    import swarm_parking as sp
    host = sp.Host()
    with sp.flocked(sp.resume_lock(window), blocking=False) as got:
        if not got:
            raise RuntimeError(f"another start of {window} is running")
        existing = sp.live_pane(host, window)
        if existing and existing.get('agent'):
            return dict(window=window, pane=existing['pane'], already=existing['agent'])
        pane, _ = sp.shell_for(host, window, str(wt), existing)
        argv, unit = sp.scoped_command(window, resume=False)
        host.type_line(pane, sp.launch_line(argv))
    out = dict(window=window, pane=pane, unit=unit)
    try:
        launches.send_brief(pane, unit, str(brief_path))
    except Exception as e:  # Claude may run; only the first message failed
        out['brief_error'] = str(e)
    return out


def admit():
    """(ok, why): launches' live memory and CPU admission (swarm-start-scoped's rule)."""
    from . import launches
    m = launches.memory()
    return launches.admits(m), launches.describe(m)


class P6Launcher:
    """One launch of one Project 6 issue. Every side effect is a method or an injected
    callable, so the tests run with no network, no tmux and no real git."""

    def __init__(self, repo, n, title, gql=None, sh=None, git=None, worktree=None, start=None,
                 admit=None, refuse=None, briefs=None, agent_root=None, wt_branch=None):
        if repo not in REPOS:
            raise ValueError(f"{repo} is not a Project 6 repo")
        self.repo, self.n, self.title = repo, int(n), (title or '').strip() or f"Issue #{n}"
        self.name = repo.split('/')[1]
        self.board = board.for_repo(repo)
        self._gql, self._sh, self._git = gql, sh, git
        self._worktree, self._start, self._admit, self._refuse = worktree, start, admit, refuse
        self.briefs = Path(briefs) if briefs else None
        self.agent_root = agent_root
        self._wt_branch = wt_branch
        self.notes = []

    # -- tools ------------------------------------------------------------------------------
    def gql(self, q, **v):
        if self._gql is None:
            from .advance import hr_gate
            self._gql = hr_gate()['gql']
        return self._gql(q, **v)

    def sh(self, argv, check=True):
        """stdout of argv; raises on a failed command when check."""
        if self._sh:
            return self._sh(argv, check=check)
        from . import tick
        return (tick.sh(argv, check=check).stdout or '').strip()

    def git(self, *args, check=True):
        if self._git:
            return self._git(list(args), check=check)
        from . import worktrees
        r = worktrees.git(list(args), worktrees.plain_repo(self.name), check=check)
        return (r.stdout or '').strip()

    def note(self, line):
        self.notes.append(line)
        return line

    # -- the steps --------------------------------------------------------------------------
    def node(self, n):
        owner, name = self.repo.split('/')
        d = self.gql(ITEM_Q, owner=owner, name=name, n=int(n))
        return ((d or {}).get('repository') or {}).get('issueOrPullRequest') or {}

    def linked_prs(self, node):
        """[(PR number, head branch)] of the open PRs of this repo that close the issue."""
        return [(p['number'], p.get('headRefName') or '')
                for p in ((node.get('closedByPullRequestsReferences') or {}).get('nodes') or [])
                if p.get('state') == 'OPEN' and (p.get('repository') or {}).get('nameWithOwner') == self.repo]

    def issue_item(self, n, node=None):
        """(state, item id, column) of issue or PR n on this board; item is None with no card."""
        node = self.node(n) if node is None else node
        for it in (node.get('projectItems') or {}).get('nodes') or []:
            if ((it.get('project') or {}).get('id')) == self.board.id:
                return node.get('state'), it['id'], board.column(it.get('fieldValueByName'))
        return node.get('state'), None, None

    def open_pr(self, branch):
        """(number, body) of the open PR of branch, or (None, '')."""
        out = self.sh(['gh', 'pr', 'list', '-R', self.repo, '--head', branch, '--state', 'open',
                       '--json', 'number,body', '--jq', '.[0] // empty'], check=False)
        try:
            d = json.loads(out) if out.strip() else {}
        except ValueError:
            d = {}
        return (int(d['number']), d.get('body') or '') if d.get('number') else (None, '')

    def closes(self, body):
        """True when body closes exactly this issue ("Fixes #98", not "#980")."""
        return re.search(rf'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:{re.escape(self.repo)})?'
                         rf'#{self.n}(?!\d)', body or '') is not None

    def start_message(self):
        return f'Start work on {self.name} #{self.n}'

    def wt_branch(self, path):
        """The branch checked out in path, '' for a detached or unreadable one, None with no dir."""
        if self._wt_branch:
            return self._wt_branch(path)
        if not Path(path).is_dir():
            return None
        r = subprocess.run(['git', '-C', str(path), 'branch', '--show-current'], capture_output=True, text=True,
                           timeout=60)
        return r.stdout.strip() if r.returncode == 0 else ''

    def owner(self, branch):
        """'free', 'ours', 'other' (never reused) or 'conflict' for a candidate name (Codex on PR #108).
        On origin: 'ours' when its open PR closes this issue, or it has no open PR and its tip
        is this issue's start commit (a launch that stopped before the PR); else 'other'; no
        such branch is 'free'. Local state must agree, because ensure_worktree reuses a local
        branch and worktree first: a local branch counts only when origin's branch is ours and
        has its tip; the worktree <root>/<name>.<slug> counts only when origin's branch is ours
        and the worktree is on that branch. Any other local branch or worktree is 'other', or
        'conflict' when origin's branch is this issue's (the launch is then refused)."""
        tip = self.git('ls-remote', 'origin', f'refs/heads/{branch}', check=False).split('\t')[0].strip()
        if tip:
            pr, body = self.open_pr(branch)
            self.git('fetch', '-q', 'origin', f'refs/heads/{branch}', check=False)
            if pr:
                remote = 'ours' if self.closes(body) else 'other'
            else:
                remote = 'ours' if self.git('log', '-1', '--format=%s', tip, check=False) == self.start_message() \
                    else 'other'
            if remote == 'other':
                return 'other'
        local = self.git('rev-parse', '--verify', '--quiet', f'refs/heads/{branch}', check=False).strip()
        if local and (not tip or not self.git_ok('merge-base', '--is-ancestor', local, tip)):
            # local work that origin's branch does not have: on this issue's own branch it is
            # an earlier worker's unpushed work (Codex on PR #108), never a reason to pick a new name
            return 'conflict' if tip else 'other'
        on = self.wt_branch(self.worktree_path(branch))
        if on is not None and (not tip or on != branch):
            return 'conflict' if tip else 'other'  # a worktree at this path on another branch
        return 'ours' if tip else 'free'

    def git_ok(self, *args):
        """True when the git command exits 0 (raises nothing)."""
        try:
            self.git(*args, check=True)
            return True
        except Exception:
            return False

    def pick_branch(self):
        """The first of <slug>, <slug>-<N>, <slug>-<N>-2 ... that is free or this issue's own.
        A branch of other work is never reused (Codex on PR #108: the -<N> name was taken
        without a check). A 'conflict' (this issue's own branch with local work origin does
        not have) refuses the launch: another name would start a second worker for the issue."""
        from .launcher import slug
        b = slug(self.title, self.n)
        for name in [b, f"{b}-{self.n}"] + [f"{b}-{self.n}-{k}" for k in range(2, 6)]:
            o = self.owner(name)
            if o == 'conflict':
                raise self.conflict(name)
            if o != 'other':
                return name
        raise RuntimeError(f"no free branch name for {self.repo}#{self.n}: {b}, {b}-{self.n} and "
                           f"{b}-{self.n}-2..5 belong to other work")

    def conflict(self, branch):
        return RuntimeError(f"{self.repo}#{self.n} already has its branch {branch}, and the local branch or "
                            f"worktree has work that origin does not have (an earlier worker?): no second "
                            "worker is started")

    def existing_branch(self, node):
        """The head branch of the issue's open PR, or None. An open PR on a branch of local
        work origin does not have refuses the launch (Codex on PR #108)."""
        linked = self.linked_prs(node)
        if not linked:
            return None
        pr, branch = linked[0]
        o = self.owner(branch) if branch else 'other'
        if o == 'ours':
            self.note(f"{self.repo}#{self.n} already has open PR #{pr} on {branch}")
            return branch
        if o == 'conflict':
            raise self.conflict(branch)
        raise RuntimeError(f"{self.repo}#{self.n} already has open PR #{pr} on {branch or 'an unknown branch'}, "
                           "which this launcher cannot reuse: no second PR or worker is started")

    def branch(self, branch):
        """Push one empty start commit on origin/main, unless the branch exists (pick_branch
        has checked that it is this issue's own)."""
        if self.git('ls-remote', 'origin', f'refs/heads/{branch}', check=False):
            return self.note(f"branch {branch} exists")
        self.git('fetch', '-q', 'origin', 'main')
        sha = self.git('commit-tree', 'origin/main^{tree}', '-p', 'origin/main', '-m', self.start_message())
        self.git('push', '-q', 'origin', f'{sha}:refs/heads/{branch}')
        return self.note(f"pushed branch {branch} ({sha[:8]}: {self.start_message()})")

    def draft_pr(self, branch):
        pr, body = self.open_pr(branch)
        if pr:
            if not self.closes(body):  # never another issue's PR, even if the branch changed hands
                raise RuntimeError(f"the open PR #{pr} of {branch} does not close {self.repo}#{self.n}")
            self.note(f"draft PR #{pr} exists")
            return pr
        url = self.sh(['gh', 'pr', 'create', '-R', self.repo, '--draft', '--base', 'main', '--head', branch,
                       '--title', self.title, '--body', f'Fixes #{self.n}'])
        m = re.search(r'/pull/(\d+)', url or '')
        if not m:
            raise RuntimeError(f"gh pr create gave no PR URL: {(url or '')[:200]}")
        self.note(f"opened draft PR #{m.group(1)}")
        return int(m.group(1))

    def move(self, item, col, n, kind):
        board.move_card({}, item, col, lambda argv: self.sh(argv), n=n, b=self.board, repo=self.repo, kind=kind)

    def cards(self, pr):
        """The PR card on Project 6 in In Progress; the issue card Backlog -> Active."""
        _, item, col = self.issue_item(pr)
        if not item:
            out = self.sh(['gh', 'project', 'item-add', str(self.board.number), '--owner', self.repo.split('/')[0],
                           '--url', f'https://github.com/{self.repo}/pull/{pr}', '--format', 'json'])
            item = json.loads(out)['id']
        if col != board.IN_PROGRESS:
            self.move(item, board.IN_PROGRESS, pr, 'pr')
        self.note(f"PR #{pr} card in In Progress")
        _, item, col = self.issue_item(self.n)
        if item and col == board.BACKLOG:
            self.move(item, board.ACTIVE, self.n, 'issue')
            self.note(f"issue #{self.n} card Backlog -> Active")
        elif not item:
            self.note(f"issue #{self.n} has no Project 6 card")

    def window(self, pr, branch):
        from . import worktrees
        return f"{WINDOW_CODE[self.repo]}-{pr}-{worktrees.branch_slug(branch)}"

    def worktree_path(self, branch):
        from . import registry, worktrees
        return Path(self.agent_root or registry.agent_root()) / f"{self.name}.{worktrees.branch_slug(branch)}"

    def brief(self, pr, branch):
        path = (self.briefs or brief_dir()) / f"{self.window(pr, branch)}.md"
        if path.exists():
            self.note(f"brief {path} exists")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(brief_text(self.repo, self.n, self.title, branch, pr, self.worktree_path(branch)))
        self.note(f"wrote brief {path}")
        return path

    def worktree(self, branch):
        if self._worktree:
            return Path(self._worktree(self.name, branch))
        from . import worktrees
        out = worktrees.ensure_worktree(self.name, branch)
        return Path(out['path'] if isinstance(out, dict) else out)

    def start(self, pr, branch, wt, path):
        window = self.window(pr, branch)
        from . import workerprocs
        why = (self._refuse or workerprocs.refuse_second_claude)(str(wt))
        if why:
            return self.note(f"no start: {why}")
        out = (self._start or start_claude)(window, wt, path)
        if out.get('already'):
            return self.note(f"{window} already runs {out['already']} in {out.get('pane')}: no start")
        line = f"started Claude in {window} ({out.get('pane')}, scope {out.get('unit')})"
        if out.get('brief_error'):
            raise RuntimeError(f"{line}, but the brief line was not sent: {out['brief_error'][:200]}")
        return self.note(line + "; brief line sent")

    # -- the run ----------------------------------------------------------------------------
    def run(self):
        """Launch the issue. Returns the notes; raises Deferred (nothing made) or the failed
        step's error (the steps before it stay, and a run again goes on from there)."""
        from . import alerts
        with alerts.once_lock(f"p6launch:{self.repo}#{self.n}"):  # never two launches of one issue at once
            node = self.node(self.n)
            state, _, _ = self.issue_item(self.n, node)
            if state and state != 'OPEN':
                self.note(f"{self.repo}#{self.n} is {state.lower()}: no launch")
                return self.notes
            ok, why = (self._admit or admit)()
            if not ok:
                raise Deferred(f"memory or CPU admission holds the start ({why}); "
                               f"{self.repo}#{self.n} stays in Backlog")
            branch = self.existing_branch(node) or self.pick_branch()
            self.branch(branch)
            pr = self.draft_pr(branch)
            self.cards(pr)
            path = self.brief(pr, branch)
            wt = self.worktree(branch)
            self.note(f"worktree {wt}")
            self.start(pr, branch, wt, path)
        return self.notes


def queue(repo, n, title, after=None, submit=None):
    """Queue one launch for the swarm-promoted daemon (spool op 'p6launch'). after: the
    {repo, pr, head} of the merge whose pull asked for it. Returns the request id."""
    from . import spool
    res = (submit or spool.submit)('p6launch', str(Path.home()), repo=repo, issue=int(n), title=title or '',
                                   after=after or {})
    return res.get('queued')


def in_flight(spool_dir=None):
    """{(repo, issue)} of the queued or running p6launch requests."""
    from . import spool
    return {(d.get('repo'), int(d.get('issue') or 0)) for d in spool.in_flight(ops=('p6launch',), spool=spool_dir)}


def pending_dir():
    from . import alerts
    return Path(alerts.STATE).parent / 'p6launch-pending'  # read at call time: tests move STATE


def _delivered(sent):
    return sent is True or sent == 'already sent'


def _send(x, tell=None):
    from . import alerts
    return (tell or alerts.tell_methbot)('exception', x['repo'], int(x['pr']), head=x['head'], key=x['key'],
                                         reason=x['reason'])


def tell_kept(x, tell=None):
    """One MethBot exception {repo, pr, head, key, reason}. It is written to pending_dir first
    and leaves it only once delivered, so a POST that fails is not lost when the spool request
    ends (Codex on PR #108); retry_pending sends it again. Returns tell's answer."""
    import hashlib
    path = pending_dir() / (hashlib.sha1(f"{x['repo']}#{x['pr']}@{x['head']}:{x['key']}".encode())
                            .hexdigest()[:20] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(x))
    tmp.replace(path)
    sent = _send(x, tell)
    if _delivered(sent):
        path.unlink(missing_ok=True)
    return sent


def retry_pending(tell=None):
    """Send each kept exception again (the daemon calls this every RETRY_EVERY s). A delivered
    one leaves the dir; an unreadable file is dropped. Returns (sent, left)."""
    d = pending_dir()
    sent = left = 0
    for path in sorted(d.glob('*.json')) if d.is_dir() else []:
        try:
            x = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if _delivered(_send(x, tell)):
            path.unlink(missing_ok=True)
            sent += 1
        else:
            left += 1
    return sent, left


RETRY_EVERY = 300  # s between retries of the kept MethBot exceptions


def execute(req, launcher=None, tell=None):
    """The spool op 'p6launch' (daemon side): {repo, issue, title, after}. A failed or deferred
    launch asked for by a pull is one MethBot exception on the merged PR and head (key
    launch:<repo>#<issue>)."""
    from . import eventdb
    repo, n, title = req.get('repo'), int(req['issue']), req.get('title') or ''
    after = req.get('after') or {}
    try:
        notes = (launcher or P6Launcher)(repo, n, title).run()
    except Exception as e:
        deferred = isinstance(e, Deferred)
        text = str(e) if deferred else f"{type(e).__name__}: {str(e)[:300]}"
        eventdb.log_event('p6-launch', source='p6launch', action='deferred' if deferred else 'failed', pr=n,
                          repo=repo, error=None if deferred else text, notes=[text] if deferred else ())
        sent = None
        if after.get('repo') and after.get('pr') and after.get('head'):
            sent = tell_kept({'repo': after['repo'], 'pr': int(after['pr']), 'head': after['head'],
                              'key': f"launch:{repo}#{n}",
                              'reason': f"Backlog pull after {after['repo']}#{after['pr']} merged: {repo}#{n} "
                                        f"was not started: {text}"}, tell=tell)
        return {'ok': deferred, 'issue': n, 'repo': repo, 'error' if not deferred else 'deferred': text,
                'methbot': str(sent)}
    eventdb.log_event('p6-launch', source='p6launch', action='launched', pr=n, repo=repo, notes=notes)
    return {'ok': True, 'issue': n, 'repo': repo, 'notes': notes}


def main(argv=None):
    """swarm-p6-launch REPO ISSUE [TITLE]: launch one Project 6 issue by hand (prints the notes)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog='swarm-p6-launch', description=__doc__.split('\n', 1)[0])
    ap.add_argument('repo', choices=REPOS)
    ap.add_argument('issue', type=int)
    ap.add_argument('title', nargs='?', default='')
    a = ap.parse_args(argv)
    title = a.title or subprocess.run(['gh', 'issue', 'view', str(a.issue), '-R', a.repo, '--json', 'title',
                                       '--jq', '.title'], capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        for line in P6Launcher(a.repo, a.issue, title).run():
            print(line)
    except Deferred as e:
        print(f"deferred: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"swarm-p6-launch: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
