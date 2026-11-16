import boardfake
from tangentswarm import board, boardcache as B


def node(n, kind, col, state='OPEN'):
    return {'id': f'I{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]},
            'content': {'__typename': kind, 'number': n, 'state': state, 'headRefOid': f'{n:040d}',
                        'repository': {'nameWithOwner': B.REPO}}}


def test_one_request_reads_open_prs_and_issues_and_every_reader_reuses_it():
    calls = []
    gql = boardfake.gql([node(1, 'PullRequest', board.READY), node(2, 'Issue', board.ACTIVE)], calls)
    now = [1000.0]
    s1 = B.snapshot(gql, now=lambda: now[0])
    now[0] += 30
    s2 = B.snapshot(gql, now=lambda: now[0])
    assert len(calls) == 1 and s1 == s2
    assert 'query:"is:pr is:open"' in calls[0] and 'query:"is:issue is:open"' in calls[0]
    assert B.item_rows(s2) == [{'status': board.READY, 'number': 1, 'state': 'OPEN', 'typename': 'PullRequest'},
                               {'status': board.ACTIVE, 'number': 2, 'state': 'OPEN', 'typename': 'Issue'}]
    now[0] += B.MAX_AGE
    B.snapshot(gql, now=lambda: now[0])
    B.snapshot(gql, max_age=0, now=lambda: now[0])       # read now
    B.invalidate()
    B.snapshot(gql, now=lambda: now[0])                  # a move dropped it
    assert len(calls) == 4


def test_a_second_page_reads_only_the_part_that_has_more():
    calls = []

    def gql(q, **v):
        calls.append((q.count('items('), v))
        if len(calls) == 1:
            ans = boardfake.answer([node(1, 'PullRequest', board.READY), node(2, 'Issue', board.ACTIVE)])
            ans['organization']['projectV2']['issues']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'X'}
            return ans
        return {'organization': {'projectV2': {'issues': {'nodes': [node(3, 'Issue', board.ACTIVE)],
                                                          'pageInfo': {'hasNextPage': False}}}}}
    snap = B.snapshot(gql)
    assert calls == [(2, {'pc': None, 'ic': None}), (1, {'ic': 'X'})]
    assert [c['number'] for _, c in B.open_issues(snap)] == [2, 3]


def test_detail_is_kept_per_head_sha_and_updated_time():
    reads = []
    get = lambda: reads.append(1) or {'x': len(reads)}  # noqa: E731
    assert B.detail(5, 'h1', 't1', get) == {'x': 1}
    assert B.detail(5, 'h1', 't1', get) == {'x': 1}
    assert B.detail(5, 'h2', 't1', get) == {'x': 2}   # a new push
    assert B.detail(5, 'h2', 't2', get) == {'x': 3}   # a new comment or review
    assert len(reads) == 3


def test_read_details_reads_an_unchanged_pr_once():
    from tangentswarm import promoted as P
    pr = node(7, 'PullRequest', board.STAGING)
    pr['content']['updatedAt'] = 't1'
    reads = []

    def gql(q, **v):
        if 'pullRequest(number:' in q:
            reads.append(q)
            return {'repository': {'p7': {'number': 7, 'headRefOid': f'{7:040d}', 'state': 'OPEN'}}}
        return boardfake.answer([pr])
    assert P.read_details(gql, [7])[7]['state'] == 'OPEN'
    assert P.read_details(gql, [7])[7]['state'] == 'OPEN'
    assert len(reads) == 1
    pr['content']['updatedAt'] = 't2'  # a new comment
    B.invalidate()
    P.read_details(gql, [7])
    assert len(reads) == 2
