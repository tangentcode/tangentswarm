"""Board columns are matched by option id, so a renamed column keeps working."""
from tangentswarm import board, tick


def test_renamed_staging_column_maps_to_the_internal_key():
    assert board.column({'name': 'Staging', 'optionId': board.OPTION[board.STAGING]}) == board.STAGING == 'Pending'
    assert board.column({'name': 'Anything new', 'optionId': board.OPTION[board.HUMAN_REVIEW]}) == board.HUMAN_REVIEW


def test_unknown_id_falls_back_to_the_name_and_missing_value_is_none():
    assert board.column({'name': 'Brand new', 'optionId': 'ffffffff'}) == 'Brand new'
    assert board.column(None) is None


def test_event_names_include_old_and_new_names():
    assert board.event_names(board.STAGING) == {'Pending', 'Approved', 'Staging'}


def test_board_prs_uses_option_ids():
    item = {'id': 'I', 'fieldValueByName': {'name': 'Staging', 'optionId': board.OPTION[board.STAGING]},
            'content': {'__typename': 'PullRequest', 'number': 5, 'state': 'OPEN', 'isDraft': False,
                        'headRefName': 'b', 'headRefOid': 'h', 'title': 't', 'body': '',
                        'repository': {'nameWithOwner': tick.REPO}, 'milestone': None,
                        'labels': {'nodes': []}, 'closingIssuesReferences': {'nodes': []}}}
    page = {'organization': {'projectV2': {'items': {'nodes': [item],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
    prs = tick.board_prs({'gql': lambda q, c=None: page, 'PARKED': set()})
    assert prs[5]['col'] == 'Pending'


def test_fast_track_labels():
    for l in ('fast-track', 'Fast Track', 'fasttrack', 'high priority', 'High-Priority', 'priority: high'):
        assert board.is_fast_track([l]), l
    assert not board.is_fast_track(['swarm', 'priority-low', 'fast'])


def test_priority_rank_order():
    assert board.priority_rank(True, False) < board.priority_rank(False, True) < board.priority_rank(False, False)


def test_board_prs_fast_track_only_for_the_active_ticket(tmp_path):
    item = {'id': 'I', 'fieldValueByName': {'name': 'Ready', 'optionId': board.OPTION[board.READY]},
            'content': {'__typename': 'PullRequest', 'number': 9, 'state': 'OPEN', 'isDraft': False,
                        'headRefName': 'b', 'headRefOid': 'h', 'title': 't', 'body': '',
                        'repository': {'nameWithOwner': tick.REPO}, 'milestone': None,
                        'labels': {'nodes': [{'name': 'swarm'}]},
                        'closingIssuesReferences': {'nodes': [{'number': 488, 'milestone': None,
                                                               'labels': {'nodes': [{'name': 'fast-track'}]}}]}}}
    page = {'organization': {'projectV2': {'items': {'nodes': [item],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
    g = {'gql': lambda q, c=None: page, 'PARKED': set()}
    assert tick.board_prs(g)[9]['rank'] == board.NORMAL  # labeled, but not the active one
    board.FT_STATE.write_text('{"active": 488}')
    assert tick.board_prs(g)[9]['rank'] == board.FAST_TRACK


def ft_item(n, col, labels=(), closes=(), kind='PullRequest', state='OPEN'):
    return dict(number=n, kind=kind, state=state, col=col, labels=list(labels),
                closes=[dict(number=c, labels=['fast-track']) for c in closes])


def test_only_one_fast_track_is_active_and_it_stays_active(tmp_path):
    path = tmp_path / 'ft.json'
    items = [ft_item(488, board.ACTIVE, ['fast-track'], kind='Issue'),
             ft_item(500, board.READY, ['high priority'])]
    assert board.update_active(items, path) == (500, None)  # furthest along goes first
    items.append(ft_item(501, board.STAGING, ['fast-track']))
    assert board.update_active(items, path) == (500, 500)   # sticky: a later one does not take over
    assert board.active_fast_track(path) == 500


def test_next_fast_track_when_the_active_enters_human_review_or_loses_the_label(tmp_path):
    path = tmp_path / 'ft.json'
    a = ft_item(500, board.READY, ['fast-track'])
    b = ft_item(490, board.IN_PROGRESS, closes=[488])  # a PR closing the labeled issue #488
    issue = ft_item(488, board.IN_PROGRESS, ['fast-track'], kind='Issue')
    assert board.update_active([a, b, issue], path)[0] == 500
    a['col'] = board.HUMAN_REVIEW
    assert board.update_active([a, b, issue], path)[0] == 488
    assert board.is_active_fast_track(490, closes=[488], active=488)
    assert not board.is_active_fast_track(500, active=488)
    b['closes'], issue['labels'] = [], []
    assert board.update_active([a, b, issue], path)[0] is None


def test_closed_or_canceled_tickets_are_not_candidates():
    assert board.ft_candidates([ft_item(1, board.READY, ['fast-track'], state='CLOSED'),
                                ft_item(2, board.CANCELED, ['fast-track'])]) == {}


def test_a_fast_track_issue_is_released_when_its_pr_reaches_human_review(tmp_path):
    """Codex on PR #26: the issue card stays Active while its PR moves on, so the issue kept
    the fast-track after its PR entered Human Review."""
    p = tmp_path / 'ft.json'
    issue = ft_item(488, board.ACTIVE, ['fast-track'], kind='Issue')
    other = ft_item(490, board.BACKLOG, ['fast-track'], kind='Issue')
    pr_ready = ft_item(530, board.READY, closes=[488])
    assert board.update_active([issue, other, pr_ready], p)[0] == 488
    pr_hr = dict(pr_ready, col=board.HUMAN_REVIEW)
    assert board.update_active([issue, other, pr_hr], p)[0] == 490


def test_one_pick_key_for_every_picker():
    """tangentswarm #35 (Michal 2026-11-15 6:09 PM ET): fast-track, soonest milestone due date, number."""
    items = [dict(number=540, due='2026-12-01', milestone='MVP'), dict(number=543, due='2026-11-15', milestone='ptw 15'),
             dict(number=530, milestone='someday'), dict(number=520), dict(number=560, labels=['fast-track']),
             dict(number=550, closes=[488])]
    assert [i['number'] for i in sorted(items, key=lambda i: board.pick_key(i, active=488))] == [550, 560, 543, 540, 530, 520]
    from tangentswarm import advance, flow, promoted, slots
    assert slots.launch_key(items[0]) == board.pick_key(items[0])
    assert [p['number'] for p in advance.minbot_order(items, 488)] == [550, 560, 543, 540, 530, 520]
    assert [p['number'] for p in flow.ready_order(items, {}, 488)] == [550, 560, 543, 540, 530, 520]
    assert [p['number'] for p in promoted.staging_order(items, {}, [], 488)] == [550, 560, 543, 540, 530, 520]


def test_soonest_due_of_a_pr_and_its_issues():
    assert board.soonest_due([{'title': 'MVP', 'dueOn': '2026-12-01T00:00:00Z'},
                              {'title': 'ptw 15', 'dueOn': '2026-11-15T00:00:00Z'}]) == ('2026-11-15', 'ptw 15')
    assert board.soonest_due([{}, {'title': 'someday'}]) == ('', 'someday')
    assert board.soonest_due([{}]) == ('', '')
