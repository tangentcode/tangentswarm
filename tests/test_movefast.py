"""tangentswarm #116: move cards within seconds of a merge.

1. The board cache follows the webhook events; a mover decides from it with no board read.
2. The sync reads the board every SWARM_BOARD_SYNC seconds and logs what the events missed.
3. No re-checks: a Staging -> Human Review move only confirms the gated head; GitHub is
   asked after the move (confirm).
4. Each PR has one priority staging site; only that site holds a card.
And the merge clock logs the time from the merge event to the move.
"""
import boardfake
import pytest

from tangentswarm import advance as A, board, boardcache as B, gatechecks as GC, moves, promoted as P

H5, H6, H8 = '5' * 40, '6' * 40, '8' * 40
HR, ST, RD = board.HUMAN_REVIEW, board.STAGING, board.READY


def node(n, col, head=None, kind='PullRequest', milestone=None):
    c = {'__typename': kind, 'number': n, 'state': 'OPEN', 'headRefOid': head or f'{n:040d}',
         'repository': {'nameWithOwner': B.REPO}, 'labels': {'nodes': []}, 'isDraft': False}
    if milestone:
        c['milestone'] = {'title': milestone}
    return {'id': f'I{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]}, 'content': c}


def counting(nodes):
    calls = []
    return boardfake.gql(nodes, calls), calls


def pr_event(action, n, **pr):
    return {'action': action, 'repository': {'full_name': B.REPO}, 'pull_request': dict(number=n, **pr)}


def item_event(item, col, project=None):
    return {'action': 'edited', 'projects_v2_item': {'node_id': item, 'project_node_id': project or board.DEFAULT.id},
            'changes': {'field_value': {'field_node_id': board.DEFAULT.status_field,
                                        'to': {'id': board.OPTION[col], 'name': col}}}}


def cols_of(snap):
    return {c['number']: B.col(it) for it, c in B.open_prs(snap)}


# -- 1. the cache follows the events ------------------------------------------------------------

def test_events_keep_the_cache_current_with_no_board_read():
    gql, calls = counting([node(1, HR, H5), node(5, ST, H5), node(6, RD, H6)])
    B.snapshot(gql)
    assert len(calls) == 1
    B.apply_event('pull_request', pr_event('closed', 1, merged=True))          # the merge
    B.apply_event('projects_v2_item', item_event('I6', ST))                    # a Status move
    B.apply_event('pull_request', pr_event('synchronize', 5, head={'sha': H8}, updated_at='t2'))
    B.apply_event('pull_request', pr_event('labeled', 6, labels=[{'name': 'fast-track'}]))
    B.apply_event('pull_request', pr_event('converted_to_draft', 6))
    snap = B.snapshot(gql)
    assert len(calls) == 1  # no board read: the events patched it
    assert cols_of(snap) == {5: ST, 6: ST}
    c5 = next(c for _, c in B.open_prs(snap) if c['number'] == 5)
    c6 = next(c for _, c in B.open_prs(snap) if c['number'] == 6)
    assert c5['headRefOid'] == H8 and c5['updatedAt'] == 't2'
    assert B._names(c6) == ['fast-track'] and c6['isDraft'] is True


def test_events_of_another_repo_or_project_or_field_change_nothing():
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    other = pr_event('closed', 5, merged=True)
    other['repository']['full_name'] = 'someone/else'
    assert B.apply_event('pull_request', other) == []
    assert B.apply_event('projects_v2_item', item_event('I5', HR, project='PVT_other')) == []
    e = item_event('I5', HR)
    e['changes']['field_value']['field_node_id'] = 'another-field'
    assert B.apply_event('projects_v2_item', e) == []
    assert cols_of(B.snapshot(gql)) == {5: ST} and len(calls) == 1


@pytest.mark.parametrize('event,payload', [
    ('pull_request', pr_event('opened', 9)),
    ('pull_request', pr_event('milestoned', 5)),
    ('projects_v2_item', {'action': 'created', 'projects_v2_item': {'node_id': 'I9', 'project_node_id': board.DEFAULT.id}}),
    ('projects_v2_item', item_event('I9', RD)),  # a move of a card the cache does not hold
    ('issues', {'action': 'labeled', 'repository': {'full_name': B.REPO}, 'issue': {'number': 3}}),
])
def test_an_event_the_cache_cannot_follow_makes_the_next_read_take_the_board(event, payload):
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    B.apply_event(event, payload)
    B.snapshot(gql)
    assert len(calls) == 2


def test_a_swarm_move_patches_the_cache_and_hr_gates_invalidate_is_held():
    """board.move_card through hr-gate's move(): hr-gate calls invalidate(); under hold() it
    does nothing, and moved() puts the card in its new column. No board read follows."""
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    seen = []
    g = {'move': lambda i, c: seen.append((i, c)) or B.invalidate(), 'OPT': {HR: 'x'}}
    board.move_card(g, 'I5', HR, n=5)
    assert seen == [('I5', HR)]
    assert cols_of(B.snapshot(gql)) == {5: HR} and len(calls) == 1
    B.invalidate()  # outside a move: the next read takes the board
    B.snapshot(gql)
    assert len(calls) == 2


def test_a_move_of_a_card_that_just_merged_does_not_dirty_the_cache():
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    B.apply_event('pull_request', pr_event('closed', 5, merged=True))
    assert B.moved('I5', HR) is True
    B.snapshot(gql)
    assert len(calls) == 1


def test_an_event_during_a_board_read_is_kept_on_top_of_the_read():
    now = [1000.0]
    old = [node(5, ST, H5), node(1, HR)]

    def gql(q, **v):  # the merge event lands while the read runs; the read still shows #1
        B.apply_event('pull_request', pr_event('closed', 1, merged=True), now=lambda: now[0])
        return boardfake.answer(old)
    B._write(B.PATH, {'prs': [], 'issues': [], 'at': 0.0})
    snap = B.snapshot(gql, max_age=0, now=lambda: now[0])
    assert cols_of(snap) == {5: ST}


# -- 2. the sync -------------------------------------------------------------------------------

def test_the_sync_reads_the_board_after_the_period_and_logs_what_was_missed():
    now = [1000.0]
    nodes = [node(5, ST, H5), node(6, RD, H6)]
    gql, calls = counting(nodes)
    B.snapshot(gql, now=lambda: now[0])
    now[0] += B.SYNC_EVERY - 1
    assert not B.sync_due(now=lambda: now[0])
    B.snapshot(gql, now=lambda: now[0])
    assert len(calls) == 1  # inside the period: the cache
    # events lost: #5 moved to Human Review, #6 got a new head, #7 is new on the board
    nodes[0]['fieldValueByName'] = {'name': HR, 'optionId': board.OPTION[HR]}
    nodes[1]['content']['headRefOid'] = H8
    nodes.append(node(7, RD))
    now[0] += 2
    assert B.sync_due(now=lambda: now[0])
    snap = B.snapshot(gql, now=lambda: now[0])
    assert len(calls) == 2 and cols_of(snap) == {5: HR, 6: RD, 7: RD}
    log = B.SYNC_LOG.read_text().splitlines()
    assert [line.split('\t', 1)[1] for line in log] == [
        f'sync\t#5 column {ST} -> {HR}', 'sync\t#6 head 66666666 -> 88888888', f'sync\t#7 is on the board in {RD}, not in the cache']


def test_a_sync_that_finds_nothing_missed_logs_nothing():
    now = [0.0]
    gql, _ = counting([node(5, ST, H5)])
    B.snapshot(gql, now=lambda: now[0])
    now[0] += B.SYNC_EVERY + 1
    B.snapshot(gql, now=lambda: now[0])
    assert not B.SYNC_LOG.exists()


# -- 3. no re-checks: only the gated head -----------------------------------------------------------

LOG = ("2026-11-13T19:00:00-05:00\tapply\t#5\t55555555\tPASS\tfail=-\tcaller=x\n"
       "2026-11-13T19:00:00-05:00\tapply\t#8\t88888888\tPASS\tfail=-\tcaller=x\n")


class G(dict):
    """hr-gate's namespace: every GraphQL call and move goes to one ordered list."""
    def __init__(self, tmp_path, after=None):
        super().__init__()
        (tmp_path / 'hr.log').write_text(LOG)
        self.order = []
        after = after or {}

        def gql(q, **v):
            self.order.append('gql')
            return {'repository': {f'p{n}': dict(number=n, state='OPEN', isDraft=False, headRefOid=h)
                                   for n, h in after.items()}}
        self.update(LOG=str(tmp_path / 'hr.log'), read_queue_file=lambda: [], gql=gql,
                    move=lambda i, c: self.order.append(('move', i, c)), log=lambda *a: None)

    @property
    def moves(self):
        return [x[1:] for x in self.order if x != 'gql']


def staging(*prs):
    return {HR: [dict(number=1, head='a', item='I1')], ST: [dict(number=n, head=h, item=f'I{n}') for n, h in prs]}


def test_a_move_reads_nothing_from_github_before_it_and_confirms_after(tmp_path):
    g = G(tmp_path, after={5: H5})
    cols = staging((5, H5))
    assert P.Promoter(g=g).fast_path(cols, reread=lambda: cols) == [5]
    assert g.order == [('move', 'I5', HR), 'gql']  # the move first; the one confirm read after it


def test_the_move_only_confirms_the_gated_head(tmp_path):
    """No draft, stacked, comment, served or merge check: those ran before Staging. A head
    with no gate pass stays."""
    g = G(tmp_path, after={8: H8})
    cols = staging((5, '9' * 40), (8, H8))  # #5 has a new, ungated head
    p = P.Promoter(g=g)
    assert p.fast_path(cols, reread=lambda: cols) == [8]
    assert p.left == [(5, '9' * 40, 'head 99999999 has no gate pass')]
    assert not hasattr(p, 'cheap_check') and not hasattr(P, 'merge_problem')


def test_confirm_sends_back_a_pr_whose_head_moved_or_that_became_a_draft(tmp_path):
    g = G(tmp_path, after={5: '9' * 40})
    cols = staging((5, H5))
    assert P.Promoter(g=g).fast_path(cols, reread=lambda: cols) == [5]
    assert g.moves == [('I5', HR), ('I5', ST)]
    assert 'confirm: head is 99999999, not 55555555' in moves.LOG.read_text()
    g = G(tmp_path, after={5: H5})
    g['gql'] = lambda q, **v: {'repository': {'p5': dict(number=5, state='OPEN', isDraft=True, headRefOid=H5)}}
    P.Promoter(g=g).fast_path(cols, reread=lambda: cols)
    assert g.moves == [('I5', HR), ('I5', ST)]


def test_confirm_leaves_a_merged_pr_and_survives_a_failed_read(tmp_path):
    g = G(tmp_path)
    g['gql'] = lambda q, **v: {'repository': {'p5': dict(number=5, state='MERGED', headRefOid='x')}}
    cols = staging((5, H5))
    assert P.Promoter(g=g).fast_path(cols, reread=lambda: cols) == [5] and g.moves == [('I5', HR)]

    def boom(q, **v):
        raise RuntimeError('502')
    g = G(tmp_path)
    g['gql'] = boom
    assert P.Promoter(g=g).fast_path(cols, reread=lambda: cols) == [5] and g.moves == [('I5', HR)]


def test_a_merge_event_to_the_move_is_logged_end_to_end(tmp_path, monkeypatch):
    """The webhook patches the cache and starts the merge clock; the daemon's next check moves
    from the cache (one board read in all: the first one) and logs event -> move."""
    from tangentswarm import webhook as W
    gql_nodes = [node(1, HR, 'a' * 40), node(5, ST, H5)]
    reads = []
    g = G(tmp_path, after={5: H5})
    confirm = g['gql']
    g['gql'] = lambda q, **v: (reads.append(q) or boardfake.answer(gql_nodes)) if 'projectV2' in q else confirm(q, **v)
    B.snapshot(g['gql'])
    ex = W.Executor(g={'gql': lambda q, **v: {}}, dry=True)  # never the real hr-gate
    W.handle('pull_request', pr_event('closed', 1, merged=True, base={'ref': 'main'}, head={'sha': 'a' * 40},
                                      merged_at='2026-11-17T15:26:08Z'), 'd1', ex, received=100.0)
    assert moves._pending(moves.MERGES)['1']['received'] == 100.0
    p = P.Promoter(g=g, dry_run=False)
    monkeypatch.setattr(p, 'side_work', lambda cols: (None, False, []))
    monkeypatch.setattr(p, 'drain', lambda: [])
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: P._null())
    monkeypatch.setattr(P.time, 'time', lambda: 101.5)
    monkeypatch.setattr(moves.time, 'time', lambda: 101.5)
    assert p.once() == 'promoted'
    assert g.moves == [('I5', HR)] and len(reads) == 1
    line = moves.CLOCK_LOG.read_text().strip().split('\t')
    assert line[2:5] == ['merge #1', f'moved #5 Staging -> {HR}', 'event->move 1.5s']
    assert line[5].startswith('merge->move ')
    assert moves._pending(moves.MERGES) == {}


