"""Project 6 launcher with no AI (tangentswarm #98). Every GitHub, git, tmux and Claude call is
a fake: no network in these tests."""
import json

import pytest

from tangentswarm import alerts, board, eventdb, p6launch as L, spool

TS = 'tangentcode/tangentswarm'
METH = 'tangentcode/methodology'
B6 = board.for_repo(TS)
HEAD = 'c' * 40
TITLE = 'Project 6 launcher: start Backlog workers with no AI'
SLUG = 'project-6-launcher-start'


def status(col):
    return {'name': col, 'optionId': B6.option[col]}


class World:
    """GitHub (gql, gh), git, the worktree maker, the Claude start and admission, all fake."""

    def __init__(self, tmp_path, repo=TS, issue_state='OPEN', issue_col=board.BACKLOG, remote=(), prs=None,
                 pr_col=None, admit=(True, 'room'), refuse=None, start=None, tips=None,
                 local=None, wts=None):
        self.tmp, self.repo = tmp_path, repo
        self.issue_state, self.issue_col, self.pr_col = issue_state, issue_col, pr_col
        self.remote = set(remote)            # branches on origin
        self.tips = dict(tips or {})         # branch -> its tip commit subject
        self.local = dict(local or {})       # local branch -> its tip ('tip:<b>' is origin's tip)
        self.wts = dict(wts or {})           # worktree path -> the branch it has
        self.prs = dict(prs or {})           # branch -> (number, body)
        self.gh, self.gits, self.worktrees, self.starts = [], [], [], []
        self._admit, self._refuse, self._start = admit, refuse, start

    # -- fakes ---------------------------------------------------------------------------
    def gql(self, q, owner, name, n):
        assert f'{owner}/{name}' == self.repo
        if n == 98:
            items = [{'id': 'ISSUE98', 'project': {'id': B6.id}, 'fieldValueByName': status(self.issue_col)}] \
                if self.issue_col else []
            return {'repository': {'issueOrPullRequest': {'state': self.issue_state, 'projectItems': {'nodes': items}}}}
        items = [{'id': 'OTHER', 'project': {'id': 'P4'}, 'fieldValueByName': None}]
        if self.pr_col:
            items.append({'id': f'PR{n}', 'project': {'id': B6.id}, 'fieldValueByName': status(self.pr_col)})
        return {'repository': {'issueOrPullRequest': {'state': 'OPEN', 'projectItems': {'nodes': items}}}}

    def sh(self, argv, check=True):
        self.gh.append(argv)
        if argv[:3] == ['gh', 'pr', 'list']:
            b = argv[argv.index('--head') + 1]
            if b in self.prs:
                n, body = self.prs[b]
                return json.dumps({'number': n, 'body': body})
            return ''
        if argv[:3] == ['gh', 'pr', 'create']:
            b = argv[argv.index('--head') + 1]
            self.prs[b] = (108, argv[argv.index('--body') + 1])
            return f'https://github.com/{self.repo}/pull/108'
        if argv[:3] == ['gh', 'project', 'item-add']:
            return json.dumps({'id': 'PRITEM'})
        return ''

    def git(self, args, check=True):
        self.gits.append(args)
        if args[0] == 'ls-remote':
            b = args[2].removeprefix('refs/heads/')
            return f'tip:{b}\trefs/heads/{b}' if b in self.remote else ''
        if args[0] == 'rev-parse':
            return self.local.get(args[-1].removeprefix('refs/heads/'), '')
        if args[0] == 'merge-base':  # only a local tip equal to origin's is its ancestor here
            if args[2] != args[3] and check:
                raise RuntimeError('not an ancestor')
            return ''
        if args[0] == 'log':
            return self.tips.get(args[-1].removeprefix('tip:'), 'Some other work')
        if args[0] == 'commit-tree':
            return 'f' * 40
        if args[0] == 'push':
            self.remote.add(args[-1].split('refs/heads/')[1])
        return ''

    def worktree(self, name, branch):
        self.worktrees.append((name, branch))
        return f'/ver/{name}.{branch}'

    def start(self, window, wt, path):
        self.starts.append((window, str(wt), str(path)))
        if self._start:
            return self._start(window, wt, path)
        return {'window': window, 'pane': '%9', 'unit': f'swarm-{window}-abc'}

    def launcher(self, n=98, title=TITLE):
        return L.P6Launcher(self.repo, n, title, gql=self.gql, sh=self.sh, git=self.git, worktree=self.worktree,
                            start=self.start, admit=lambda: self._admit, refuse=lambda wt: self._refuse,
                            briefs=self.tmp / 'briefs', agent_root='/ver', wt_branch=lambda p: self.wts.get(str(p)))

    def moves(self):
        return [(a[a.index('--id') + 1], a[-1]) for a in self.gh if a[:3] == ['gh', 'project', 'item-edit']]

    def writes(self):
        return [a for a in self.gh if a[:3] in (['gh', 'pr', 'create'], ['gh', 'project', 'item-add'],
                                                ['gh', 'project', 'item-edit'])] + \
               [a for a in self.gits if a[0] in ('push', 'commit-tree')]


