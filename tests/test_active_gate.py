"""bin/active-gate: the Active sweep and --promote-missing, with no network (tangentswarm #140).

Every GitHub read and write is a stub: project_items, fetch_issue, move and add_to_project
are replaced, and gql / sh raise if anything reaches them.
"""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'bin' / 'active-gate'


class Labels:
    """A stand-in for tangentswarm.labels: every issue is assigned, label sync does nothing."""

    def assigned_by_card(self, items):
        return True, 'card in Active'

    class Sync:
        def __init__(self, **kw):
            pass

        def main(self, linked):
            return 0


def pr_item(num, col):
    return {'id': f'PRITEM{num}', 'type': 'PULL_REQUEST', 'fieldValueByName': {'name': col},
            'content': {'__typename': 'PullRequest', 'number': num, 'title': f'pr {num}',
                        'state': 'OPEN', 'id': f'PR{num}'}}


def issue_item(num, col):
    return {'id': f'ITEM{num}', 'type': 'ISSUE', 'fieldValueByName': {'name': col},
            'content': {'__typename': 'Issue', 'number': num, 'title': f'issue {num}',
                        'state': 'OPEN', 'id': f'I{num}'}}


def issue(num, col='Active', prs=(), subs=(), labels=(), state='OPEN'):
    return {
        'number': num, 'title': f'issue {num}', 'state': state, 'id': f'I{num}',
        'labels': {'nodes': [{'name': l} for l in labels]},
        'projectItems': {'nodes': [{'id': f'ITEM{num}', 'project': {'number': 4},
                                    'fieldValueByName': {'name': col}}]},
        'subIssues': {'nodes': [{'number': n, 'state': st} for n, st in subs],
                      'pageInfo': {'hasNextPage': False}},
        'closedByPullRequestsReferences': {'nodes': [{'number': n, 'state': 'OPEN'} for n in prs]},
        'timelineItems': {'nodes': []},
    }


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """bin/active-gate loaded as a module, with a fake board and a record of card moves."""
    loader = importlib.machinery.SourceFileLoader('active_gate_under_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    g = importlib.util.module_from_spec(spec)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    loader.exec_module(g)
    g.LOCK = str(tmp_path / 'active-gate.lock')
    g.LOG = str(tmp_path / 'active-gate.log')
    g.items, g.issues, g.moves = [], {}, []

    def no_network(*a, **k):
        raise AssertionError(f'network call in a test: {a}')

    g.gql = no_network
    g.sh = no_network
    g._labels = lambda: Labels()
    g.project_items = lambda: list(g.items)
    g.fetch_issue = lambda n: g.issues[n]
    g.move = lambda item, status: g.moves.append((item, status))
    g.add_to_project = no_network
    return g


def board(g, issues, prs):
    """issues: {num: (col, issue dict)}; prs: {num: col}."""
    g.items = [issue_item(n, col) for n, (col, _) in issues.items()]
    g.items += [pr_item(n, col) for n, col in prs.items()]
    g.issues = {n: iss for n, (_, iss) in issues.items()}


def test_deferred_option_id(gate):
    assert gate.OPT['Deferred'] == '0837891f'
    assert gate.col_of({'optionId': '0837891f', 'name': 'Later'}) == 'Deferred'


def test_all_linked_prs_deferred_moves_issue_to_deferred(gate, capsys):
    board(gate, {471: ('Active', issue(471, prs=[486]))}, {486: 'Deferred'})
    assert gate.main(['--sweep', '--demote']) == 1
    assert gate.moves == [('ITEM471', 'Deferred')]
    out = capsys.readouterr().out
    assert 'all linked open PRs are in Deferred: #486' in out
    assert '-> moved to Deferred (deferred)' in out
    assert 'needs-launch' not in out


def test_two_deferred_prs_move_issue_to_deferred(gate):
    board(gate, {471: ('Active', issue(471, prs=[486, 487]))}, {486: 'Deferred', 487: 'Deferred'})
    gate.main(['--sweep', '--demote'])
    assert gate.moves == [('ITEM471', 'Deferred')]


def test_development_link_to_deferred_pr_counts(gate):
    iss = issue(471)
    pr = {'__typename': 'PullRequest', 'number': 486, 'state': 'OPEN'}
    iss['timelineItems']['nodes'] = [{'__typename': 'ConnectedEvent', 'createdAt': '2026-11-17T10:00:00Z',
                                      'source': pr, 'subject': {'__typename': 'Issue', 'number': 471}}]
    board(gate, {471: ('Active', iss)}, {486: 'Deferred'})
    gate.main(['--sweep', '--demote'])
    assert gate.moves == [('ITEM471', 'Deferred')]


def test_one_deferred_and_one_in_progress_pr_stays_active(gate, capsys):
    board(gate, {471: ('Active', issue(471, prs=[486, 490]))}, {486: 'Deferred', 490: 'In Progress'})
    assert gate.main(['--sweep', '--demote']) == 0
    assert gate.moves == []
    rows, ok, kind = gate.eval_issue(gate.issues[471], gate.pr_board_from_items(gate.items))
    assert ok and kind == 'ok'
    assert 'ALL PASS' in capsys.readouterr().out


def test_no_linked_pr_stays_needs_launch(gate, capsys):
    board(gate, {471: ('Active', issue(471))}, {})
    gate.main(['--sweep', '--demote'])
    assert gate.moves == []
    out = capsys.readouterr().out
    assert 'needs-launch (no linked PR; leaving in Active)' in out
    _, _, kind = gate.eval_issue(gate.issues[471], {})
    assert kind == 'unlinked'


def test_deferred_and_unboarded_pr_is_not_deferred(gate):
    """A linked PR with no Project 4 card is not in Deferred: the issue keeps needs-launch."""
    board(gate, {471: ('Active', issue(471, prs=[486, 488]))}, {486: 'Deferred'})
    gate.main(['--sweep', '--demote'])
    assert gate.moves == []
    _, _, kind = gate.eval_issue(gate.issues[471], gate.pr_board_from_items(gate.items))
    assert kind == 'unlinked'


def test_deferred_pr_with_open_sub_issue_stays_active(gate):
    board(gate, {471: ('Active', issue(471, prs=[486], subs=[(500, 'OPEN')]))}, {486: 'Deferred'})
    assert gate.main(['--sweep', '--demote']) == 0
    assert gate.moves == []


def test_parked_issue_with_deferred_pr_is_untouched(gate, capsys):
    board(gate, {344: ('Active', issue(344, prs=[486]))}, {486: 'Deferred'})
    gate.main(['--sweep', '--demote'])
    assert gate.moves == []
    assert 'parked; leaving in Active' in capsys.readouterr().out


def test_sweep_without_demote_moves_nothing(gate):
    board(gate, {471: ('Active', issue(471, prs=[486]))}, {486: 'Deferred'})
    assert gate.main(['--sweep']) == 1
    assert gate.moves == []


def test_no_board_read_never_counts_deferred(gate):
    """In-process callers that pass no pr_board (swarm-tick --dry-run) keep the old kinds."""
    _, _, kind = gate.eval_issue(issue(471, prs=[486]), None)
    assert kind == 'ok'
    assert gate.deferred_pr_nums([486], None) == []
    assert gate.deferred_pr_nums([], {}) == []


def test_demote_status_routes(gate):
    assert gate.demote_status('deferred') == 'Deferred'
    assert gate.demote_status('unlinked') is None
    assert gate.demote_status('pending') is None
    assert gate.demote_status('closed') == 'Done'


@pytest.mark.parametrize('col', ['Active', 'Backlog', 'In Progress'])
def test_promote_missing_never_promotes_a_deferred_issue(gate, capsys, col):
    board(gate, {471: (col, issue(471, col=col, prs=[486]))}, {486: 'Deferred'})
    assert gate.main(['--promote-missing']) == 0
    assert gate.moves == []
    assert '#471  skip' in capsys.readouterr().out


def test_promote_missing_still_promotes_a_flow_pr_issue(gate):
    board(gate, {471: ('Backlog', issue(471, col='Backlog', prs=[490]))}, {490: 'In Progress'})
    gate.main(['--promote-missing'])
    assert gate.moves == [('ITEM471', 'Active')]
