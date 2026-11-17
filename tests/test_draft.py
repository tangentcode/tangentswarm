"""tangentswarm #95: every server move into In Progress converts the PR to a draft."""
import subprocess

from tangentswarm import board, flow as F, mainmerge as M, watchdog as WD

PLATFORM = 'tangentcode/platform'
B2 = board.Board(9, 'P9', 'F9', {c: f'o-{c}' for c in board.COLUMNS}, ['o/a', 'o/b'], False)


def _gate():
    return {'OPT': {}, 'project_id': lambda: 'P', 'STATUS_FIELD': 'S'}


# -- board.move_card ----------------------------------------------------------------------

def test_a_pr_move_into_in_progress_makes_it_a_draft():
    calls = []
    board.move_card(_gate(), 'I7', board.IN_PROGRESS, calls.append, n=7)
    assert calls[0][:3] == ['gh', 'project', 'item-edit']
    assert board.DRAFTED == [(7, PLATFORM)]  # the default board's only repo


def test_other_columns_issues_and_moves_with_no_number_stay_as_they_are():
    calls = []
    for col in (board.AUTO_REVIEW, board.READY, board.STAGING, board.NEEDS_INPUT, board.BACKLOG):
        board.move_card(_gate(), 'I7', col, calls.append, n=7)
    board.move_card(_gate(), 'I7', board.IN_PROGRESS, calls.append)              # no PR number
    board.move_card(_gate(), 'I7', board.IN_PROGRESS, calls.append, n=7, kind='issue')
    assert board.DRAFTED == [] and len(calls) == 7


def test_the_repo_comes_from_the_caller_on_a_board_with_more_than_one_repo():
    calls = []
    board.move_card({}, 'I3', board.IN_PROGRESS, calls.append, n=3, b=B2, repo='o/b')
    assert board.DRAFTED == [(3, 'o/b')]
    board.move_card({}, 'I3', board.IN_PROGRESS, calls.append, n=3, b=B2)
    assert board.DRAFTED[-1] == (3, None)  # make_draft logs the missing repo; the move stands


def test_a_failed_move_does_not_draft():
    def boom(argv):
        raise RuntimeError('gh down')
    try:
        board.move_card(_gate(), 'I7', board.IN_PROGRESS, boom, n=7)
    except RuntimeError:
        pass
    assert board.DRAFTED == []


# -- board.make_draft (the real one, with a fake gh) --------------------------------------

def test_make_draft_runs_gh_pr_ready_undo():
    calls = []
    assert board.REAL_MAKE_DRAFT(447, PLATFORM, calls.append) is True
    assert calls == [['gh', 'pr', 'ready', '447', '-R', PLATFORM, '--undo']]


def test_make_draft_never_raises():
    def boom(argv):
        raise RuntimeError('gh down')
    assert board.REAL_MAKE_DRAFT(447, PLATFORM, boom) is False
    bad = lambda argv: subprocess.CompletedProcess(argv, 1, '', 'HTTP 502')
    assert board.REAL_MAKE_DRAFT(447, PLATFORM, bad) is False
    calls = []
    assert board.REAL_MAKE_DRAFT(447, None, calls.append) is False and calls == []


def test_make_draft_without_sh_uses_tick_sh(monkeypatch):
    from tangentswarm import tick
    ran = []
    monkeypatch.setattr(tick, 'sh', lambda argv, check=False: ran.append((argv, check)))
    assert board.REAL_MAKE_DRAFT(5, PLATFORM) is True
    assert ran == [(['gh', 'pr', 'ready', '5', '-R', PLATFORM, '--undo'], True)]


# -- tangentswarm #134: a draft is never merge-ready ---------------------------------------

def test_make_draft_also_removes_merge_ready_and_pre_merging():
    """platform #528: rejected into In Progress, made a draft, and kept merge-ready."""
    calls = []
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, calls.append) is True
    assert calls == [['gh', 'pr', 'ready', '528', '-R', PLATFORM, '--undo']]
    assert F.LABELS_WRITTEN == [(528, 'remove', [F.MERGE_READY]), (528, 'remove', [F.PRE_MERGING])]


def test_the_labels_go_to_the_prs_own_repo(monkeypatch):
    seen = []
    monkeypatch.setattr(F, 'label_write', lambda n, op, names, repo=F.REPO: seen.append((n, op, names, repo)))
    assert board.REAL_MAKE_DRAFT(9, 'o/b', lambda argv: None) is True
    assert seen == [(9, 'remove', [F.MERGE_READY], 'o/b'), (9, 'remove', [F.PRE_MERGING], 'o/b')]


def test_the_labels_go_also_when_the_draft_conversion_fails():
    def boom(argv):
        raise RuntimeError('gh down')
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, boom) is False
    assert [w[2] for w in F.LABELS_WRITTEN] == [[F.MERGE_READY], [F.PRE_MERGING]]


