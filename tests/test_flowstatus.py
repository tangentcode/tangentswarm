"""flow-status: one board query, one ls-remote, and a kept report when nothing changed."""
import subprocess
from datetime import datetime, timezone

import pytest

from tangentswarm import flowstatus as F


def pr(n, branch, head, draft=False, body=''):
    return dict(__typename='PullRequest', number=n, state='OPEN', title=f'PR {n}', isDraft=draft,
                headRefName=branch, headRefOid=head, body=body, repository={'nameWithOwner': F.REPO},
                milestone=None, labels={'nodes': []}, closingIssuesReferences={'nodes': []},
                timelineItems={'nodes': []})


def node(status, content):
    from tangentswarm import board
    key = {'Staging': board.STAGING}.get(status, status)
    return {'id': f"I{content.get('number')}", 'content': content,
            'fieldValueByName': {'name': key, 'optionId': board.OPTION[key]}}


def pages(*page_nodes):
    """One batched snapshot answer with every node (boardcache reads open items only)."""
    import boardfake
    calls = []
    return boardfake.gql([n for nodes in page_nodes for n in nodes], calls), calls


def test_read_board_keeps_open_platform_prs_in_report_columns():
    other = dict(pr(9, 'x', 'h9'), repository={'nameWithOwner': 'someone/else'})
    gql, calls = pages(
        [node('Human Review', pr(1, 'a', 'h1')), node('Staging', pr(2, 'b', 'h2')), node('Ready', pr(3, 'c', 'h3'))],
        [node('In Progress', dict(pr(4, 'd', 'h4'), state='CLOSED')), node('Human Review', other),
         node('Inbox', {'__typename': 'Issue', 'number': 5, 'state': 'OPEN',
                       'repository': {'nameWithOwner': F.REPO}})])
    items, prs = F.read_board(gql)
    assert len(calls) == 1 and 'is:pr is:open' in calls[0] and 'is:issue is:open' in calls[0]  # one request
    assert sorted(prs) == [1, 2]                      # Ready is not a report column; 4 is closed
    assert 'repository' not in prs[1]
    assert {(i['number'], i['status']) for i in items} == {(1, 'Human Review'), (2, 'Pending'), (3, 'Ready'),
                                                           (5, 'Inbox')}  # open items only


def test_ls_heads_parses_one_call_and_raises_on_failure():
    out = 'aaa\trefs/heads/main\nbbb\trefs/heads/feat-v1\nccc\trefs/tags/v1\n'
    ok = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, '')
    assert F.ls_heads('url', run=ok) == {'main': 'aaa', 'feat-v1': 'bbb'}
    bad = lambda *a, **k: subprocess.CompletedProcess(a, 128, '', 'fatal: no')
    with pytest.raises(RuntimeError, match='fatal: no'):
        F.ls_heads('url', run=bad)


def test_relevant_heads_are_main_and_each_pr_branch_with_its_vn_tips():
    heads = {'main': 'm', 'feat': 'f', 'feat-v2': 'f2', 'feat-old': 'x', 'other': 'o'}
    assert F.relevant_heads(heads, {1: pr(1, 'feat', 'f')}) == {'feat': 'f', 'feat-v2': 'f2', 'main': 'm'}


def test_fingerprint_moves_only_with_what_the_report_reads():
    items = [{'status': 'Human Review', 'number': 1, 'state': 'OPEN', 'typename': 'PullRequest'}]
    prs = {1: pr(1, 'feat', 'f')}
    heads = {'main': 'm', 'feat': 'f', 'unrelated': 'u'}
    base = F.fingerprint(items, prs, heads, 'q', None)
    assert F.fingerprint(items, prs, dict(heads, unrelated='u2'), 'q', None) == base
    assert F.fingerprint(items, prs, dict(heads, main='m2'), 'q', None) != base
    assert F.fingerprint(items, prs, dict(heads, **{'feat-v1': 'v'}), 'q', None) != base
    assert F.fingerprint(items, {1: pr(1, 'feat', 'f', draft=True)}, heads, 'q', None) != base
    assert F.fingerprint(items, prs, heads, 'q2', None) != base
    assert F.fingerprint(items, prs, heads, 'q', 488) != base