def test_a_merge_that_frees_no_move_still_gets_its_clock_line(tmp_path):
    moves.note_merge(1, received=10.0)
    p = P.Promoter(g=G(tmp_path))
    p.fill({HR: [], ST: []})
    assert '\tmerge #1\tno move\t' in moves.CLOCK_LOG.read_text()


def test_a_merge_after_the_pass_started_waits_for_the_next_pass():
    moves.note_merge(1, received=50.0)
    assert moves.merge_clock([], upto=40.0, now=60.0) == []
    assert moves.merge_clock([7], upto=60.0, now=61.0)[0].split('\t')[2:5] == [
        'merge #1', 'moved #7 Staging -> Human Review', 'event->move 11.0s']


# -- 4. one priority staging site ------------------------------------------------------------------

CFG = {'sites': {'ptw': 'ptw.example', 'work': 'work.example', 'minavo': 'minavo.example'}}


def test_the_milestone_picks_the_priority_site_and_the_default_is_ptw():
    assert GC.priority_site(['PTW'], CFG) == 'ptw'
    assert GC.priority_site(['ptw beta'], CFG) == 'ptw'
    assert GC.priority_site(['Optware'], CFG) == 'ptw'      # not a whole word: the default
    assert GC.priority_site([], CFG) == 'ptw'
    cfg = dict(CFG, priority={'PTW': 'ptw', 'Minavo': 'minavo'}, priority_default='work')
    assert GC.priority_site(['Minavo launch'], cfg) == 'minavo'
    assert GC.priority_site(['Q4'], cfg) == 'work'
    assert GC.priority_host(['Minavo launch'], cfg) == 'minavo.example'
    assert GC.priority_host([], {'sites': {'work': 'w'}}) is None  # no ptw site: every site counts


