"""tangentswarm #45 (Michal 2026-11-15 7:20 PM ET): the same flow on Project 6 with the
tangentswarm and methodology repos (no staging site)."""
import json

from tangentswarm import board, boardflow, boards, webhook as W
from test_webhook import Fake, codex_pr

TS = 'tangentcode/tangentswarm'
B6 = next(b for b in board.BOARDS if b.number == 6)


def test_the_boards_come_from_config():
    assert board.DEFAULT.number == 4 and board.DEFAULT.repos == ('tangentcode/platform',) and board.DEFAULT.staging
    assert B6.repos == (TS, 'tangentcode/methodology') and not B6.staging
    assert board.for_repo(TS) is B6 and board.for_repo('tangentcode/other') is None
    assert board.for_project(B6.id) is B6 and board.column({'optionId': B6.option[board.READY]}) == board.READY
    calls = []
    board.move_card({}, 'I9', board.HUMAN_REVIEW, calls.append, b=B6)
    assert calls == [['gh', 'project', 'item-edit', '--id', 'I9', '--project-id', B6.id, '--field-id',
                      B6.status_field, '--single-select-option-id', B6.option[board.HUMAN_REVIEW]]]


def test_a_tangentswarm_event_runs_the_same_rules_with_its_repo():
    p = {'action': 'opened', 'repository': {'full_name': TS}, 'pull_request': {'number': 47, 'draft': True,
                                                                              'base': {'ref': 'main'}}}
    (a,) = W.classify('pull_request', p)
    assert a.kind == 'card' and a.args['repo'] == TS


class Fake6(Fake):
    def card(self, n):
        return ('ITEM6', self.col)


def test_another_repo_queues_its_own_minbot_check_and_board_promote_and_wakes_for_a_relay():
    ex = Fake6(col=board.AUTO_REVIEW, full=codex_pr(clean=True))
    ex.run(W.Action('codex', 47, args={'repo': TS}))
    assert ex.queued == [['--minbot', '47', '--repo', TS], ['--promote-board', '6']] and ex.cascades == []
    ex = Fake6(col=board.AUTO_REVIEW, full=codex_pr(findings=True))
    ex.give = None
    ex.run(W.Action('codex', 47, args={'repo': TS}))
    assert ex.relays == [] and ex.calls == [] and 'no worker relay for tangentcode/tangentswarm' in ex.wakes[0]['why']


def test_a_project_6_card_move_carries_its_board():
    p = {'action': 'edited', 'sender': {'login': 'tangentstorm'},
         'projects_v2_item': {'content_node_id': 'PR_x', 'project_node_id': B6.id},
         'changes': {'field_value': {'field_node_id': B6.status_field, 'from': {'id': B6.option[board.HUMAN_REVIEW]},
                                     'to': {'id': B6.option[board.DONE]}}}}
    (a,) = W.classify('projects_v2_item', p)
    assert a.kind == 'cascade' and a.args['board'] == 6


import pytest


@pytest.fixture
def clean_now(monkeypatch):
    """promote() re-reads each candidate: by default the same head, base main, Codex clean."""
    from tangentswarm import advance as A
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: dict(
        codex_pr('h' * 40, clean=True), isDraft=False, mergeable='MERGEABLE', body='', baseRefName='main'))
    monkeypatch.setattr(A, 'check_runs', lambda h, repo=None: [{'name': 't', 'status': 'completed',
                                                               'conclusion': 'success'}])


class Adv:
    def __init__(self, cols, dry=False):
        self.dry, self.lines, self.moves, self.minbots = dry, [], [], []
        self.g = {'gql': lambda q, **v: None}
        self.cols = cols

    def out(self, line):
        self.lines.append(line)

    def minbot(self, *a):
        self.minbots.append(a)


def pr(n, **kw):
    kw.setdefault('mergeable', 'MERGEABLE')
    return dict(number=n, repo=TS, head='h' * 40, item=f'I{n}', draft=False, closes=[], **kw)