def test_a_backlog_issue_gets_branch_pr_cards_brief_worktree_window_and_claude(tmp_path):
    w = World(tmp_path)
    notes = w.launcher().run()
    # 1. the branch from origin/main with one empty start commit
    assert ['fetch', '-q', 'origin', 'main'] in w.gits
    assert ['commit-tree', 'origin/main^{tree}', '-p', 'origin/main', '-m', 'Start work on tangentswarm #98'] in w.gits
    assert ['push', '-q', 'origin', f"{'f' * 40}:refs/heads/{SLUG}"] in w.gits
    # 2. the draft PR, Fixes #N
    create = [a for a in w.gh if a[:3] == ['gh', 'pr', 'create']]
    assert create == [['gh', 'pr', 'create', '-R', TS, '--draft', '--base', 'main', '--head', SLUG,
                       '--title', TITLE, '--body', 'Fixes #98']]
    # 3. the PR card on Project 6 in In Progress; the issue card Backlog -> Active
    assert ['gh', 'project', 'item-add', str(B6.number), '--owner', 'tangentcode', '--url',
            f'https://github.com/{TS}/pull/108', '--format', 'json'] in w.gh
    assert w.moves() == [('PRITEM', B6.option[board.IN_PROGRESS]), ('ISSUE98', B6.option[board.ACTIVE])]
    assert all(a[a.index('--project-id') + 1] == B6.id for a in w.gh if a[:3] == ['gh', 'project', 'item-edit'])
    # 4. the brief file
    brief = tmp_path / 'briefs' / f'swarm-108-{SLUG}.md'
    text = brief.read_text()
    assert f'/ver/tangentswarm.{SLUG}' in text and f'branch {SLUG}' in text and 'PR is #108' in text
    assert f'gh issue view 98 --repo {TS} --comments' in text and 'MERGE-READY <full head sha>' in text
    assert '~/.venvs/swarm-tmux/bin/python -m pytest' in text
    # 5. the worktree and 6. the window with Claude and the brief line
    assert w.worktrees == [('tangentswarm', SLUG)]
    assert w.starts == [(f'swarm-108-{SLUG}', f'/ver/tangentswarm.{SLUG}', str(brief))]
    assert notes[-1] == f'started Claude in swarm-108-{SLUG} (%9, scope swarm-swarm-108-{SLUG}-abc); brief line sent'
    rows = eventdb.store().rows("source='board'")
    assert [r['action'] for r in rows] == [board.IN_PROGRESS, board.ACTIVE]


def test_methodology_uses_meth_windows_and_its_own_checks(tmp_path):
    w = World(tmp_path, repo=METH)
    w.launcher().run()
    assert w.starts[0][0] == f'meth-108-{SLUG}' and w.worktrees == [('methodology', SLUG)]
    assert ['commit-tree', 'origin/main^{tree}', '-p', 'origin/main', '-m', 'Start work on methodology #98'] in w.gits
    text = (tmp_path / 'briefs' / f'meth-108-{SLUG}.md').read_text()
    assert "the repo's own checks" in text and 'swarm-tmux' not in text


def test_a_second_run_reuses_every_step(tmp_path):
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (108, 'Fixes #98')}, pr_col=board.IN_PROGRESS,
              issue_col=board.ACTIVE)
    (tmp_path / 'briefs').mkdir()
    brief = tmp_path / 'briefs' / f'swarm-108-{SLUG}.md'
    brief.write_text('kept')
    notes = w.launcher().run()
    assert w.writes() == []  # no push, no new PR, no card add or move
    assert brief.read_text() == 'kept'
    assert f'branch {SLUG} exists' in notes and 'draft PR #108 exists' in notes
    assert len(w.starts) == 1  # the start itself refuses a window that runs an agent


def test_a_pr_card_in_another_column_moves_to_in_progress(tmp_path):
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (108, 'Fixes #98')}, pr_col=board.BACKLOG)
    w.launcher().run()
    assert w.moves()[0] == ('PR108', B6.option[board.IN_PROGRESS])
    assert not [a for a in w.gh if a[:3] == ['gh', 'project', 'item-add']]