WEB = ['web/ptw/board.ts']
SITES = [('ptw.example', 'aaaaaaaa', None), ('work.example', 'bbbbbbbb', None), ('minavo.example', None, 'cannot read')]


def test_only_the_priority_site_must_serve_the_head():
    has = lambda s: s == 'aaaaaaaa'  # noqa: E731  ptw serves it; work lags; minavo unreadable
    ok, why = GC.check_served_sites(WEB, True, SITES, has, priority='ptw.example')
    assert ok and why == ('the priority site serves it: ptw.example: static .deploy-sha aaaaaaaa contains the head; '
                          'rolling out (not blocking): work.example, minavo.example')
    ok, why = GC.check_served_sites(WEB, True, SITES, has, priority='work.example')
    assert not ok and why.startswith('the priority site does not serve it: work.example: static .deploy-sha bbbbbbbb LACKS')
    ok, why = GC.check_served_sites(WEB, False, SITES, has, priority='work.example')
    assert ok and why.startswith('lenient re-gate: the priority site does not serve it')
    ok, why = GC.check_served_sites(WEB, True, SITES, has, priority=None)  # no priority site: all
    assert not ok and why.startswith('2 of 3 staging sites do not serve it')


def test_hr_gate_condition_6_uses_the_configured_default_priority_site(monkeypatch):
    from tangentswarm import config
    monkeypatch.setattr(config, 'get', lambda *k, default=None: CFG if k == ('staging',) else default)
    ok, why = GC.check_served_sites(WEB, True, SITES, lambda s: s == 'aaaaaaaa')
    assert ok and why.startswith('the priority site serves it: ptw.example')


