"""swarm-label-sync and the retired bot labels (Michal 2026-11-14 7:21-7:23 PM ET)."""
import io
import json

import pytest

from tangentswarm import board
from tangentswarm import labels as L

HUMAN = {'__typename': 'User', 'login': 'tangentstorm'}
MINBOT = {'__typename': 'Bot', 'login': 'tangentcode-minbot'}


def issue(n, labels=(), by=None, milestone=None):
    by = by or {}
    return {'number': n, 'labels': {'nodes': [{'name': x} for x in labels]}, 'milestone': milestone,
            'timelineItems': {'nodes': [{'label': {'name': x}, 'actor': by.get(x, HUMAN)} for x in labels]}}


def pr(n=495, labels=(), milestone=None, fixes=()):
    return {'number': n, 'state': 'OPEN', 'labels': {'nodes': [{'name': x} for x in labels]},
            'milestone': milestone, 'closingIssuesReferences': {'nodes': list(fixes)}}


M1, M2 = {'number': 1, 'title': 'M1'}, {'number': 2, 'title': 'M2'}


def test_495_inherits_fast_track_from_494():
    add, milestone, notes = L.plan(pr(fixes=[issue(494, ['fast-track'], milestone=M1)]))
    assert add == ['fast-track'] and milestone == M1 and notes == []


def test_only_human_labels_are_copied_and_retired_labels_never():
    i = issue(494, ['fast-track', 'swarm', 'MinBot-passed', 'auto-tag', 'priority:high'],
              by={'auto-tag': MINBOT, 'priority:high': {'__typename': 'Bot', 'login': 'renovate[bot]'}})
    assert L.human_labels(i) == ['fast-track']
    i['timelineItems']['nodes'] = []  # no LabeledEvent: who put it there is unknown, so no copy
    assert L.human_labels(i) == []
    i = issue(1, ['x'], by={'x': {'__typename': 'User', 'login': 'some-app[bot]'}})
    assert L.human_labels(i) == []


def test_sync_only_adds_and_never_replaces_a_milestone():
    p = pr(labels=['fast-track', 'mine'], milestone=M2, fixes=[issue(494, ['fast-track', 'ui'], milestone=M1)])
    add, milestone, notes = L.plan(p)
    assert add == ['ui'] and milestone is None
    assert notes == ['keeps its milestone M2 (never replaced)']
    add, milestone, notes = L.plan(pr(fixes=[issue(1, milestone=M1), issue(2, milestone=M2)]))
    assert milestone == M1 and 'using M1' in notes[0]


def test_sync_writes_through_minbot_and_reports_the_milestone():
    calls, out = [], []
    s = L.Sync(apply=True, out=out.append, gql=lambda q: None, run=lambda *a: calls.append(a))
    assert s.sync(pr(fixes=[issue(494, ['fast-track'], milestone=M1)]))
    assert calls == [('label', 'add', 495, 'fast-track', '--repo', L.REPO)]
    assert out == ['#495: added fast-track',
                   '#495: milestone: needs M1 (minbot has no milestone command; not written)']
    calls.clear()
    assert not s.sync(pr(labels=['fast-track'], milestone=M1, fixes=[issue(494, ['fast-track'], milestone=M1)]))
    assert calls == []


def test_sweep_pages_through_every_open_pr():
    pages = iter([{'repository': {'pullRequests': {'pageInfo': {'hasNextPage': True, 'endCursor': 'c1'},
                                                   'nodes': [pr(1)]}}},
                  {'repository': {'pullRequests': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                                   'nodes': [pr(2, fixes=[issue(3, ['fast-track'])])]}}}])
    seen, out = [], []
    s = L.Sync(out=out.append, gql=lambda q: seen.append(q) or next(pages), run=lambda *a: None)
    assert s.main(sweep=True) == 0
    assert 'after:"c1"' in seen[1]
    assert out == ['#2: would add fast-track', 'label-sync: 2 PR(s) read, 1 need a change (dry run)']


def test_event_syncs_only_when_the_closing_links_may_change():
    assert L.event_pr('pull_request', {'action': 'opened', 'pull_request': {'number': 7}}) == 7
    assert L.event_pr('pull_request', {'action': 'edited', 'changes': {'body': {}}, 'pull_request': {'number': 7}}) == 7
    assert L.event_pr('pull_request', {'action': 'edited', 'changes': {'title': {}}, 'pull_request': {'number': 7}}) is None
    assert L.event_pr('pull_request', {'action': 'synchronize', 'pull_request': {'number': 7}}) is None
    assert L.event_pr('issues', {'action': 'opened'}) is None


def test_event_cli_skips_other_events(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'action': 'closed'})))
    assert L.main(['event', 'pull_request']) == 0
    assert capsys.readouterr().out.startswith('skip:')


def card(col, project=4):
    return {'nodes': [{'project': {'number': project},
                       'fieldValueByName': {'name': 'renamed', 'optionId': board.OPTION[col]}}]}


@pytest.mark.parametrize('col', [board.ACTIVE, board.IN_PROGRESS, board.AUTO_REVIEW, board.NEEDS_INPUT,
                                 board.READY, board.STAGING, board.HUMAN_REVIEW])
def test_assigned_means_a_card_in_active_or_later(col):
    ok, why = L.assigned_by_card(card(col))
    assert ok and why == f"Project 4 card in {col}"   # by option id: the shown name may change


@pytest.mark.parametrize('col', [board.INBOX, board.TRIAGE, board.BACKLOG, board.DONE, board.PARKED])
def test_not_assigned_before_active_or_after_the_flow(col):
    assert not L.assigned_by_card(card(col))[0]
    assert L.assigned_by_card(card(board.ACTIVE, project=9)) == (False, 'no Project 4 card')