def test_a_label_the_pr_does_not_carry_is_not_an_error(monkeypatch):
    monkeypatch.setattr(F, 'label_write', lambda n, op, names, repo=F.REPO: "HTTP 404: Label does not exist")
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, lambda argv: None) is True
    monkeypatch.setattr(F, 'label_write', lambda n, op, names, repo=F.REPO: "'merge-ready' not found")
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, lambda argv: None) is True


def test_a_failed_label_removal_is_logged_and_never_raises(monkeypatch):
    from tangentswarm import eventdb
    logged = []
    monkeypatch.setattr(eventdb, 'log_event', lambda kind, **kw: logged.append((kind, kw)))
    monkeypatch.setattr(F, 'label_write', lambda n, op, names, repo=F.REPO: 'HTTP 502')
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, lambda argv: None) is False
    drop = [kw for kind, kw in logged if kw.get('action') == 'drop-labels']
    assert len(drop) == 1 and 'merge-ready: HTTP 502' in drop[0]['error'] and 'pre-merging' in drop[0]['error']

    def raises(n, op, names, repo=F.REPO):
        raise OSError('no minbot')
    monkeypatch.setattr(F, 'label_write', raises)
    assert board.REAL_MAKE_DRAFT(528, PLATFORM, lambda argv: None) is False


def test_no_repo_means_no_label_write():
    assert board.REAL_MAKE_DRAFT(528, None, lambda argv: None) is False
    assert F.LABELS_WRITTEN == []


def test_the_webhook_reject_drafts_through_make_draft(monkeypatch):
    """The #528 path: webhook draft() goes through board.make_draft, so it drops the labels."""
    from tangentswarm import webhook as W
    monkeypatch.setattr(board, 'make_draft', board.REAL_MAKE_DRAFT)
    calls = []
    x = W.Executor(g=_gate(), sh=calls.append)
    x.draft(528)
    assert x.done == ['#528 is a draft']
    assert calls == [['gh', 'pr', 'ready', '528', '-R', x.repo, '--undo']]
    assert F.LABELS_WRITTEN == [(528, 'remove', [F.MERGE_READY]), (528, 'remove', [F.PRE_MERGING])]


# -- the movers ---------------------------------------------------------------------------

def _no_gh_move(monkeypatch):
    moved = []
    monkeypatch.setattr(board, '_move', lambda g, item, col, sh=None, b=None: moved.append((item, col)))
    return moved


def test_the_pull_sends_a_conflicting_staging_pr_back_as_a_draft(monkeypatch):
    """The #95 case: platform #447 went Staging -> In Progress and stayed ready for review."""
    moved = _no_gh_move(monkeypatch)
    f = F.Flow(dry_run=False, out=lambda l: None, g=_gate())
    f.give_worker = lambda n, text, priority=False, queue=False: (0, 'sent')
    p = dict(number=447, head='h' * 40, item='I447', col=board.STAGING)
    assert f.fail(p, 'conflicts with main') is True
    assert moved == [('I447', board.IN_PROGRESS)] and board.DRAFTED == [(447, PLATFORM)]


def test_the_pull_with_no_worker_still_moves_and_drafts(monkeypatch):
    """tangentswarm #101 (drop parking): with no worker the card goes to In Progress anyway,
    as a draft, and waits there for a Claude."""
    moved = _no_gh_move(monkeypatch)
    f = F.Flow(dry_run=False, out=lambda l: None, g=_gate())
    f.give_worker = lambda n, text, priority=False, queue=False: (3, 'no slot')
    assert f.fail(dict(number=447, head='h' * 40, item='I447', col=board.STAGING), 'conflicts') is True
    assert moved == [('I447', board.IN_PROGRESS)] and board.DRAFTED == [(447, PLATFORM)]


def test_the_main_merge_recheck_gives_the_mover_the_pr_number(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'RELAY_DIR', tmp_path, raising=False)
    moved = _no_gh_move(monkeypatch)
    staging = [dict(number=12, head='a' * 40, item='I12')]
    out = M.recheck(staging, 'm' * 40, lambda m, h: False,
                    lambda item, col, n=None: board.move_card(_gate(), item, col, n=n), lambda s: None,
                    cache_path=tmp_path / 'c', give=lambda p, text, path: (True, 'sent'))
    assert out == [12] and moved == [('I12', board.IN_PROGRESS)] and board.DRAFTED == [(12, PLATFORM)]


def test_the_watchdog_sweep_drafts_an_open_pr_it_takes_out_of_done(monkeypatch):
    moved = _no_gh_move(monkeypatch)
    w = WD.Watchdog(g=_gate(), panes=type('P', (), {'latest': {}})(), out=lambda line: None,
                    alert=lambda *a, **k: True, incident=lambda *a, **k: None)
    monkeypatch.setattr('tangentswarm.alerts.record', lambda *a, **k: None)
    w.sweep_move(31, 'I31', board.DONE, board.IN_PROGRESS, 'an open PR in Done')
    w.sweep_move(32, 'I32', board.IN_PROGRESS, board.BACKLOG, 'an issue card with no PR')
    assert moved == [('I31', board.IN_PROGRESS), ('I32', board.BACKLOG)]
    assert board.DRAFTED == [(31, PLATFORM)]