def test_a_pull_waits_only_for_its_priority_site(monkeypatch):
    W_ = 'w' * 40
    g = {'served_all': lambda: [('ptw', 'ptw.example', 'o' * 40, None), ('work', 'work.example', W_, None)],
         'contains': lambda a, d: False}
    d = dict(pr=9, head='9' * 40, work=W_, started=0)
    assert A.lagging(g, d, now=lambda: 0, priority='work.example') == []
    assert [x[1] for x in A.lagging(g, d, now=lambda: 0, priority='ptw.example')] == ['ptw.example']
    assert [x[1] for x in A.lagging(g, d, now=lambda: 0, priority=None)] == ['ptw.example']


def test_the_priority_site_of_a_pull_comes_from_its_milestone_in_the_cache(monkeypatch):
    from tangentswarm import config
    cfg = dict(CFG, priority={'PTW': 'ptw', 'Minavo': 'minavo'}, priority_default='work')
    monkeypatch.setattr(config, 'get', lambda *k, default=None: cfg if k == ('staging',) else default)
    gql, _ = counting([node(9, RD, milestone='Minavo beta'), node(10, RD)])
    assert A.priority_host(9, gql) == 'minavo.example'
    assert A.priority_host(10, gql) == 'work.example'
    assert A.priority_host(11, None) == 'work.example'


