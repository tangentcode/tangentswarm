"""Project 6 worker done: verify, merge and clean up on the server, no AI (tangentswarm #89).
Every GitHub, tmux and git call is a fake: no network in these tests."""
import json

import pytest

from tangentswarm import alerts, board, eventdb, p6merge as M, webhook as W, worktrees

HEAD = 'a' * 40
OLD = 'b' * 40
TS = 'tangentcode/tangentswarm'
METH = 'tangentcode/methodology'
CODEX = {'login': 'chatgpt-codex-connector'}
WORKER = {'login': 'tangentstorm'}
B6 = board.for_repo(TS)


def summary(head=HEAD, status='completed', anyf=False, unres=False):
    st = {'code_review': {'commit_sha': head, 'has_any_findings': anyf, 'has_unresolved_findings': unres,
                          'status': status}}
    return {'author': CODEX, 'body': f'<!-- codex-review-state:v1 {json.dumps(st)} -->\nCodex Review Summary'}


def check(name='tests', status='COMPLETED', conclusion='SUCCESS', required=True):
    return {'__typename': 'CheckRun', 'name': name, 'status': status, 'conclusion': conclusion, 'isRequired': required}


def thread(resolved=False, outdated=False, replies=(), opener=CODEX, oid=HEAD):
    cs = [{'author': opener, 'originalCommit': {'oid': oid}}] + [{'author': a, 'originalCommit': {'oid': oid}}
                                                                  for a in replies]
    return {'id': f'T{len(replies)}{int(resolved)}{int(outdated)}', 'isResolved': resolved, 'isOutdated': outdated,
            'comments': {'nodes': cs}}


def full(head=HEAD, state='OPEN', draft=False, base='main', mergeable='MERGEABLE', comments=None, threads=(),
         checks=None, closes=((89, 'OPEN', TS),), branch='p6-done-automerge', hrepo=TS):
    return {'number': 90, 'state': state, 'isDraft': draft, 'baseRefName': base, 'headRefOid': head,
            'headRefName': branch, 'headRepository': {'nameWithOwner': hrepo}, 'mergeable': mergeable,
            'closingIssuesReferences': {'nodes': [{'number': n, 'state': s, 'repository': {'nameWithOwner': r}}
                                                  for n, s, r in closes]},
            'comments': {'nodes': [summary(head)] if comments is None else comments},
            'reviews': {'nodes': []}, 'reviewThreads': {'nodes': list(threads)},
            'commits': {'nodes': [{'commit': {'oid': head, 'statusCheckRollup': {'contexts': {
                'nodes': [check()] if checks is None else checks}}}}]}}


class World:
    """The fakes of one run: GitHub (gql, gh), tmux, the worktree remover and MethBot."""

    def __init__(self, prs, windows=('@1\tswarm-90-done-automerge', '@2\tswarm-9-other', '@3\tswarm-900-x',
                                     '@4\tmeth-90-doc'), remove=None, live=1, backlog=None):
        self.prs = list(prs)  # one read each; the last one repeats
        self.gh, self.tmuxed, self.posts, self.wakes, self.slept, self.removed = [], [], [], [], [], []
        self.windows = windows
        self.merges_on_command = True
        self._remove = remove
        self.live = live
        self.backlog = backlog if backlog is not None else [
            {'repo': TS, 'number': 95}, {'repo': METH, 'number': 120}, {'repo': TS, 'number': 99}]

    def gql(self, q, **v):
        if 'issueOrPullRequest' in q:
            return {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': [
                {'id': f"ITEM{v['n']}", 'project': {'id': B6.id}}, {'id': 'OTHER', 'project': {'id': 'P4'}}]}}}}
        pr = self.prs.pop(0) if len(self.prs) > 1 else self.prs[0]
        return {'repository': {'p': pr}}

    def sh(self, argv):
        self.gh.append(argv)
        if argv[:3] == ['gh', 'pr', 'merge'] and self.merges_on_command:
            self.prs = [dict(self.prs[-1], state='MERGED')]
        return ''

    def tmux(self, args):
        self.tmuxed.append(args)
        return '\n'.join(self.windows) if args[0] == 'list-windows' else ''

    def remove(self, repo, branch, delete_branch=False):
        self.removed.append((repo, branch, delete_branch))
        if self._remove:
            return self._remove(repo, branch)
        return {'path': f'/ver/{repo}.{branch}', 'removed': True, 'branch_deleted': True}

    def kw(self, **extra):
        return {**dict(gql=self.gql, sh=self.sh, tmux=self.tmux, remove=self.remove, procs=lambda p: [],
                       live=lambda: self.live, backlog=lambda: self.backlog, sleep=self.slept.append,
                       tipcheck=lambda branch: None), **extra}

    def run(self, repo=TS, n=90, head=HEAD, trigger='done', **extra):
        kw = self.kw(**extra)
        tell = lambda *a, **k: alerts.tell_methbot(*a, post=self.posts.append, **k)  # noqa: E731
        wake = lambda why, key, pr: self.wakes.append((why, key, pr)) or 'woke swarmbot'  # noqa: E731
        return M.run(repo, n, head, trigger, tell=tell, wake=wake, **kw)

    def merges(self):
        return [a for a in self.gh if a[:3] == ['gh', 'pr', 'merge']]

    def moves(self):
        return [a[a.index('--id') + 1] for a in self.gh if a[:3] == ['gh', 'project', 'item-edit']]


