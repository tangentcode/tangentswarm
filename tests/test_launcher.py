"""tangentswarm.launcher (MethBot 2026-11-15 3:35 AM ET): the cascade launches the Backlog
issues it names (branch, draft PR "Fixes #N", board, brief, worker). Nothing consumed
START_CLAUDE before, so Platform In Progress sat empty."""
import json

import pytest

from tangentswarm import board, launcher as L, slots


class Run:
    """Fake subprocess.run: records argv, answers by command."""
    def __init__(self, branch_exists=False, pr_exists=None, pane_cmd='claude'):
        self.calls, self.branch_exists, self.pr_exists = [], branch_exists, pr_exists
        self.pane_cmd = pane_cmd

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
        elif argv[:2] == ['tmux', 'display-message']:
            out = self.pane_cmd

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
    """MethBot 3:55 AM ET: windows must read mn-<PR>-<short slug>, like mn-536-avo-save-icon."""
    assert L.slug('Board: mark right / wrong answers in the PTW quiz view!', 412) == 'board-mark-right-wrong'
    assert L.slug('Dictate on a non plain text field should insert at the cursor', 503) == \
        'dictate-plain-text-field'
    assert L.slug('', 7) == 'issue-7'


def test_a_launch_makes_the_branch_draft_pr_board_brief_and_worker(tmp_path, monkeypatch):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    run, started, held = Run(), [], []
    slots.launch_mark([412])

    def launch(branch, pr, brief):
        st = slots.load()  # Codex on PR #26: at every step the launch holds a slot somewhere
        held.append('412' in st['launching'] or str(pr) in st['unstarted'])
        started.append((branch, pr, brief))
        return ('launched', {'window': 'mn-601-x', 'pane': '%9'})
    lz = L.Launcher(g=gate(), run=run, git_dir=tmp_path, launch=launch)
    steps = lz.launch(412, 'Board marks right and wrong')
    assert held == [True]
    br = 'board-marks-right-wrong'
    assert ['git', '-C', str(tmp_path), 'push', '-q', 'origin', f"{'c' * 40}:refs/heads/{br}"] in run.calls
    create = [c for c in run.calls if c[:3] == ['gh', 'pr', 'create']][0]
    assert '--draft' in create and create[create.index('--body') + 1] == 'Fixes #412'
    edits = [c for c in run.calls if c[:3] == ['gh', 'project', 'item-edit']]
    assert [(e[e.index('--id') + 1], e[-1]) for e in edits] == [
        ('PRITEM', board.OPTION[board.IN_PROGRESS]), ('ISSUEITEM', board.OPTION[board.ACTIVE])]
    assert started == [(br, 601, str(tmp_path / 'briefs' / '601.txt'))]
    text = (tmp_path / 'briefs' / '601.txt').read_text()
    assert text.startswith('Task #412: Board marks right and wrong') and 'draft PR #601' in text
    assert steps[-1] == 'worker launched in mn-601-x (%9, claude runs)'
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


def test_the_launch_copies_the_issue_milestone_to_the_pr(tmp_path, monkeypatch):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    run = Run()
    real = run.__call__

    def answer(argv, **k):
        r = real(argv, **k)
        if argv[:3] == ['gh', 'api', 'repos/tangentcode/platform/issues/412']:
            r.stdout = '7'
        return r
    L.Launcher(g=gate(), run=answer, git_dir=tmp_path, launch=lambda *a, **k: ('held', {})).launch(412, 't')
    assert ['gh', 'api', '-X', 'PATCH', 'repos/tangentcode/platform/issues/601', '-F', 'milestone=7'] in run.calls


def test_stale_named_entries_without_a_draft_pr_are_dropped():
    """2026-11-15: #399-#431, #502, #503 stayed "named" from passes that never launched."""
    slots.save(dict(slots.load(), named={'399': 1000.0, '412': 1000.0, '502': 5000.0}))
    assert slots.prune_named(taken={412}, now=5300.0) == [399]  # #502 is fresh: its PR may lag
    assert set(slots.load()['named']) == {'412', '502'}


def test_a_launch_is_done_only_when_claude_runs_in_the_pane(tmp_path, monkeypatch):
    """MethBot 3:55 AM ET: #537-#542 got a window with only a bash prompt."""
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    run = Run(pane_cmd='bash')
    lz = L.Launcher(g=gate(), run=run, git_dir=tmp_path,
                    launch=lambda branch, pr, brief: ('launched', {'window': 'mn-601-x', 'pane': '%9'}))
    with pytest.raises(RuntimeError, match='no claude runs in %9 after the start .it runs bash.'):
        lz.launch(412, 'Board marks right and wrong')
    rec = slots.load()['unstarted']['601']
    assert rec['branch'] == 'board-marks-right-wrong' and rec['issue'] == 412  # retried by the next pass
    run.pane_cmd = 'claude'
    assert lz.start_existing(601, rec).endswith('claude runs)')
    assert slots.load()['unstarted'] == {}


def test_a_held_start_is_not_a_failure(tmp_path, monkeypatch):
    lz = L.Launcher(g=gate(), run=Run(), git_dir=tmp_path, launch=lambda *a, **k: ('held', {'window': 'w'}))
    assert lz.start('b', 601, '/brief').startswith('worker held (memory)')