# -- Codex on PR #117 ------------------------------------------------------------------------------

@pytest.mark.parametrize('found', [dict(headRefOid='9' * 40, isDraft=False), dict(headRefOid=H5, isDraft=True)])
def test_a_pr_that_confirm_sends_back_is_not_picked_again_and_the_next_one_moves(tmp_path, monkeypatch, found):
    """Codex on PR #117 (P2): confirm() writes what GitHub said into the cache. The next pass
    refuses #5 (a new head has no gate pass; a draft is refused) and moves #8 instead. Covers
    a moved head and a same-head draft."""
    monkeypatch.setattr(P, 'HR_TARGET', 2)
    nodes = [node(1, HR, 'a' * 40), node(5, ST, H5), node(8, ST, H8)]
    g = G(tmp_path)
    reads = []

    def gql(q, **v):
        if 'projectV2' in q:
            reads.append(q)
            return boardfake.answer(nodes)
        g.order.append('gql')
        return {'repository': {'p5': dict(number=5, state='OPEN', **found),
                               'p8': dict(number=8, state='OPEN', isDraft=False, headRefOid=H8)}}
    g['gql'] = gql
    g['OPT'] = {HR: 'x', ST: 'y'}
    p = P.Promoter(g=g)
    one = lambda: {k: v[:1] if k == ST else v for k, v in P.read_columns(gql).items()}  # noqa: E731
    assert p.fast_path(one(), reread=one) == [5]
    assert g.moves == [('I5', HR), ('I5', ST)]
    assert p.fast_path(P.read_columns(gql), reread=lambda: P.read_columns(gql)) == [8]
    assert g.moves[2:] == [('I8', HR)]
    assert p.left and p.left[0][0] == 5 and len(reads) == 1  # no board read: the cache
    if found['isDraft']:  # ready again: the event clears it and #5 may move
        B.apply_event('pull_request', pr_event('ready_for_review', 5))
        assert [q['number'] for q in P.read_columns(gql)[ST] if not q['draft']] == [5]