class FakeGate(dict):
    """The hr-gate globals report() and run() use, with a ScratchRepo that counts runs."""
    def __init__(self, board):
        super().__init__()
        self.runs = 0
        gate = self

        class Scratch:
            def __enter__(self):
                gate.runs += 1
                return self

            def __exit__(self, *a):
                return False

        def list_column_prs(*cols, refresh=False):
            return [dict(number=n, head=p['headRefOid'], branch=p['headRefName'], pr=p, entered=None)
                    for it in self['list_board_items']() if it['status'] in cols
                    for n, p in [(it['number'], self['_pr_cache'][it['number']])]]

        self.update(
            ET=timezone.utc, QUEUE_PATH='/nonexistent/queue.tsv', PENDING_TARGET=3, IN_PROGRESS_MAX=3,
            HR_TARGET=2, ScratchRepo=Scratch, _pr_cache={}, _hr_cache=None,
            gql=board, origin_url=lambda: 'url',
            fetch=lambda n, refresh=False: self['_pr_cache'][n],
            list_column_prs=list_column_prs,
            list_hr_prs=lambda refresh=False: list_column_prs('Human Review'),
            hr_fit=lambda p, hr, s, in_hr=False: (True, 'independent', 'independent', None),
            queue_from_board_or_file=lambda: [],
            conflict_sorted_pending=lambda s, q, hr: q,
            ready_next_candidate=lambda s, hr, q: None,
            invariant_status=lambda hr, q, ip, ar, ready: ('OK', 'invariant holds'))


def test_run_keeps_the_report_until_something_changes(tmp_path):
    gql, _ = pages([node('Human Review', pr(1, 'feat', 'f1'))])
    g = FakeGate(gql)
    heads = {'main': 'm1', 'feat': 'f1'}
    cache = tmp_path / 'fs.json'
    when = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)

    first = F.run(g, cache_path=cache, heads_of=lambda url: heads, now=when)
    assert g.runs == 1 and g['_pr_cache'][1]['headRefOid'] == 'f1'
    assert first[0] == 'flow-status 6:00 AM ET'
    assert first[1:3] == ['Human Review: 1/2', '  #1 feat f1  PR 1  [OK independent]']

    again = F.run(g, cache_path=cache, heads_of=lambda url: heads, now=when)
    assert g.runs == 1                                   # no git work, no PR reads
    assert again[0] == 'flow-status 6:00 AM ET  (no change since 6:00 AM)'
    assert again[1:] == first[1:]

    F.run(g, cache_path=cache, heads_of=lambda url: dict(heads, main='m2'), now=when)
    assert g.runs == 2                                   # main moved: recompute
    F.run(g, fresh=True, cache_path=cache, heads_of=lambda url: dict(heads, main='m2'), now=when)
    assert g.runs == 3                                   # --fresh ignores the kept report


def test_install_points_hr_gate_readers_at_the_one_query(tmp_path):
    g = FakeGate(None)
    g['_hr_cache'] = ['stale']
    items = [{'status': 'Pending', 'number': 2, 'state': 'OPEN', 'typename': 'PullRequest'}]
    F.install(g, items, {2: pr(2, 'b', 'h2')}, {'main': 'm', 'b-v1': 'v'})
    assert g['list_board_items'](refresh=True) == items
    assert g['remote_sha']('b-v1') == 'v' and g['remote_sha']('gone') is None
    assert g['_remote_heads_cache'] == ['b-v1', 'main']
    assert g['_hr_cache'] is None and g['fetch'](2)['headRefOid'] == 'h2'