def test_a_branch_name_of_another_issue_gets_the_number(tmp_path):
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (50, 'Fixes #980')})
    w.launcher().run()
    assert ['push', '-q', 'origin', f"{'f' * 40}:refs/heads/{SLUG}-98"] in w.gits
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')] and w.starts[0][0] == f'swarm-108-{SLUG}-98'


def test_admission_that_holds_makes_nothing(tmp_path):
    w = World(tmp_path, admit=(False, 'slice full'))
    with pytest.raises(L.Deferred, match='slice full'):
        w.launcher().run()
    assert w.writes() == [] and w.worktrees == [] and w.starts == []
    assert not (tmp_path / 'briefs').exists()


def test_a_closed_issue_is_not_launched(tmp_path):
    w = World(tmp_path, issue_state='CLOSED')
    notes = w.launcher().run()
    assert notes == [f'{TS}#98 is closed: no launch'] and w.writes() == [] and w.starts == []


def test_a_live_claude_in_the_worktree_starts_nothing(tmp_path):
    w = World(tmp_path, refuse='another Claude is live in /ver/x (pid 7)')
    notes = w.launcher().run()
    assert w.starts == [] and notes[-1] == 'no start: another Claude is live in /ver/x (pid 7)'


def test_a_window_that_runs_an_agent_starts_nothing(tmp_path):
    w = World(tmp_path, start=lambda window, wt, path: {'window': window, 'pane': '%3', 'already': 'claude'})
    notes = w.launcher().run()
    assert notes[-1] == f'swarm-108-{SLUG} already runs claude in %3: no start'


def test_a_brief_line_that_was_not_sent_is_a_failure(tmp_path):
    w = World(tmp_path, start=lambda window, wt, path: {'window': window, 'pane': '%3', 'unit': 'u',
                                                        'brief_error': 'no ready Claude prompt'})
    with pytest.raises(RuntimeError, match='brief line was not sent: no ready Claude prompt'):
        w.launcher().run()


def test_only_project_6_repos():
    with pytest.raises(ValueError):
        L.P6Launcher('tangentcode/platform', 1, 't')


def test_closes_is_exact():
    p = L.P6Launcher(TS, 98, 't')
    assert p.closes('Fixes #98') and p.closes(f'closes {TS}#98') and not p.closes('Fixes #980')
    assert not p.closes('see #98')


# -- the spool op -------------------------------------------------------------------------

AFTER = {'repo': TS, 'pr': 90, 'head': HEAD}


class Fake:
    def __init__(self, raise_=None, notes=('ok',)):
        self.raise_, self.notes, self.made = raise_, list(notes), []

    def __call__(self, repo, n, title):
        self.made.append((repo, n, title))
        return self

    def run(self):
        if self.raise_:
            raise self.raise_
        return self.notes


def test_a_launch_is_queued_in_the_spool_and_counts_as_in_flight():
    rid = L.queue(TS, 98, TITLE, after=AFTER)
    req = json.loads((spool.SPOOL / f'{rid}.req.json').read_text())
    assert req['op'] == 'p6launch' and req['repo'] == TS and req['issue'] == 98 and req['after'] == AFTER
    assert L.in_flight() == {(TS, 98)}


def test_the_spool_serves_p6launch(monkeypatch):
    seen = []
    monkeypatch.setattr(L, 'execute', lambda req: seen.append(req) or {'ok': True})
    assert spool.execute({'op': 'p6launch', 'repo': TS, 'issue': 98}) == {'ok': True}
    assert seen == [{'op': 'p6launch', 'repo': TS, 'issue': 98}]


def test_execute_launches_and_logs_one_event():
    f = Fake()
    out = L.execute({'repo': TS, 'issue': 98, 'title': TITLE, 'after': AFTER}, launcher=f)
    assert out == {'ok': True, 'issue': 98, 'repo': TS, 'notes': ['ok']} and f.made == [(TS, 98, TITLE)]
    assert [r['action'] for r in eventdb.store().rows("source='p6launch'")] == ['launched']