def test_a_draft_in_the_cache_is_never_moved(tmp_path):
    g = G(tmp_path)
    cols = {HR: [], ST: [dict(number=5, head=H5, item='I5', draft=True)]}
    p = P.Promoter(g=g)
    assert p.fast_path(cols, reread=lambda: cols) == [] and p.left == [(5, H5, 'is a draft')]


@pytest.mark.parametrize('changes,reads', [
    ({'body': {'from': 'old'}}, 2),           # "Fixes #N" may have changed: read the board
    ({'base': {'ref': {'from': 'x'}}}, 2),    # the base is not in the cache either
    (None, 2),                                # no changes field: be safe
    ({'title': {'from': 'old'}}, 1),          # a title-only edit: the cache follows it
])
def test_a_body_edit_makes_the_next_read_take_the_board(changes, reads):
    """Codex on PR #117 (P2): closingIssuesReferences (and the labels and milestones it
    brings) come from the body; the cache cannot derive them."""
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    e = pr_event('edited', 5, body='Fixes #9', title='t2')
    if changes is not None:
        e['changes'] = changes
    B.apply_event('pull_request', e)
    snap = B.snapshot(gql)
    assert len(calls) == reads
    if reads == 1:
        assert next(c for _, c in B.open_prs(snap))['title'] == 't2'


def moved_event(item, frm, to):
    e = item_event(item, to)
    e['changes']['field_value']['from'] = {'id': board.OPTION[frm], 'name': frm} if frm else None
    return e


def test_a_status_event_older_than_a_local_move_never_overwrites_it():
    """Codex on PR #117 (P2): a queued Ready -> Staging event handled after the swarm moved
    #5 Staging -> Human Review must not put #5 back in Staging. Its "from" is not the cached
    column, so the cache is marked dirty and the next read takes the board."""
    board_nodes = [node(5, ST, H5)]
    gql, calls = counting(board_nodes)
    B.snapshot(gql)
    B.moved('I5', HR)                                         # the swarm's own move
    B.apply_event('projects_v2_item', moved_event('I5', ST, HR))  # its echo: no change, no read
    assert cols_of(B.snapshot(gql)) == {5: HR} and len(calls) == 1
    B.apply_event('projects_v2_item', moved_event('I5', RD, ST))  # a stale, delayed delivery
    board_nodes[0]['fieldValueByName'] = {'name': HR, 'optionId': board.OPTION[HR]}
    assert cols_of(B.snapshot(gql)) == {5: HR} and len(calls) == 2  # read again, never Staging


def test_an_in_order_status_event_and_one_with_no_status_before_apply():
    gql, calls = counting([node(5, RD, H5), node(6, RD, H6)])
    B.snapshot(gql)
    B.apply_event('projects_v2_item', moved_event('I5', RD, ST))
    snap = B.snapshot(gql)
    assert cols_of(snap)[5] == ST and len(calls) == 1
    B._write(B.PATH, dict(snap, prs=[dict(it, fieldValueByName=None) if it['id'] == 'I6' else it for it in snap['prs']]))
    B.apply_event('projects_v2_item', moved_event('I6', None, ST))  # a card's first status
    assert cols_of(B.snapshot(gql))[6] == ST and len(calls) == 1