def test_promote_fills_human_review_in_pick_order(monkeypatch, clean_now):
    cols = {board.HUMAN_REVIEW: [pr(1)], board.READY: [pr(30), pr(20, due='2026-11-20', milestone='m'), pr(25)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    moved = []
    monkeypatch.setattr(board, 'move_card', lambda g, item, col, sh=None, n=None, b=None: moved.append((item, col, b.number)))
    monkeypatch.setattr(board, 'active_fast_track', lambda path=None: 30)  # a platform ticket number
    assert boardflow.promote(adv, B6) == [20]  # one slot: the soonest due date first, not platform's #30
    assert moved == [('I20', board.HUMAN_REVIEW, 6)]
    assert boardflow.promote(adv, board.DEFAULT) == []  # a staging board: the cascade's job


def test_minbot_repo_passes_a_clean_pr_to_ready_then_promotes(monkeypatch):
    from tangentswarm import advance as A
    cols = {board.AUTO_REVIEW: [pr(47)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    head = 'h' * 40
    d = dict(codex_pr(head, clean=True), isDraft=False, mergeable='MERGEABLE', body='', baseRefName='main')
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: d)
    monkeypatch.setattr(A, 'check_runs', lambda h, repo=None: [{'name': 'tests', 'status': 'completed',
                                                               'conclusion': 'success'}])
    moved = []
    monkeypatch.setattr(board, 'move_card', lambda g, item, col, sh=None, n=None, b=None: moved.append((item, col)))
    assert boardflow.minbot_repo(adv, 47, TS)
    assert adv.minbots[0][:4] == ('evidence', 47, '--repo', TS) and moved[0] == ('I47', board.READY)
    d['mergeable'] = 'CONFLICTING'
    assert not boardflow.minbot_repo(adv, 47, TS) and 'conflicts with main' in adv.lines[-1]
    d['mergeable'] = 'UNKNOWN'  # Codex on PR #47: not confirmed is not a pass
    assert not boardflow.minbot_repo(adv, 47, TS) and 'not confirmed' in adv.lines[-1]


def test_every_read_and_cleanup_of_another_repo_uses_that_repo(monkeypatch):
    """Codex on PR #47: do_codex read the same-number platform PR; a tangentswarm close
    cleared a platform PR's launch records."""
    from tangentswarm import advance, launches, slots
    seen = []
    real = advance.codex_details
    monkeypatch.setattr(advance, 'codex_details', lambda gql, n, repo=None: seen.append(repo) or real(gql, n, repo))
    ex = Fake6(col=board.AUTO_REVIEW, full=codex_pr(clean=True))
    ex.run(W.Action('codex', 47, args={'repo': TS}))
    assert seen and set(seen) == {TS}
    slots.unstarted_add(47, branch='platform-47', brief='', issue=1)
    launches.hold('platform-47', pr=47)
    Fake6(col=board.IN_PROGRESS).run(W.Action('closed', 47, args={'repo': TS}))
    assert '47' in slots.load()['unstarted'] and launches.read_queue()


def test_a_project_6_pr_is_fast_track_by_its_own_labels(monkeypatch, clean_now):
    cols = {board.READY: [pr(30), pr(40, ft=True)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    assert boardflow.promote(adv, B6) == [40, 30]


def test_same_number_findings_in_two_repos_both_wake():
    """Codex on PR #47: the wake key had no repo, so the second repo's handoff was lost."""
    wakes = []
    for repo in (TS, 'tangentcode/methodology'):
        ex = Fake6(col=board.AUTO_REVIEW, full=codex_pr(findings=True))
        ex.give, ex.wake_post = None, wakes.append
        ex.run(W.Action('codex', 47, args={'repo': repo}))
    keys = [k for k in W._seen()['wake'] if k.startswith('tangentcode/')]
    assert sorted(k.split(':')[0] for k in keys) == ['tangentcode/methodology', TS]  # both kept, none deduped
    assert wakes[0]['why'].startswith(TS + ':')


def test_a_ready_pr_that_conflicts_now_is_not_promoted(monkeypatch, clean_now):
    """Codex on PR #47: a merge after its MinBot pass made it conflict with main."""
    cols = {board.READY: [pr(20, due='2026-11-20', milestone='m', mergeable='CONFLICTING'), pr(30)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    from tangentswarm import advance as A
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: dict(
        codex_pr('h' * 40, clean=True), baseRefName='main', mergeable='CONFLICTING' if n == 20 else 'MERGEABLE'))
    assert boardflow.promote(adv, B6) == [30] and any('#20 stays in Ready: conflicts' in l for l in adv.lines)


def test_a_busy_lock_requeues_the_repo_and_board_arguments(monkeypatch, tmp_path):
    """Codex on PR #47: a requeue lost --repo and --promote-board."""
    from tangentswarm import advance as A, gatelock

    class Busy:
        def __init__(self, name):
            pass

        def __enter__(self):
            raise gatelock.Busy('held by hr-gate')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(gatelock, 'gate_lock', Busy)
    monkeypatch.setattr(A, 'Advance', lambda **k: None)
    queued = []
    monkeypatch.setattr(A, 'queue_self', lambda args=(), qdir=None: queued.append(list(args)) or 'queued')
    A.main(['--minbot', '47', '--repo', TS])
    A.main(['--promote-board', '6'])
    assert queued == [['--minbot', '47', '--repo', TS], ['--promote-board', '6']]


def test_a_board_pass_retries_unknown_mergeability(monkeypatch, tmp_path, clean_now):
    """Codex on PR #47: a promote that met UNKNOWN had no retry; the daemon queues a board
    pass every BOARD_EVERY s, and the pass checks Auto Review and promotes."""
    from tangentswarm import gatelock, promoted as P
    queued = []
    monkeypatch.setattr(gatelock, 'enqueue', lambda argv, by='', qdir=None: queued.append(argv[1:]))
    monkeypatch.setattr(P, 'BOARD_EVERY', 300)
    p = P.Promoter.__new__(P.Promoter)
    assert p.board_passes(now=1000) == [6] and queued == [['--promote-board', '6', '--board-pass']]
    assert p.board_passes(now=1000 + 10) == []  # not again before BOARD_EVERY
    cols = {board.AUTO_REVIEW: [pr(47)], board.READY: [pr(30)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    checked = []
    monkeypatch.setattr(boardflow, 'minbot_repo', lambda a, n, repo, promote_after=True: checked.append((n, repo, promote_after)))
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    assert boardflow.board_pass(adv, B6) == [30] and checked == [(47, TS, False)]  # promote once, after all checks


def test_a_ready_pr_with_red_ci_now_is_not_promoted(monkeypatch):
    """Codex on PR #47: a CI rerun failed after the PR reached Ready."""
    from tangentswarm import advance as A
    cols = {board.READY: [pr(20, due='2026-11-20', milestone='m'), pr(30)]}
    cols[board.READY][0]['head'] = 'r' * 40
    adv = Adv(cols)
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: dict(
        codex_pr('r' * 40 if n == 20 else 'h' * 40, clean=True), baseRefName='main', mergeable='MERGEABLE'))
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    monkeypatch.setattr(A, 'check_runs', lambda h, repo=None: [{'name': 't', 'status': 'completed',
                                                               'conclusion': 'failure' if h[0] == 'r' else 'success'}])
    assert boardflow.promote(adv, B6) == [30] and any('#20 stays in Ready: CI is not green' in l for l in adv.lines)


def test_promote_and_the_pass_need_base_main_and_codex_clean_now(monkeypatch):
    """Codex on PR #47: a PR on another base, or with findings after its pass, stays."""
    from tangentswarm import advance as A
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    adv = Adv({board.READY: [pr(20, due='2026-11-20', milestone='m'), pr(30)]})
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    det = {20: dict(codex_pr('h' * 40, findings=True), baseRefName='main', mergeable='MERGEABLE'),
           30: dict(codex_pr('h' * 40, clean=True), baseRefName='main', mergeable='MERGEABLE')}
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: det[n])
    assert boardflow.promote(adv, B6) == [30] and any('#20 stays in Ready: not Codex-clean' in l for l in adv.lines)
    det[30] = dict(det[30], baseRefName='deploy')
    adv.lines.clear()
    assert boardflow.promote(adv, B6) == [] and any('targets deploy, not main' in l for l in adv.lines)


def test_promote_runs_the_same_gate_as_the_minbot_check(monkeypatch, clean_now):
    """Codex on PR #47: an open question for Michal added in Ready must keep the PR there."""
    from tangentswarm import advance as A
    monkeypatch.setattr(board, 'move_card', lambda *a, **k: None)
    adv = Adv({board.READY: [pr(30)]})
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: dict(
        codex_pr('h' * 40, clean=True), baseRefName='main', mergeable='MERGEABLE',
        body='## Question for Michal\nOld or new colors?'))
    assert boardflow.promote(adv, B6) == [] and any('open question for Michal' in l for l in adv.lines)