@pytest.mark.parametrize('err, ok', [(RuntimeError('gh pr create: boom'), False), (L.Deferred('slice full'), True)])
def test_a_failed_or_deferred_launch_tells_methbot_once(err, ok):
    posts = []
    tell = lambda *a, **k: alerts.tell_methbot(*a, post=posts.append, **k)  # noqa: E731
    req = {'repo': METH, 'issue': 120, 'title': 't', 'after': AFTER}
    out = L.execute(req, launcher=Fake(err), tell=tell)
    assert out['ok'] is ok and out['methbot'] == 'True'
    assert len(posts) == 1 and posts[0]['kind'] == 'exception' and posts[0]['pr'] == 90
    assert f'{METH}#120 was not started' in posts[0]['reason'] and str(err) in posts[0]['reason']
    L.execute(req, launcher=Fake(err), tell=tell)
    assert len(posts) == 1  # once per merge and issue


def test_a_hand_launch_with_no_merge_tells_nobody():
    out = L.execute({'repo': TS, 'issue': 98}, launcher=Fake(RuntimeError('x')),
                    tell=lambda *a, **k: pytest.fail('no MethBot POST'))
    assert out['ok'] is False and out['methbot'] == 'None'


# -- Codex on PR #108: a branch or PR of other work is never reused ----------------------------

def test_both_names_of_other_work_give_the_next_free_name(tmp_path):
    # Codex's case: the slug and <slug>-98 both belong to other issues' open PRs
    w = World(tmp_path, remote={SLUG, f'{SLUG}-98'}, prs={SLUG: (50, 'Fixes #980'), f'{SLUG}-98': (60, 'Fixes #7')})
    w.launcher().run()
    assert ['push', '-q', 'origin', f"{'f' * 40}:refs/heads/{SLUG}-98-2"] in w.gits
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98-2')]
    assert not [a for a in w.gh if a[:3] == ['gh', 'project', 'item-edit'] and 'PR60' in a]


def test_a_branch_with_no_pr_is_ours_only_with_this_issues_start_commit(tmp_path):
    # a launch that stopped after the push and before the PR: the branch is reused, no new push
    w = World(tmp_path, remote={SLUG}, tips={SLUG: 'Start work on tangentswarm #98'})
    w.launcher().run()
    assert not [a for a in w.gits if a[0] == 'push'] and w.worktrees == [('tangentswarm', SLUG)]
    # a branch with no PR and any other tip (another issue's start, or hand work) is not ours
    w = World(tmp_path / 'b', remote={SLUG}, tips={SLUG: 'Start work on tangentswarm #9'})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')]


def test_no_free_name_makes_nothing(tmp_path):
    names = [SLUG, f'{SLUG}-98'] + [f'{SLUG}-98-{k}' for k in range(2, 6)]
    w = World(tmp_path, remote=set(names))
    with pytest.raises(RuntimeError, match='no free branch name'):
        w.launcher().run()
    assert w.writes() == [] and w.worktrees == [] and w.starts == []


def test_a_reused_pr_that_does_not_close_the_issue_is_refused(tmp_path):
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (50, 'Fixes #98')})
    lau = w.launcher()
    w.prs[SLUG] = (50, 'Fixes #7')  # the branch changed hands after the pick
    with pytest.raises(RuntimeError, match='PR #50 of .* does not close'):
        lau.draft_pr(SLUG)


# -- Codex on PR #108: a MethBot exception whose POST fails is kept and sent again --------------

@pytest.mark.parametrize('err', [RuntimeError('gh pr create: boom'), L.Deferred('slice full')])
def test_a_failed_methbot_post_is_kept_until_it_is_delivered(err):
    req = {'repo': METH, 'issue': 120, 'title': 't', 'after': AFTER}
    out = L.execute(req, launcher=Fake(err), tell=lambda *a, **k: alerts.tell_methbot(*a, post=lambda p: False, **k))
    assert 'True' not in out['methbot'] and len(list(L.pending_dir().glob('*.json'))) == 1
    down = lambda *a, **k: alerts.tell_methbot(*a, post=lambda p: False, **k)  # noqa: E731
    assert L.retry_pending(tell=down) == (0, 1)  # still down: kept
    posts = []
    up = lambda *a, **k: alerts.tell_methbot(*a, post=posts.append, **k)  # noqa: E731
    assert L.retry_pending(tell=up) == (1, 0)
    assert len(posts) == 1 and f'{METH}#120 was not started' in posts[0]['reason'] and posts[0]['pr'] == 90
    assert list(L.pending_dir().glob('*.json')) == [] and L.retry_pending(tell=up) == (0, 0)


def test_a_delivered_exception_leaves_no_pending_file():
    posts = []
    L.execute({'repo': TS, 'issue': 98, 'after': AFTER}, launcher=Fake(RuntimeError('x')),
              tell=lambda *a, **k: alerts.tell_methbot(*a, post=posts.append, **k))
    assert len(posts) == 1 and not list(L.pending_dir().glob('*.json'))