def test_an_out_of_order_push_or_older_pr_event_never_rolls_the_cache_back():
    """Neighbors of the stale status event: synchronize carries "before" (applied only onto
    that head; else the board is read), and every PR event carries updated_at (an event older
    than the cached value of the SAME field is skipped: the cache knows that state or a later one)."""
    gql, calls = counting([node(5, ST, H5)])
    B.snapshot(gql)
    B.apply_event('pull_request', pr_event('synchronize', 5, head={'sha': H8}, updated_at='2026-11-17T16:00:02Z')
                  | {'before': H5})
    c = next(c for _, c in B.open_prs(B.snapshot(gql)))
    assert c['headRefOid'] == H8 and len(calls) == 1
    # the older push (H6 -> H5... delivered late): its updated_at is older: skipped, no read
    B.apply_event('pull_request', pr_event('synchronize', 5, head={'sha': H5}, updated_at='2026-11-17T16:00:01Z')
                  | {'before': H6})
    c = next(c for _, c in B.open_prs(B.snapshot(gql)))
    assert c['headRefOid'] == H8 and len(calls) == 1
    # a push whose "before" the cache never saw (a lost event): read the board
    B.apply_event('pull_request', pr_event('synchronize', 5, head={'sha': '7' * 40}, updated_at='2026-11-17T16:00:09Z')
                  | {'before': '9' * 40})
    B.snapshot(gql)
    assert len(calls) == 2


@pytest.mark.parametrize('late', [
    ('labeled', {'labels': [{'name': 'fast-track'}]}, lambda c: B._names(c) == ['fast-track']),
    ('converted_to_draft', {}, lambda c: c['isDraft'] is True),
    ('synchronize', {'head': {'sha': H8}}, lambda c: c['headRefOid'] == H8),
    ('edited', {'title': 't1'}, lambda c: c['title'] == 't1'),
])
def test_a_newer_patch_of_another_field_never_hides_an_older_event(late):
    """Codex on PR #117 (P2): a comment at T2 moves only updatedAt; a delayed labeled (or
    draft, push, title) event at T1 must still apply: freshness is per field."""
    nd = node(5, ST, H5)
    nd['content']['updatedAt'] = '2026-11-17T16:00:00Z'  # the board read
    gql, calls = counting([nd])
    B.snapshot(gql)
    B.apply_event('issue_comment', {'action': 'created', 'repository': {'full_name': B.REPO},
                                    'issue': {'number': 5, 'state': 'open', 'pull_request': {},
                                              'updated_at': '2026-11-17T16:00:02Z'}})
    action, extra, check = late
    e = pr_event(action, 5, updated_at='2026-11-17T16:00:01Z', **extra)
    if action == 'synchronize':
        e['before'] = H5
    if action == 'edited':
        e['changes'] = {'title': {'from': 't0'}}
    B.apply_event('pull_request', e)
    c = next(c for _, c in B.open_prs(B.snapshot(gql)))
    assert check(c) and c['updatedAt'] == '2026-11-17T16:00:02Z' and len(calls) == 1


def test_an_event_older_than_the_board_read_or_than_its_own_field_is_skipped():
    nd = node(5, ST, H5)
    nd['content']['updatedAt'] = '2026-11-17T16:00:05Z'
    gql, calls = counting([nd])
    B.snapshot(gql)
    B.apply_event('pull_request', pr_event('labeled', 5, updated_at='2026-11-17T16:00:01Z',
                                           labels=[{'name': 'old'}]))           # older than the read
    B.apply_event('pull_request', pr_event('labeled', 5, updated_at='2026-11-17T16:00:09Z',
                                           labels=[{'name': 'new'}]))
    B.apply_event('pull_request', pr_event('unlabeled', 5, updated_at='2026-11-17T16:00:07Z',
                                           labels=[]))                          # older than "new"
    c = next(c for _, c in B.open_prs(B.snapshot(gql)))
    assert B._names(c) == ['new'] and len(calls) == 1