def test_swarm_start_scoped_runs_without_the_daemon_pythonpath():
    """03:44-03:51 ET: the daemon's PYTHONPATH made swarm-start-scoped load this checkout's
    tangentswarm.registry, which lacks first_prompt_problem; every start crashed."""
    from tangentswarm import launches
    env = launches.clean_env({'PYTHONPATH': '/x/flow-live', 'PATH': '/bin'})
    assert env == {'PATH': '/bin'}


def test_the_cascade_retries_unstarted_prs_before_it_names_new_issues(monkeypatch):
    from tangentswarm import advance as A, spool
    from test_advance import CascadeGate
    slots.unstarted_add(537, branch='dictate-plain-text-field', brief='/b/537.txt', issue=503, now=1)
    slots.unstarted_add(538, branch='minavo-open-file', brief='/b/538.txt', issue=415, now=2)
    monkeypatch.setattr(slots, 'live_claudes', lambda: 8)
    out = []
    A.Cascade(g=CascadeGate(issues=[(40, board.BACKLOG)]), out=out.append).signal({})
    reqs = sorted(json.loads(p.read_text())['pr'] for p in spool.SPOOL.glob('*.req.json'))
    assert reqs == [537, 538]  # both fit under the cap of 10
    assert not any(l.startswith('START_CLAUDE') for l in out)  # no room left for new issues
    assert 'start: queued the worker start of #537 (dictate-plain-text-field): its window has no Claude' in out


def test_a_setup_failure_after_the_draft_pr_is_recovered_by_the_start_retry(tmp_path, monkeypatch):
    """Codex on PR #26: a launch that failed after the draft PR but before unstarted_add was
    stranded (the picker skips the issue, and retry_starts had no record)."""
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    started = []
    lz = L.Launcher(g=gate(), run=Run(), git_dir=tmp_path,
                    launch=lambda branch, pr, brief: started.append(pr) or ('launched', {'window': 'w', 'pane': '%9'}))
    slots.launch_mark([412])
    monkeypatch.setattr(L.Launcher, 'board', lambda self, n, pr: (_ for _ in ()).throw(RuntimeError('rate limited')))
    with pytest.raises(RuntimeError):
        lz.launch(412, 'Board marks right and wrong')
    rec = slots.load()['unstarted']['601']
    assert rec['setup'] == 'pending' and rec['queued'] > 0 and '412' not in slots.load()['launching']
    monkeypatch.undo()
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    note = lz.start_existing(601, rec)
    assert started == [601] and 'claude runs' in note and '601' not in slots.load()['unstarted']
    assert (tmp_path / 'briefs' / '601.txt').exists()


def test_a_failed_brief_send_keeps_the_record_and_the_retry_sends_only_the_brief(tmp_path, monkeypatch):
    """Codex on PR #26: a Claude that ran without its brief line counted as launched, and
    nothing sent the brief again."""
    from tangentswarm import brief, launches
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path / 'briefs')
    monkeypatch.setattr(brief, 'write_brief', lambda n: None)
    lz = L.Launcher(g=gate(), run=Run(), git_dir=tmp_path,
                    launch=lambda branch, pr, brief: ('launched', {'window': 'w', 'pane': '%9', 'unit': 'u.scope',
                                                                   'brief_error': 'prompt not ready'}))
    steps = lz.launch(412, 'Board marks right and wrong')
    assert 'the start retry sends it' in steps[-1]
    rec = slots.load()['unstarted']['601']
    assert rec['brief_pane'] == '%9' and rec['unit'] == 'u.scope' and rec['brief'].endswith('601.txt')
    sent = []
    monkeypatch.setattr(launches, 'send_brief', lambda pane, unit, path: sent.append((pane, unit, path)))
    lz._launch = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no second worker start'))
    note = lz.start_existing(601, rec)
    assert sent == [('%9', 'u.scope', rec['brief'])] and 'brief line sent' in note
    assert '601' not in slots.load()['unstarted']


def test_a_branch_is_reused_only_for_an_exact_closing_reference():
    """Codex on PR #26: issue #50 reused the branch of a PR whose body says "Fixes #507"."""
    assert L.closes('Fixes #50', 50) and L.closes('closes tangentcode/platform#50.', 50)
    assert L.closes('Some text\nResolves #50\n', 50)
    assert not L.closes('Fixes #507', 50) and not L.closes('see #50', 50) and not L.closes('', 50)


def test_a_pending_brief_is_retried_even_at_the_claude_cap(monkeypatch):
    """Codex on PR #26: the cap check stopped the retry of a brief to a Claude that runs."""
    from tangentswarm import advance as A, spool
    from test_advance import CascadeGate
    slots.unstarted_add(537, branch='a', brief='/b/537.txt', issue=503, now=1)
    slots.brief_pending(539, '%9', 'u.scope', '/b/539.txt', branch='c', now=2)
    monkeypatch.setattr(slots, 'live_claudes', lambda: slots.MAX_CLAUDES)
    out = []
    A.Cascade(g=CascadeGate(issues=[(40, board.BACKLOG)]), out=out.append).signal({})
    reqs = sorted(json.loads(p.read_text())['pr'] for p in spool.SPOOL.glob('*.req.json'))
    assert reqs == [539]  # the brief line, not the new worker start of #537
    assert 'start: queued the brief line of #539 (c)' in out