def test_an_unreadable_pending_file_is_dropped():
    L.pending_dir().mkdir(parents=True)
    (L.pending_dir() / 'bad.json').write_text('{')
    assert L.retry_pending(tell=lambda *a, **k: pytest.fail('no POST')) == (0, 0)
    assert not (L.pending_dir() / 'bad.json').exists()


def test_the_daemon_retries_kept_exceptions_every_retry_every_seconds(monkeypatch):
    from tangentswarm import promoted
    p = promoted.Promoter(g={}, dry_run=False)
    calls = []
    retry = lambda: calls.append(1) or (0, 0)  # noqa: E731
    p.retry_p6launch(now=1000.0, retry=retry)
    p.retry_p6launch(now=1000.0 + L.RETRY_EVERY - 1, retry=retry)
    p.retry_p6launch(now=1000.0 + L.RETRY_EVERY, retry=retry)
    assert len(calls) == 2
    p.retry_p6launch(now=5000.0, retry=lambda: (_ for _ in ()).throw(OSError('x')))  # never raises


# -- Codex on PR #108 (adfa44e): local branches and worktrees count too ------------------------

def test_a_local_branch_or_worktree_of_other_work_is_never_used(tmp_path):
    # a local branch left after an old cleanup, not on origin: the name is not free
    w = World(tmp_path, local={SLUG: 'old'})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')]
    # a worktree dir at the path with no origin branch
    w = World(tmp_path / 'b', wts={f'/ver/tangentswarm.{SLUG}': SLUG})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')]


def test_local_state_of_our_own_branch_is_reused(tmp_path):
    # origin's branch is ours (open PR closes #98), the local branch has origin's tip, and the
    # worktree is on that branch: a run again of a launch
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (108, 'Fixes #98')}, local={SLUG: f'tip:{SLUG}'},
              wts={f'/ver/tangentswarm.{SLUG}': SLUG})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', SLUG)] and not [a for a in w.gits if a[0] == 'push']


def test_local_work_that_origin_does_not_have_or_a_worktree_on_another_branch_is_other(tmp_path):
    w = World(tmp_path, remote={SLUG}, prs={SLUG: (108, 'Fixes #98')}, local={SLUG: 'unrelated'})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')]
    w = World(tmp_path / 'b', remote={SLUG}, prs={SLUG: (108, 'Fixes #98')},
              wts={f'/ver/tangentswarm.{SLUG}': 'something-else'})
    w.launcher().run()
    assert w.worktrees == [('tangentswarm', f'{SLUG}-98')]


def test_owner_with_real_git_repos(tmp_path, monkeypatch):
    """Codex's repro: a local branch of unrelated work that origin does not have is not free."""
    import subprocess

    def g(*a, cwd=tmp_path):
        return subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    root = tmp_path / 'ver'
    root.mkdir()
    g('init', '-q', '--bare', '-b', 'main', str(tmp_path / 'origin.git'))
    g('clone', '-q', str(tmp_path / 'origin.git'), str(root / 'tangentswarm'))
    repo = root / 'tangentswarm'
    for k, v in (('user.name', 't'), ('user.email', 't@example.invalid')):
        g('config', k, v, cwd=repo)
    g('commit', '-q', '--allow-empty', '-m', 'base', cwd=repo)
    g('push', '-q', 'origin', 'HEAD:main', cwd=repo)
    g('branch', 'mine', cwd=repo)
    g('commit', '-q', '--allow-empty', '-m', 'Unrelated local work', cwd=repo)
    g('branch', 'unrelated', cwd=repo)
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(root))
    lau = L.P6Launcher(TS, 98, TITLE, sh=lambda argv, check=True: '', agent_root=str(root))
    assert lau.owner('unrelated') == 'other'  # local only: never free
    assert lau.owner('nothing-here') == 'free'
    g('push', '-q', 'origin', 'mine', cwd=repo)  # origin has 'mine' with no PR and no start commit
    assert lau.owner('mine') == 'other'
    sha = g('commit-tree', 'origin/main^{tree}', '-p', 'origin/main', '-m', 'Start work on tangentswarm #98', cwd=repo)
    g('push', '-q', 'origin', f'{sha}:refs/heads/started', cwd=repo)
    assert lau.owner('started') == 'ours'  # a launch that stopped before its PR
    g('branch', 'started', 'unrelated', cwd=repo)  # but a local 'started' of other work
    assert lau.owner('started') == 'other'