# -- the comment marker -----------------------------------------------------------------------

def test_merge_ready_sha_reads_only_a_first_line_with_the_full_sha():
    assert M.merge_ready_sha(f'MERGE-READY {HEAD}\nall green') == HEAD
    assert M.merge_ready_sha(f'\n  MERGE-READY {HEAD}') == HEAD
    assert M.merge_ready_sha('MERGE-READY abc1234') is None
    assert M.merge_ready_sha(f'Done.\nMERGE-READY {HEAD}') is None
    assert M.merge_ready_sha(f'MERGE-READY {HEAD}x') is None
    assert M.merge_ready_sha('') is None and M.merge_ready_sha(None) is None


# -- verify: every check on the exact head --------------------------------------------------

def test_a_clean_pr_has_no_problem():
    assert M.problems(full(), HEAD) == []


@pytest.mark.parametrize('pr, why', [
    (full(state='CLOSED'), 'the PR is closed'),
    (full(draft=True), 'the PR is a draft'),
    (full(base='work'), 'targets work, not main'),
    (full(head=OLD), f'the PR head is {OLD[:8]}, not the reported {HEAD[:8]}'),
    (full(comments=[summary(status='running', anyf=None, unres=None)]), 'Codex is not clean'),
    (full(comments=[summary(anyf=True, unres=True)]), 'Codex is not clean'),
    (full(comments=[summary(head=OLD)]), 'Codex is not clean'),
    (full(comments=[]), 'Codex is not clean'),
    (full(threads=[thread()]), 'Codex is not clean'),  # an open Codex thread on the head is a finding
    (full(threads=[thread(opener=WORKER)]), '1 unresolved review thread(s)'),
    (full(threads=[thread(outdated=True, oid=OLD)]), '1 unresolved review thread(s)'),
    (full(threads=[thread(outdated=True, oid=OLD, replies=[CODEX])]), '1 unresolved review thread(s)'),
    (full(checks=[check(conclusion='FAILURE')]), 'required CI is not green: "tests" failure'),
    (full(checks=[check(status='IN_PROGRESS', conclusion=None)]), 'required CI is still running: "tests"'),
    (full(checks=[{'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'ERROR', 'isRequired': True}]),
     'required CI is not green: "ci/x" error'),
    (full(checks=[{'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'PENDING', 'isRequired': True}]),
     'required CI is still running: "ci/x"'),
    (full(mergeable='CONFLICTING'), 'the PR conflicts with main'),
    (full(mergeable='UNKNOWN'), 'GitHub has not confirmed the PR merges cleanly (mergeable UNKNOWN)'),
    ({}, 'the PR could not be read'),
])
def test_each_failed_check_is_a_reason(pr, why):
    out = M.problems(pr, HEAD)
    assert any(why in r for r in out), out


def test_a_codex_clean_receipt_without_the_completed_summary_is_not_clean():
    receipt = {'author': CODEX, 'body': f"Codex Review: Didn't find any major issues.\nReviewed commit: {HEAD[:10]}"}
    assert 'no completed review-state summary' in M.problems(full(comments=[receipt]), HEAD)[0]


def test_ok_threads_and_checks_do_not_block():
    pr = full(threads=[thread(resolved=True), thread(outdated=True, oid=OLD, replies=[WORKER])],
              checks=[check(), check('lint', conclusion='SKIPPED'), check('docs', conclusion='FAILURE', required=False),
                      {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'SUCCESS', 'isRequired': True}])
    assert M.problems(pr, HEAD) == []


def test_no_required_check_passes():
    assert M.problems(full(checks=[]), HEAD) == []


def test_every_failed_check_is_listed():
    out = M.problems(full(draft=True, mergeable='CONFLICTING', checks=[check(conclusion='FAILURE')]), HEAD)
    assert len(out) == 3


# -- all checks pass: merge and clean up ---------------------------------------------------

def test_a_clean_done_merges_at_the_head_and_cleans_up():
    w = World([full()])
    r = w.run()
    assert r['merged'] is True and r['pr'] == 90
    assert w.merges() == [['gh', 'pr', 'merge', '90', '-R', TS, '--merge', '--match-head-commit', HEAD]]
    assert w.moves() == ['ITEM90', 'ITEM89']  # the PR card and the closed issue card, Project 6 only
    assert all(a[a.index('--project-id') + 1] == B6.id and a[-1] == B6.option[board.DONE]
               for a in w.gh if a[:3] == ['gh', 'project', 'item-edit'])
    assert ['gh', 'issue', 'close', '89', '-R', TS, '--reason', 'completed'] in w.gh
    assert ['kill-window', '-t', '@1'] in w.tmuxed and len([a for a in w.tmuxed if a[0] == 'kill-window']) == 1
    assert w.removed == [('tangentswarm', 'p6-done-automerge', True)]
    assert any('deleted local branch p6-done-automerge' in x for x in r['notes'])
    rows = eventdb.store().rows("source='p6merge'")
    assert [x['action'] for x in rows] == ['merged'] and rows[0]['repo'] == TS
    assert w.wakes == [(f'{TS}#90 merged at {HEAD[:8]}: a deploy is needed. Notify only: deploys need '
                        "Michal's OK.", f'p6-deploy:90@{HEAD}', 90)]
    pulls = [p for p in w.posts if p['kind'] == 'pull']
    assert len(pulls) == 1 and f'{TS}#95, {METH}#120, {TS}#99' in pulls[0]['reason']
    assert [p['kind'] for p in w.posts] == ['pull']  # no exception: everything passed


def test_the_same_head_is_merged_once():
    w = World([full()])
    assert w.run()['merged'] is True
    again = w.run(trigger='comment')
    assert again['merged'] is True and again['already'] is True
    assert len(w.merges()) == 1 and len([p for p in w.posts if p['kind'] == 'pull']) == 1


def test_an_unknown_mergeable_is_read_again_before_it_fails():
    w = World([full(mergeable='UNKNOWN'), full()])
    assert w.run()['merged'] is True
    assert w.slept[:1] == [M.MERGEABLE_WAIT]
    w = World([full(mergeable='UNKNOWN')])
    r = w.run(n=91)
    assert r['merged'] is False and 'mergeable UNKNOWN' in r['why'] and len(w.slept) == M.MERGEABLE_TRIES - 1


def test_methodology_uses_meth_windows_and_no_deploy_note():
    w = World([full(closes=((7, 'CLOSED', METH),), hrepo=METH)])
    r = w.run(repo=METH)
    assert r['merged'] is True and ['kill-window', '-t', '@4'] in w.tmuxed
    assert not any(a[0] == 'kill-window' and a[-1] == '@1' for a in w.tmuxed)
    assert w.wakes == [] and w.removed == [('methodology', 'p6-done-automerge', True)]
    assert not any(a[:3] == ['gh', 'issue', 'close'] for a in w.gh)  # already closed by the merge
    assert w.moves() == ['ITEM90', 'ITEM7']


def test_an_issue_of_another_repo_is_never_closed_or_moved():
    w = World([full(closes=((89, 'OPEN', METH),))])
    assert w.run()['merged'] is True
    assert w.moves() == ['ITEM90'] and not any(a[:3] == ['gh', 'issue', 'close'] for a in w.gh)


def test_a_pr_merged_elsewhere_at_the_head_is_cleaned_up_with_no_merge():
    w = World([full(state='MERGED')])
    r = w.run(trigger='comment')
    assert r['merged'] is True and w.merges() == []
    assert any('already merged' in x for x in r['notes']) and w.removed


# -- any check fails: no merge, one MethBot exception per PR and head -----------------------

def test_a_failed_check_posts_one_exception_per_pr_and_head_and_never_merges():
    w = World([full(draft=True)])
    r = w.run()
    assert r['merged'] is False and 'the PR is a draft' in r['why'] and r['methbot'] is True
    assert w.merges() == [] and w.tmuxed == [] and w.removed == [] and w.wakes == []
    assert len(w.posts) == 1 and w.posts[0]['kind'] == 'exception' and w.posts[0]['head'] == HEAD
    assert w.posts[0]['repo'] == TS and w.posts[0]['pr'] == 90 and 'the PR is a draft' in w.posts[0]['reason']
    again = w.run(trigger='comment')
    assert again['merged'] is False and len(w.posts) == 1  # deduped per PR and head
    w.prs = [full(head=OLD, draft=True)]
    assert w.run(head=OLD)['merged'] is False and len(w.posts) == 2  # a new head is a new exception
    assert [x['action'] for x in eventdb.store().rows("source='p6merge'")] == ['refused'] * 3


def test_a_failed_merge_command_is_an_exception():
    w = World([full()])

    def sh(argv):
        w.gh.append(argv)
        if argv[:3] == ['gh', 'pr', 'merge']:
            raise RuntimeError('Head branch was modified')
    w.sh = sh
    r = w.run()
    assert r['merged'] is False and 'gh pr merge failed' in r['why']
    assert w.posts[0]['kind'] == 'exception' and w.tmuxed == []


def test_not_a_project_6_repo_or_not_a_full_sha_does_nothing():
    w = World([full()])
    assert 'not a Project 6 repo' in w.run(repo='tangentcode/platform')['why']
    assert 'full head sha' in w.run(head='abc')['why']
    assert w.gh == [] and w.posts == []


# -- cleanup problems after the merge are reported; the other steps go on --------------------

def test_a_dirty_worktree_is_left_and_reported_and_the_rest_goes_on():
    def dirty(repo, branch):
        raise worktrees.WorktreeError('REFUSING: /ver/tangentswarm.x has uncommitted or untracked changes')
    w = World([full()], remove=dirty)
    r = w.run()
    assert r['merged'] is True and any('worktree' in x and 'left' in x for x in r['notes'])
    kinds = [p['kind'] for p in w.posts]
    assert kinds == ['pull', 'exception'] and 'uncommitted' in w.posts[1]['reason']
    assert w.wakes  # the deploy note still went
    w.prs = [full()]
    w.run()  # the same head again: already merged, no second exception
    assert len(w.posts) == 2


def test_no_worktree_deletes_only_the_branch():
    def gone(repo, branch):
        raise worktrees.WorktreeError(f"no worktree of /ver/{repo} has {branch!r} checked out")
    deleted = []
    w = World([full()], remove=gone)
    r = w.run(delbranch=lambda repo, branch: deleted.append((repo, branch)) or f'deleted local branch {branch}')
    assert r['merged'] is True and deleted == [('tangentswarm', 'p6-done-automerge')]
    assert [p['kind'] for p in w.posts] == ['pull']


def test_a_kept_branch_is_reported():
    w = World([full()], remove=lambda repo, branch: {'path': 'p', 'branch_deleted': False, 'branch_kept': 'not merged'})
    w.run()
    assert [p['kind'] for p in w.posts] == ['pull', 'exception'] and 'not merged' in w.posts[1]['reason']


def test_no_window_is_a_note_not_a_problem():
    w = World([full()], windows=())
    r = w.run()
    assert any('no swarm-90-* window' in x for x in r['notes']) and [p['kind'] for p in w.posts] == ['pull']


def test_the_cleanup_waits_for_the_closed_windows_processes(tmp_path):
    seen = iter([[(1, 'claude')], [(1, 'claude')], []])
    w = World([full()])
    r = w.run(wt=tmp_path, procs=lambda p: next(seen))
    assert r['merged'] is True and w.slept == [1, 1]


# -- the Project 6 Backlog pull ---------------------------------------------------------------

def test_the_pull_takes_only_the_free_slots_in_order():
    w = World([full()], live=2)
    w.run()
    pull = [p for p in w.posts if p['kind'] == 'pull'][0]
    assert f'{TS}#95, {METH}#120' in pull['reason'] and '#99' not in pull['reason']


def test_no_free_slot_or_an_empty_backlog_posts_no_pull():
    w = World([full()], live=4)
    r = w.run()
    assert w.posts == [] and any('none pulled' in x for x in r['notes'])
    w = World([full()], backlog=[])
    r = w.run(n=91)
    assert w.posts == [] and any('Backlog is empty' in x for x in r['notes'])


def item(n, col, repo=TS, labels=(), due=None, state='OPEN', kind='Issue'):
    return {'id': f'I{n}', 'fieldValueByName': {'name': col, 'optionId': B6.option[col]},
            'content': {'__typename': kind, 'number': n, 'state': state, 'title': f't{n}',
                        'repository': {'nameWithOwner': repo},
                        'milestone': {'title': 'M', 'dueOn': due} if due else None,
                        'labels': {'nodes': [{'name': l} for l in labels]}}}


def test_backlog_issues_are_in_the_strict_order_and_triage_stays():
    pages = [
        [item(30, board.BACKLOG), item(40, board.BACKLOG, labels=['fast-track']), item(5, board.TRIAGE),
         item(12, board.BACKLOG, repo=METH, due='2026-12-01T00:00:00Z')],
        [item(8, board.BACKLOG, labels=['fast-track'], repo=METH), item(2, board.BACKLOG, state='CLOSED'),
         item(3, board.BACKLOG, repo='tangentcode/platform'), item(4, board.IN_PROGRESS),
         item(6, board.BACKLOG, kind='PullRequest'), item(20, board.BACKLOG, due='2026-11-20T00:00:00Z')],
    ]
    calls = []

    def gql(q, **v):
        calls.append(v['c'])
        i = len(calls) - 1
        return {'organization': {'projectV2': {'items': {'nodes': pages[i], 'pageInfo': {
            'hasNextPage': i == 0, 'endCursor': 'C1'}}}}}
    got = M.backlog_issues(gql, B6)
    assert [(i['repo'], i['number']) for i in got] == [(METH, 8), (TS, 40), (TS, 20), (METH, 12), (TS, 30)]
    assert calls == [None, 'C1']


def test_live_workers_counts_project_6_worktrees_with_a_claude(tmp_path):
    root = tmp_path
    panes = [(str(root / 'tangentswarm.a'), 'claude'), (str(root / 'tangentswarm.a' / 'tests'), 'node'),
             (str(root / 'methodology.b'), 'node'), (str(root / 'tangentswarm.c'), 'bash'),
             (str(root / 'platform.d'), 'claude'), (str(root / 'tangentswarm'), 'claude'),
             (str(root / 'x' / 'tangentswarm.e'), 'claude')]
    assert M.live_workers(panes=panes, root=root) == 2


# -- the MERGE-READY comment fallback in swarm-webhook ----------------------------------------

def comment(repo, body, user=WORKER, n=90, is_pr=True):
    issue = {'number': n, 'pull_request': {}} if is_pr else {'number': n}
    return {'action': 'created', 'repository': {'full_name': repo}, 'issue': issue,
            'comment': {'user': user, 'body': body, 'html_url': 'u'}}


@pytest.mark.parametrize('repo', [TS, METH])
def test_a_merge_ready_comment_on_a_project_6_pr_is_a_p6merge(repo):
    acts = W.classify('issue_comment', comment(repo, f'MERGE-READY {HEAD}\nAll green.'))
    assert [(a.kind, a.pr, a.args['head'], a.args['repo']) for a in acts] == [('p6merge', 90, HEAD, repo)]


@pytest.mark.parametrize('payload', [
    comment('tangentcode/platform', f'MERGE-READY {HEAD}'),
    comment(TS, f'MERGE-READY {HEAD}', user={'login': 'someone'}),
    comment(TS, 'MERGE-READY abc1234'),
    comment(TS, f'Ok.\nMERGE-READY {HEAD}'),
    comment(TS, f'MERGE-READY {HEAD}', is_pr=False),
])
def test_other_comments_are_not_a_p6merge(payload):
    assert 'p6merge' not in [a.kind for a in W.classify('issue_comment', payload)]


def test_the_executor_runs_the_merge_for_the_comment():
    ran = []
    ex = W.Executor(g={'gql': 'GQL'}, sh=lambda argv: '', wake_post=lambda p: None,
                    p6merge=lambda *a, **k: ran.append((a, k)) or {'merged': True, 'notes': ['card -> Done']})
    for a in W.classify('issue_comment', comment(TS, f'MERGE-READY {HEAD}')):
        ex.run(a)
    assert ran[0][0] == (TS, 90, HEAD, 'comment') and ran[0][1]['gql'] == 'GQL'
    assert ex.done == [f'{TS}#90 merged at {HEAD[:8]}; card -> Done'] and ex.errors == []
    ex = W.Executor(g={'gql': 'GQL'}, sh=lambda argv: '', p6merge=lambda *a, **k: {'merged': False, 'why': 'draft'})
    ex.run(W.classify('issue_comment', comment(TS, f'MERGE-READY {HEAD}'))[0])
    assert ex.done == [f'{TS}#90 not merged at {HEAD[:8]}: draft']
    ex = W.Executor(g={'gql': 'GQL'}, dry=True, p6merge=lambda *a, **k: pytest.fail('dry'))
    ex.run(W.classify('issue_comment', comment(TS, f'MERGE-READY {HEAD}'))[0])
    assert ex.done == [f'would verify and merge {TS}#90 at {HEAD[:8]}']


# -- Codex on PR #90: a MethBot POST that fails after the merge is sent again ---------------

def flaky(w, fail_kinds):
    """A MethBot POST that fails for these kinds until fail_kinds is emptied."""
    def tell(*a, **k):
        if a[0] in fail_kinds:
            return alerts.tell_methbot(*a, post=lambda p: False, **k)
        return alerts.tell_methbot(*a, post=w.posts.append, **k)
    return tell


def run_with(w, tell, **extra):
    kw = w.kw(**extra)
    return M.run(TS, 90, HEAD, 'done', tell=tell, wake=lambda why, key, pr: 'woke', **kw)


@pytest.mark.parametrize('remove, kinds', [
    (None, ['pull']),  # the Backlog pull
    (lambda repo, branch: (_ for _ in ()).throw(worktrees.WorktreeError('REFUSING: dirty')), ['pull', 'exception']),
])
def test_a_failed_post_after_the_merge_is_sent_on_the_next_report_with_no_new_merge(remove, kinds):
    w = World([full()], remove=remove)
    fail = set(kinds)
    r = run_with(w, flaky(w, fail))
    assert r['merged'] is True and w.posts == [] and len(w.merges()) == 1
    assert any('not sent' in x for x in r['notes'])
    again = run_with(w, flaky(w, fail))  # MethBot still down: kept again
    assert again['already'] is True and len(again['undelivered']) == len(kinds) and w.posts == []
    fail.clear()
    again = run_with(w, flaky(w, fail))  # MethBot is back: each pending POST goes once
    assert again['already'] is True and again['undelivered'] == []
    assert sorted(p['kind'] for p in w.posts) == sorted(kinds) and len(w.merges()) == 1
    run_with(w, flaky(w, fail))
    assert len(w.posts) == len(kinds)  # nothing is sent twice
    assert len(w.removed) == 1  # the cleanup itself never runs again


def test_a_delivered_post_leaves_no_pending_file():
    w = World([full()])
    w.run()
    p = M.P6Merge(TS, 90, HEAD, 'done').pending_path()
    assert not p.exists() and [x['kind'] for x in w.posts] == ['pull']


# -- Codex on PR #90 (cfd3e08): a part read or a merge into another base never counts ---------

def test_more_than_100_threads_or_checks_is_a_reason():
    pr = full()
    pr['reviewThreads']['pageInfo'] = {'hasPreviousPage': True}
    assert M.problems(pr, HEAD) == ['more than 100 review threads: they cannot all be read']
    pr = full()
    pr['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['pageInfo'] = {'hasNextPage': True}
    assert M.problems(pr, HEAD) == ['more than 100 CI checks on the head: the required ones cannot all be read']


def test_a_codex_summary_out_of_the_read_is_not_clean():
    other = [{'author': WORKER, 'body': 'note'}] * 60  # the summary is older than the last 60 comments
    assert any('Codex is not clean' in r for r in M.problems(full(comments=other), HEAD))


def test_a_pr_merged_into_another_base_gets_no_cleanup():
    w = World([full(state='MERGED', base='work')])
    r = w.run(trigger='comment')
    assert r['merged'] is False and 'targets work, not main' in r['why']
    assert w.merges() == [] and w.moves() == [] and w.tmuxed == [] and w.removed == []
    assert not any(a[:3] == ['gh', 'issue', 'close'] for a in w.gh)
    assert [p['kind'] for p in w.posts] == ['exception']


# -- Codex on PR #90 (869bbe0): a merge queue only queues; no cleanup until it merged ---------

def test_a_queued_merge_is_not_done_and_gets_no_cleanup():
    w = World([full()])
    w.merges_on_command = False  # the command queued it; GitHub still shows it OPEN
    r = w.run()
    assert r['merged'] is False and 'does not show the PR merged' in r['why'] and 'it is open at' in r['why']
    assert len(w.merges()) == 1 and w.moves() == [] and w.tmuxed == [] and w.removed == []
    assert [p['kind'] for p in w.posts] == ['exception']
    assert len(w.slept) == M.MERGEABLE_TRIES - 1
    w.prs = [full(state='MERGED')]  # the queue merged it: the next report cleans up, with no new merge
    r = w.run(trigger='comment')
    assert r['merged'] is True and len(w.merges()) == 1 and w.removed


def test_a_merge_confirmed_on_a_later_read_goes_on():
    w = World([full()])
    w.merges_on_command = False
    real = w.sh

    def sh(argv):
        real(argv)
        if argv[:3] == ['gh', 'pr', 'merge']:
            w.prs = [full(), full(state='MERGED')]  # one more read still OPEN, then MERGED
    w.sh = sh
    assert w.run()['merged'] is True and w.slept == [M.MERGEABLE_WAIT] and w.removed


# -- Codex on PR #90 (2e61599): the merge event finishes a deferred Project 6 cleanup ---------

def merged_event(repo, head=HEAD):
    return {'action': 'closed', 'repository': {'full_name': repo},
            'pull_request': {'number': 90, 'merged': True, 'base': {'ref': 'main'},
                             'head': {'sha': head, 'ref': 'p6-done-automerge', 'repo': {'full_name': repo}}}}


class MergedEx(W.Executor):
    def card(self, n):
        return None, None

    def closing_issues(self, n):
        return []


@pytest.mark.parametrize('repo', [TS, METH])
def test_a_project_6_merge_event_runs_the_cleanup(repo):
    ran = []
    ex = MergedEx(g={'gql': 'GQL'}, sh=lambda argv: '', enqueue=lambda argv, by='': None,
                  p6merge=lambda *a, **k: ran.append(a) or {'merged': True, 'notes': ['closed window swarm-90-x']})
    for a in W.classify('pull_request', merged_event(repo)):
        ex.run(a)
    assert ran == [(repo, 90, HEAD, 'merged')] and ex.errors == []
    assert f'{repo}#90 Project 6 cleanup at {HEAD[:8]}: closed window swarm-90-x' in ex.done


def test_a_platform_merge_event_never_runs_the_project_6_cleanup():
    ex = MergedEx(g={'gql': 'GQL'}, sh=lambda argv: '', enqueue=lambda argv, by='': None,
                  cascade=lambda: 'queued', signal_promoted=lambda: 'woken',
                  p6merge=lambda *a, **k: pytest.fail('platform'))
    ex.repo = 'tangentcode/platform'
    ex.p6_cleanup(W.Action('merged', 90, '', {'head': HEAD}))
    assert ex.errors == []


def test_the_merge_event_after_a_queued_merge_cleans_up_with_no_second_merge():
    w = World([full()])
    w.merges_on_command = False
    assert w.run()['merged'] is False  # queued: no cleanup
    w.prs = [full(state='MERGED')]
    r = w.run(trigger='merged')  # the merge event
    assert r['merged'] is True and len(w.merges()) == 1 and w.removed and w.moves()
    again = w.run(trigger='merged')
    assert again['already'] is True and len(w.removed) == 1


# -- Codex on PR #90 (6a0f2c6): only the worker's own reply clears an outdated thread ----------

def test_replies_by_other_reviewers_do_not_clear_an_outdated_thread():
    alice, bob = {'login': 'alice'}, {'login': 'bob'}
    t = thread(outdated=True, oid=OLD, opener=alice, replies=[bob])
    assert M.unresolved_threads(full(threads=[t])) == [t['id']]
    t = thread(outdated=True, oid=OLD, opener=alice, replies=[bob, WORKER])
    assert M.unresolved_threads(full(threads=[t])) == []
    t = thread(outdated=False, opener=alice, replies=[WORKER])  # not outdated: a reply never clears it
    assert M.unresolved_threads(full(threads=[t])) == [t['id']]


def test_the_worker_login_is_the_webhook_identity():
    assert M.WORKER_LOGIN == W.MICHAL


# -- Codex on PR #90 (561c3f2): never a fork's or a diverged local worktree --------------------

@pytest.mark.parametrize('hrepo', ['someone/tangentswarm', None])
def test_a_fork_head_touches_no_local_worktree_or_branch(hrepo):
    w = World([full(hrepo=hrepo)])
    r = w.run()
    assert r['merged'] is True and w.removed == []
    assert any('no local worktree or branch is touched' in x for x in r['notes'])
    assert [p['kind'] for p in w.posts] == ['pull']  # a note, not a problem


def test_a_local_branch_with_more_commits_is_left_and_reported():
    w = World([full()])
    r = w.run(tipcheck=lambda branch: 'its tip cccccccc has commits that are not in the merged head')
    assert r['merged'] is True and w.removed == []
    assert [p['kind'] for p in w.posts] == ['pull', 'exception'] and 'not in the merged head' in w.posts[1]['reason']


def test_tip_problem_reads_the_local_branch(tmp_path, monkeypatch):
    import subprocess

    def git(*a):
        return subprocess.run(['git', '-C', str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()
    repo = tmp_path / 'tangentswarm'
    repo.mkdir()
    git('init', '-q', '-b', 'main')
    git('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-q', '--allow-empty', '-m', 'a')
    merged = git('rev-parse', 'HEAD')
    git('branch', 'feat')
    monkeypatch.setattr(worktrees, 'plain_repo', lambda name, top=None: str(repo))
    run = M.P6Merge(TS, 90, merged, 'done')
    assert run.tip_problem('feat') is None and run.tip_problem('missing') is None
    git('checkout', '-q', 'feat')
    git('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-q', '--allow-empty', '-m', 'b')
    assert 'has commits that are not in the merged head' in run.tip_problem('feat')
    ahead = git('rev-parse', 'HEAD')
    assert M.P6Merge(TS, 90, ahead, 'done').tip_problem('main') is None  # an older tip is in the head
