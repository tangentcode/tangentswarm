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
    return dict(number=n, repo=TS, head='h' * 40, item=f'I{n}', draft=False, closes=[], **kw)


def test_promote_fills_human_review_in_pick_order(monkeypatch):
    cols = {board.HUMAN_REVIEW: [pr(1)], board.READY: [pr(30), pr(20, due='2026-11-20', milestone='m'), pr(25)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    moved = []
    monkeypatch.setattr(board, 'move_card', lambda g, item, col, sh=None, b=None: moved.append((item, col, b.number)))
    assert boardflow.promote(adv, B6) == [20]  # one slot: the soonest due date first
    assert moved == [('I20', board.HUMAN_REVIEW, 6)]
    assert boardflow.promote(adv, board.DEFAULT) == []  # a staging board: the cascade's job


def test_minbot_repo_passes_a_clean_pr_to_ready_then_promotes(monkeypatch):
    from tangentswarm import advance as A
    cols = {board.AUTO_REVIEW: [pr(47)]}
    adv = Adv(cols)
    monkeypatch.setattr(boards, 'open_prs', lambda gql, b: adv.cols)
    head = 'h' * 40
    d = dict(codex_pr(head, clean=True), isDraft=False, mergeable='MERGEABLE', body='')
    monkeypatch.setattr(A, 'codex_details', lambda gql, n, repo=None: d)
    monkeypatch.setattr(A, 'check_runs', lambda h, repo=None: [{'name': 'tests', 'status': 'completed',
                                                               'conclusion': 'success'}])
    moved = []
    monkeypatch.setattr(board, 'move_card', lambda g, item, col, sh=None, b=None: moved.append((item, col)))
    assert boardflow.minbot_repo(adv, 47, TS)
    assert adv.minbots[0][:4] == ('evidence', 47, '--repo', TS) and moved[0] == ('I47', board.READY)
    d['mergeable'] = 'CONFLICTING'
    assert not boardflow.minbot_repo(adv, 47, TS) and 'conflicts with main' in adv.lines[-1]


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
