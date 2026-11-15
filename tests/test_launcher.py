"""tangentswarm.launcher (MethBot 2026-11-15 3:35 AM ET): the cascade launches the Backlog
issues it names (branch, draft PR "Fixes #N", board, brief, worker). Nothing consumed
START_CLAUDE before, so Platform In Progress sat empty."""
import json

import pytest

from tangentswarm import board, launcher as L, slots


class Run:
    """Fake subprocess.run: records argv, answers by command."""
    def __init__(self, branch_exists=False, pr_exists=None):
        self.calls, self.branch_exists, self.pr_exists = [], branch_exists, pr_exists

    def __call__(self, argv, **k):
        self.calls.append(argv)
        out = ''
        if 'ls-remote' in argv:
            out = 'abc\trefs/heads/x' if self.branch_exists else ''
        elif 'commit-tree' in argv:
            out = 'c' * 40
        elif argv[:3] == ['gh', 'pr', 'list']:
            out = str(self.pr_exists or '')
        elif argv[:3] == ['gh', 'pr', 'create']:
            out = 'https://github.com/tangentcode/platform/pull/601'
        elif argv[:3] == ['gh', 'project', 'item-add']:
            out = json.dumps({'id': 'PRITEM'})

        class R:
            returncode, stdout, stderr = 0, out, ''
        return R()


def gate():
    from boardfake import answer
    issue = {'id': 'ISSUEITEM', 'fieldValueByName': {'name': 'Backlog', 'optionId': board.OPTION[board.BACKLOG]},
             'content': {'__typename': 'Issue', 'number': 412, 'title': 't', 'state': 'OPEN',
                         'repository': {'nameWithOwner': L.REPO}, 'labels': {'nodes': []}}}
    return {'gql': lambda q, **v: answer([issue]), 'project_id': lambda: 'PROJ', 'STATUS_FIELD': 'FIELD'}


def test_slug_is_issue_number_plus_short_title():
    assert L.slug('Board: mark right / wrong answers in the PTW quiz view!', 412) == \
        'issue-412-board-mark-right-wrong-answers-in'
    assert L.slug('', 7) == 'issue-7'


def test_a_launch_makes_the_branch_draft_pr_board_brief_and_worker(tmp_path, monkeypatch):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    run, started = Run(), []
    slots.launch_mark([412])
    lz = L.Launcher(g=gate(), run=run, git_dir=tmp_path,
                    launch=lambda branch, pr, brief: started.append((branch, pr, brief)) or
                    ('launched', {'window': 'mn-601-x', 'pane': '%9'}))
    steps = lz.launch(412, 'Board marks right and wrong')
    br = 'issue-412-board-marks-right-and-wrong'
    assert ['git', '-C', str(tmp_path), 'push', '-q', 'origin', f"{'c' * 40}:refs/heads/{br}"] in run.calls
    create = [c for c in run.calls if c[:3] == ['gh', 'pr', 'create']][0]
    assert '--draft' in create and create[create.index('--body') + 1] == 'Fixes #412'
    edits = [c for c in run.calls if c[:3] == ['gh', 'project', 'item-edit']]
    assert [(e[e.index('--id') + 1], e[-1]) for e in edits] == [
        ('PRITEM', board.OPTION[board.IN_PROGRESS]), ('ISSUEITEM', board.OPTION[board.ACTIVE])]
    assert started == [(br, 601, str(tmp_path / 'briefs' / '601.txt'))]
    text = (tmp_path / 'briefs' / '601.txt').read_text()
    assert text.startswith('Task #412: Board marks right and wrong') and 'draft PR #601' in text
    assert steps[-1] == 'worker launched in mn-601-x (%9)'
    st = slots.load()
    assert '412' in st['named'] and '412' not in st['launching']  # named only once the PR exists


def test_a_rerun_reuses_the_branch_and_the_pr(tmp_path, monkeypatch):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    run = Run(branch_exists=True, pr_exists=601)
    L.Launcher(g=gate(), run=run, git_dir=tmp_path, launch=lambda *a, **k: ('held', {})).launch(412, 'x')
    assert not any('commit-tree' in c or c[:3] == ['gh', 'pr', 'create'] for c in run.calls)


def test_a_failed_launch_frees_the_issue_for_the_picker(monkeypatch):
    slots.launch_mark([412])

    def boom(self, n, title):
        raise RuntimeError('gh pr create: no commits between main and the branch')
    monkeypatch.setattr(L.Launcher, 'launch', boom)
    with pytest.raises(RuntimeError):
        L.execute({'issue': 412, 'title': 't'})
    assert slots.load()['launching'] == {}


def test_the_picker_skips_a_queued_launch(tmp_path):
    from test_slots import issue_nodes
    snap = {'issues': issue_nodes([(412, board.BACKLOG), (413, board.BACKLOG)]), 'prs': []}
    got = slots.backlog_issues(snap, launching={'412': 1000.0}, now=1000 + 60)
    assert [i['number'] for i in got] == [413]
    got = slots.backlog_issues(snap, launching={'412': 1000.0}, now=1000 + slots.LAUNCHING_TTL)
    assert [i['number'] for i in got] == [412, 413]
