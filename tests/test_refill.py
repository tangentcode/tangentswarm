"""tangentswarm #136: refill Human Review and Staging within seconds of ANY event that frees a
Human Review slot (a merge, a close, a reject, any other move out), with no board re-read and
no re-checks of a Ready PR.

1. The webhook wakes swarm-promoted first for every such event (not only a merge).
2. An event that touches only what the movers do not read makes the board cache stale, not
   dirty: the movers use it at once; the other readers read the board as before. Issue and PR
   labels and milestones are followed from the event.
3. Ready -> Staging of a pre-merged Ready PR is the card move only: no hr-gate run.
4. A push to work starts the ptw staging deploy at once; a deploy end queues the cascade at
   once and starts the deploy again when work moved past what it built.
"""
import subprocess

import boardfake
import pytest

from tangentswarm import board, boardcache as B, flow, promoted as P, stagingdeploy, webhook as W

HR, ST, RD, IP, BL = board.HUMAN_REVIEW, board.STAGING, board.READY, board.IN_PROGRESS, board.BACKLOG


def node(n, col, kind='PullRequest', closes=()):
    c = {'__typename': kind, 'number': n, 'state': 'OPEN', 'headRefOid': f'{n:040d}', 'isDraft': False,
         'repository': {'nameWithOwner': B.REPO}, 'labels': {'nodes': []}}
    if closes:
        c['closingIssuesReferences'] = {'nodes': [{'number': i, 'repository': {'nameWithOwner': B.REPO},
                                                   'labels': {'nodes': []}} for i in closes]}
    return {'id': f'I{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]}, 'content': c}


def counting(nodes):
    calls = []
    return boardfake.gql(nodes, calls), calls


def issue_event(action, n, **issue):
    return {'action': action, 'repository': {'full_name': B.REPO}, 'issue': dict(number=n, **issue)}


def item_event(item, col, frm=None, action='edited'):
    ch = {'field_node_id': board.DEFAULT.status_field, 'to': {'id': board.OPTION[col], 'name': col}}
    if frm:
        ch['from'] = {'id': board.OPTION[frm], 'name': frm}
    return {'action': action, 'projects_v2_item': {'node_id': item, 'project_node_id': board.DEFAULT.id},
            'changes': {'field_value': ch}}


# -- 2. the board cache: stale is not dirty -----------------------------------------------------

@pytest.mark.parametrize('event,payload', [
    ('issues', issue_event('opened', 30)),                       # a new issue (#569 on 2026-11-17)
    ('projects_v2_item', {'action': 'created',
                          'projects_v2_item': {'node_id': 'I30', 'project_node_id': board.DEFAULT.id}}),
    ('projects_v2_item', item_event('I30', BL)),                 # its first Status: Backlog
    ('projects_v2_item', item_event('I30', board.ACTIVE, frm=BL)),
    ('pull_request', {'action': 'opened', 'repository': {'full_name': B.REPO}, 'pull_request': {'number': 31}}),
    ('pull_request', {'action': 'edited', 'repository': {'full_name': B.REPO}, 'changes': {'body': {}},
                      'pull_request': {'number': 5, 'body': 'Fixes #40'}}),
    ('issues', issue_event('labeled', 30, labels=[{'name': 'fast-track'}])),  # an issue not in the cache
])
def test_an_event_only_other_readers_need_leaves_the_movers_on_the_cache(event, payload):
    gql, calls = counting([node(5, ST), node(6, HR)])
    B.snapshot(gql)
    B.apply_event(event, payload)
    assert len(calls) == 1
    P.read_columns(gql)                                    # a mover: the cache as is
    B.snapshot(gql, stale_ok=True)
    assert len(calls) == 1
    B.snapshot(gql)                                        # any other reader: the board again
    assert len(calls) == 2
    B.snapshot(gql)
    assert len(calls) == 2                                 # the read cleared the stale mark


@pytest.mark.parametrize('payload', [
    item_event('I30', ST),                                 # a card the cache does not hold, into Staging
    item_event('I30', IP, frm=RD),                         # out of Ready
    item_event('I5', HR, frm=RD),                          # a known card from the wrong column
    {'action': 'restored', 'projects_v2_item': {'node_id': 'I30', 'project_node_id': board.DEFAULT.id}},
])
def test_a_move_the_cache_cannot_follow_in_the_flight_columns_still_reads_the_board(payload):
    gql, calls = counting([node(5, ST), node(6, HR)])
    B.snapshot(gql)
    B.apply_event('projects_v2_item', payload)
    B.snapshot(gql, stale_ok=True)
    assert len(calls) == 2


def test_a_reopened_pr_still_reads_the_board_for_every_reader():
    gql, calls = counting([node(5, ST)])
    B.snapshot(gql)
    B.apply_event('pull_request', {'action': 'reopened', 'repository': {'full_name': B.REPO},
                                   'pull_request': {'number': 9}})
    B.snapshot(gql, stale_ok=True)
    assert len(calls) == 2


def test_an_older_issue_event_never_rolls_back_a_newer_label_or_milestone():
    """Codex on PR #138: per-field freshness for issue patches, on the issue node and on each
    PR's node of it. unlabeled (newer) then labeled (older): fast-track stays removed."""
    gql, calls = counting([node(5, ST, closes=[40]), node(40, BL, kind='Issue')])
    B.snapshot(gql)
    B.apply_event('issues', issue_event('labeled', 40, labels=[{'name': 'fast-track'}], updated_at='2026-11-17T17:00:05Z'))
    B.apply_event('issues', issue_event('unlabeled', 40, labels=[], updated_at='2026-11-17T17:00:09Z'))
    B.apply_event('issues', issue_event('labeled', 40, labels=[{'name': 'fast-track'}], updated_at='2026-11-17T17:00:07Z'))
    B.apply_event('issues', issue_event('milestoned', 40, milestone={'title': 'MVP'}, updated_at='2026-11-17T17:00:09Z'))
    B.apply_event('issues', issue_event('demilestoned', 40, milestone=None, updated_at='2026-11-17T17:00:01Z'))
    snap = B.snapshot(gql)
    assert len(calls) == 1
    c5 = next(c for _, c in B.open_prs(snap) if c['number'] == 5)
    issue = next(c for _, c in B.open_issues(snap) if c['number'] == 40)
    assert B.pr_labels(c5) == [] and B._names(issue) == []
    assert issue['milestone']['title'] == 'MVP' and B.closes(c5)[0]['milestone']['title'] == 'MVP'


def test_an_issue_event_older_than_the_board_read_is_skipped():
    old = node(40, BL, kind='Issue')
    old['content']['updatedAt'] = '2026-11-17T17:00:10Z'
    old['content']['labels'] = {'nodes': [{'name': 'fast-track'}]}
    gql, calls = counting([old])
    B.snapshot(gql)
    B.apply_event('issues', issue_event('unlabeled', 40, labels=[], updated_at='2026-11-17T17:00:02Z'))
    issue = next(c for _, c in B.open_issues(B.snapshot(gql)) if c['number'] == 40)
    assert B._names(issue) == ['fast-track'] and len(calls) == 1


def test_an_issue_event_with_no_time_is_not_applied_and_marks_the_cache_stale():
    gql, calls = counting([node(5, ST, closes=[40]), node(40, BL, kind='Issue')])
    B.snapshot(gql)
    B.apply_event('issues', issue_event('labeled', 40, labels=[{'name': 'fast-track'}]))
    assert B.pr_labels(next(c for _, c in B.open_prs(B.snapshot(gql, stale_ok=True)))) == []
    assert len(calls) == 1
    B.snapshot(gql)
    assert len(calls) == 2


def test_issue_labels_and_milestones_are_followed_into_the_prs_that_close_it():
    """The fast-track label of #568 on 2026-11-17 made the cache dirty; now the event patches
    the issue and every PR that closes it (the pick order), with no board read."""
    gql, calls = counting([node(5, ST, closes=[40]), node(40, BL, kind='Issue')])
    B.snapshot(gql)
    B.apply_event('issues', issue_event('labeled', 40, labels=[{'name': 'bug'}, {'name': 'fast-track'}],
                                        updated_at='2026-11-17T17:00:01Z'))
    B.apply_event('issues', issue_event('milestoned', 40, milestone={'title': 'MVP', 'due_on': '2026-12-01T08:00:00Z'},
                                        updated_at='2026-11-17T17:00:02Z'))
    snap = B.snapshot(gql)
    assert len(calls) == 1
    c5 = next(c for _, c in B.open_prs(snap) if c['number'] == 5)
    assert 'fast-track' in B.pr_labels(c5)
    assert B.pr_due(c5) == ('2026-12-01', 'MVP') or B.pr_due(c5)[1] == 'MVP'
    issue = next(c for _, c in B.open_issues(snap) if c['number'] == 40)
    assert B._names(issue) == ['bug', 'fast-track'] and issue['milestone']['title'] == 'MVP'
    B.apply_event('issues', issue_event('demilestoned', 40, milestone=None, updated_at='2026-11-17T17:00:03Z'))
    snap = B.snapshot(gql)
    assert len(calls) == 1 and next(c for _, c in B.open_issues(snap) if c['number'] == 40)['milestone'] is None


def test_a_pr_milestone_is_followed():
    gql, calls = counting([node(5, ST)])
    B.snapshot(gql)
    B.apply_event('pull_request', {'action': 'milestoned', 'repository': {'full_name': B.REPO},
                                   'pull_request': {'number': 5, 'milestone': {'title': 'ptw 15', 'due_on': None}}})
    snap = B.snapshot(gql)
    assert len(calls) == 1 and B.milestones(next(c for _, c in B.open_prs(snap))) == ['ptw 15']


def test_a_stale_mark_during_a_board_read_is_kept_after_it(tmp_path):
    """The event came after the read started: the read may not have it, so the stale mark stays
    (as a dirty mark does)."""
    t = [1000.0]
    gql0, _ = counting([node(5, ST)])
    B.snapshot(gql0, now=lambda: t[0])

    def gql(q, **v):
        B.patch({'op': 'stale'}, now=lambda: t[0] + 1)  # an event while the board is read
        return boardfake.answer([node(5, ST)])
    t[0] = 1000.0 + B.MAX_AGE + 5  # the sync is due
    B.snapshot(gql, now=lambda: t[0])
    raw = B._load(B.PATH)
    assert raw.get('stale') and not raw.get('dirty')


def test_a_stale_event_during_the_first_read_marks_it_stale_not_dirty():
    def gql(q, **v):
        B.apply_event('issues', issue_event('opened', 30))
        return boardfake.answer([node(5, ST)])
    B.snapshot(gql)
    raw = B._load(B.PATH)
    assert raw.get('stale') and not raw.get('dirty')


class Lock:
    def __init__(self, order):
        self.order = order

    def __enter__(self):
        self.order.append('lock')
        return self

    def __exit__(self, *a):
        self.order.append('unlock')
        return False


def test_the_resync_runs_after_the_refill_drain_and_outside_the_lock(monkeypatch):
    """Codex on PR #138: a stale cache was read again in side_work, before the drain of the
    queued refill cascade and under the serial lock: one board read before Ready -> Staging.
    Now the read runs last, after the drain and the unlock."""
    order = []
    cols = {HR: [{'number': 1}], ST: [{'number': 5}]}
    monkeypatch.setattr(P, 'read_columns', lambda gql, **k: cols)
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: Lock(order))
    monkeypatch.setattr(P.gatelock, 'queued', lambda: [])
    p = P.Promoter(g={'gql': None})
    monkeypatch.setattr(p, 'fill', lambda c: order.append('move') or (c, [5], 2))
    monkeypatch.setattr(p, 'queue_cascade', lambda why, now=None: order.append('queue'))
    monkeypatch.setattr(p, 'side_work', lambda c: order.append('side') or (None, False, []))
    monkeypatch.setattr(p, 'drain', lambda: order.append('drain') or [])
    monkeypatch.setattr(p, 'resync', lambda: order.append('resync'))
    assert p.once() == 'promoted'
    assert order == ['lock', 'move', 'queue', 'side', 'drain', 'unlock', 'resync']


def test_an_idle_check_resyncs_and_a_dry_run_never_does(monkeypatch):
    order = []
    monkeypatch.setattr(P, 'read_columns', lambda gql, **k: {HR: [{'number': 1}, {'number': 2}]})
    monkeypatch.setattr(P.gatelock, 'queued', lambda: [])
    for dry, want in ((False, ['side', 'resync']), (True, ['side'])):
        order.clear()
        p = P.Promoter(g={'gql': None}, dry_run=dry)
        monkeypatch.setattr(p, 'side_work', lambda c: order.append('side') or (None, False, []))
        monkeypatch.setattr(p, 'resync', lambda: order.append('resync'))
        assert p.once() == 'idle' and order == want


def test_the_daemon_resyncs_a_stale_cache_after_the_move(tmp_path):
    gql, calls = counting([node(5, ST)])
    B.snapshot(gql)
    B.apply_event('issues', issue_event('opened', 30))
    p = P.Promoter(g={'gql': gql})
    p.resync()
    assert len(calls) == 2


# -- 1. the webhook wakes swarm-promoted for every freed Human Review slot ---------------------

def michal_move(frm, to):
    return {'action': 'edited', 'sender': {'login': W.MICHAL},
            'projects_v2_item': {'content_node_id': 'PR_x', 'node_id': 'IX', 'project_node_id': board.PROJECT_ID},
            'changes': {'field_value': {'field_node_id': board.STATUS_FIELD, 'from': {'id': board.OPTION[frm]},
                                        'to': {'id': board.OPTION[to]}}}}


class Ex(W.Executor):
    def __init__(self, col=HR, order=None):
        self.order = order if order is not None else []
        super().__init__(g={'project_id': lambda: 'P', 'STATUS_FIELD': 'F', 'gql': lambda q, **v: {}},
                         sh=lambda argv: self.order.append(('sh', argv[:3])) or '',
                         cascade=lambda: self.order.append('cascade') or 'queued',
                         give=lambda n, text: self.order.append(('relay', n)) or (True, 'sent'),
                         wake_post=lambda payload: None,
                         signal_promoted=lambda: self.order.append('signal') or 'woken (test)')
        self.col = col

    def card(self, n):
        return ('ITEM', self.col)

    def pr_of(self, node):
        self.order.append('pr_of')
        return 528

    def comments_since_head(self, n):
        return []


def test_a_reject_wakes_swarm_promoted_before_the_slow_relay():
    """platform #528 on 2026-11-17: the reject queued the cascade, but nothing woke the daemon:
    about 13 s until its next poll. Now the wake and the cascade go first."""
    ex = Ex()
    W.handle('projects_v2_item', michal_move(HR, IP), ex=ex)
    assert ex.order[:2] == ['signal', 'cascade']
    assert ex.order.index('pr_of') > 1 and ('relay', 528) in ex.order
    assert W.PROMOTED_WOKEN == []


@pytest.mark.parametrize('to', [board.DONE, ST, RD, board.NEEDS_INPUT])
def test_any_other_move_out_of_human_review_wakes_swarm_promoted(to):
    ex = Ex()
    W.handle('projects_v2_item', michal_move(HR, to), ex=ex)
    assert ex.order == ['signal', 'cascade']


def test_a_move_that_frees_no_human_review_slot_sends_no_signal():
    ex = Ex()
    W.handle('projects_v2_item', michal_move(ST, board.DONE), ex=ex)
    assert ex.order == ['cascade']


@pytest.mark.parametrize('col,cascade', [(HR, True), (ST, True), (IP, False)])
def test_a_close_without_a_merge_wakes_first_and_restocks_a_freed_slot(col, cascade):
    ex = Ex(col=col)
    W.handle('pull_request', {'action': 'closed', 'repository': {'full_name': W.REPO},
                              'pull_request': {'number': 9, 'merged': False, 'base': {'ref': 'main'}}}, ex=ex)
    assert ex.order[0] == 'signal'
    assert ('cascade' in ex.order) is cascade


def test_a_close_on_another_board_sends_no_signal():
    ex = Ex(col=HR)
    ex.run(W.Action('closed', 9, args={'repo': 'tangentcode/tangentswarm'}))
    assert 'signal' not in ex.order


# -- 3. Ready -> Staging: no gate re-run ---------------------------------------------------------

SHOT = 'https://github.com/user-attachments/assets/0f0e0d0c-0b0a-0908-0706-050403020100'
GOOD_BODY = f'Fixes #1\n\n![page]({SHOT})\n\nWhere to see: work.pawntakesworld.com/play'


class G(dict):
    def __init__(self, files=('web/a.ts',), served='w' * 40, body=GOOD_BODY):
        super().__init__()
        self.moves, self.logged, self.tools, self.fetched, self.fresh = [], [], [], [], {}
        self.update(move=lambda item, col: self.moves.append((item, col)), OPT={ST: 'x', HR: 'y'},
                    log=lambda *a: self.logged.append(a), gql=None, PARKED=set(),
                    fetch=lambda n, refresh=False: self.fetched.append(refresh) or {
                        'number': n, 'body': body, 'files': {'nodes': [{'path': f} for f in files]},
                        'state': 'OPEN', 'isDraft': False, 'headRefOid': f'{n:040d}',
                        'commits': {'nodes': [{'commit': {'committedDate': '2026-11-17T17:00:00Z'}}]}, **self.fresh},
                    evaluate=lambda pr, **k: pytest.fail('no gate re-run for a pre-merged Ready PR'),
                    list_hr_prs=lambda refresh=False: pytest.fail('no Human Review read'),
                    served_all=lambda: [('ptw', 'work.pawntakesworld.com', served, None)],
                    contains=lambda a, b: False, work_broken=lambda gc: None,
                    merge_tree_clean=lambda d, a, b: True)


class Scratch:
    """hr-gate's scratch repo: main is a SHA, so no git runs (Flow.sha_of is stubbed)."""
    dir, main = '/no-such-scratch', 'a' * 40

    def __init__(self, fail=None):
        self.fail, self.ensured = fail, []

    def ensure_main(self):
        if self.fail:
            raise self.fail

    def ensure(self, *shas):
        self.ensured += shas


def ready_card(n=7):
    return dict(number=n, state='OPEN', draft=False, branch=f'b{n}', head=f'{n:040d}', title='t', col=RD,
                item=f'I{n}', rank=1, parked=False, body='')


def full_hr(card):
    """The board with card in Ready and a full Human Review (#1, #2): a pulled PR lands in
    Staging (tangentswarm #144: fill.landing)."""
    hr = {n: dict(ready_card(n), col=HR) for n in (1, 2)}
    return {**hr, card['number']: card}


def pull(n=7, **kw):
    d = dict(pr=n, head=f'{n:040d}', branch=f'b{n}', item=f'I{n}', tip='v' * 40, work='w' * 40, started=10_000,
             pre_merged=True)
    d.update(kw)
    flow.save_pull(d)
    return d


def make(g, dry=False):
    f = flow.Flow(dry_run=dry, out=lambda line: None, g=g, now=lambda: 10_000)
    f.sha_of = lambda scratch, ref: ref
    f.tool = lambda argv: pytest.fail(f'no tool run: {argv}')
    return f


def test_a_pre_merged_ready_pr_moves_to_staging_with_no_gate_run(monkeypatch):
    """platform #547 on 2026-11-17: 72 s of hr-gate --apply before Ready -> Staging. Now: the
    card move, the gate pass line for its head (the promote fast path reads it) and the move
    log. Nothing else."""
    monkeypatch.setattr(P, 'HR_TARGET', 2)
    d = pull()
    g = G()
    f = make(g)
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert g.moves == [('I7', ST)] and flow.load_pulls() == []
    assert g.logged == [('apply', 7, d['head'][:8], 'PASS', [])]
    assert any('Ready -> Staging' in x and 'no gate re-run' in x for x in f.did)
    line = f"t\tapply\t#7\t{d['head'][:8]}\tPASS\tfail=-"
    assert P.gate_times(line.replace('t\t', '2026-11-17T12:38:41-05:00\t', 1))  # the format the fast path reads


def test_a_pre_merged_pull_waits_until_its_priority_site_serves_it():
    d = pull()
    g = G(served='o' * 40)
    f = make(g)
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert g.moves == [] and flow.load_pull()['pr'] == 7
    assert any('not served yet' in n for n in f.notes)
    f.advance(d, full_hr(ready_card()), scratch=Scratch(), served=True)  # the cascade checked it already
    assert g.moves == [('I7', ST)]


def test_a_pre_merged_pull_with_no_served_file_moves_at_once():
    d = pull()
    g = G(files=('docs/x.md',), served='o' * 40, body='Where to see: docs only')
    make(g).advance(d, full_hr(ready_card()), scratch=Scratch())
    assert g.moves == [('I7', ST)]


def test_a_dry_run_says_what_it_would_move():
    d = pull()
    g = G()
    f = make(g, dry=True)
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert g.moves == [] and g.logged == [] and f.did == ['would: #7 Ready -> Staging (pre-merged tip in work; no gate re-run)']


def test_a_draft_or_an_older_pull_record_still_takes_the_gate():
    """Only a pre-merged pull of a Ready, non-draft card skips the gate; anything else keeps the
    old path (a record from before #136 has no pre_merged)."""
    for d, card in ((pull(pre_merged=False), ready_card()), (pull(), dict(ready_card(), draft=True))):
        g = G()
        ran = []
        g['evaluate'] = lambda pr, **k: ran.append(1) or ([('1 not draft', False, 'draft')], None)
        g['list_hr_prs'] = lambda refresh=False: []
        f = make(g)
        f.leave = lambda p, why, tell=True: None
        f.advance(d, {7: card}, scratch=Scratch())
        assert ran == [1] and g.moves == []


@pytest.mark.parametrize('files,body,why', [
    (('web/a.ts',), 'Where to see: /play', '7a: no image in body'),                       # UI, no screenshot
    (('web/a.ts',), f'![x](shot.png)\nWhere to see: /play', '7a:'),                     # a relative name
    (('api/x.py',), 'Fixes #1, no line', '7b: no where-to-see line'),                     # no where-to-see
])
def test_ready_does_not_prove_the_body_evidence_so_a_gap_takes_the_full_gate(files, body, why):
    """Codex on PR #138: the MinBot pass (Ready) checks rows 1, 2, 3 and 5 only, so a UI PR may
    reach Ready while its screenshot job runs. The fast move checks 7a and 7b from ONE fresh PR
    read; a gap sends the pull down the old gate path (wait, screenshot job, timeout)."""
    d = pull()
    g = G(files=files, body=body)
    g['shots_exists'] = lambda path: True  # hr-gate's swarm-shots check (the live namespace has it)
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('7a screenshots current', False, 'no image in body')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    f.screenshot_job = lambda n, head, full=None: None
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert ran == [1] and g.moves == [] and g.logged == [] and flow.load_pull()['pr'] == 7
    assert any(why in n and 'the full gate runs' in n for n in f.notes)


@pytest.mark.parametrize('fresh,why', [
    ({'headRefOid': 'f' * 40}, 'the head is ffffffff, not the pulled'),  # a push the cache has not seen
    ({'isDraft': True}, 'the PR is a draft'),                            # a draft conversion not seen yet
    ({'state': 'CLOSED'}, 'the PR is CLOSED'),
])
def test_the_fast_move_needs_the_fresh_pr_at_the_pulled_head(fresh, why):
    """Codex on PR #138: the cache may lag GitHub. The fresh read must agree with the pull, or
    the old gate path runs on what GitHub says (no PASS for the old head, no move)."""
    d = pull()
    g = G()
    g.fresh = fresh
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('1 not draft', False, 'x')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    f.leave = lambda p, why, tell=True: None
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert ran == [1] and g.moves == [] and g.logged == []
    assert any(why in n and 'the full gate runs' in n for n in f.notes)


def storm(when, body='please rename it'):
    return {'author': {'login': 'tangentstorm'}, 'createdAt': when, 'body': body}


@pytest.mark.parametrize('fresh,blocks', [
    ({'comments': {'nodes': [storm('2026-11-17T17:05:00Z')]}}, True),               # a later comment
    ({'reviews': {'nodes': [dict(storm(None), submittedAt='2026-11-17T17:05:00Z', comments={'nodes': []})]}}, True),
    ({'reviews': {'nodes': [{'author': {'login': 'tangentstorm'}, 'submittedAt': '2026-11-17T17:05:00Z',
                             'body': '', 'comments': {'nodes': [storm('2026-11-17T17:06:00Z')]}}]}}, True),
    ({'reviewThreads': {'nodes': [{'comments': {'nodes': [storm('2026-11-17T17:05:00Z')]}}]}}, True),
    ({'comments': {'nodes': [storm('2026-11-17T16:55:00Z')]}}, False),              # before the head
    ({'comments': {'nodes': [storm('2026-11-17T17:05:00Z', '@codex review')]}}, False),
    ({'comments': {'nodes': [storm('2026-11-17T17:05:00Z', '### MinBot pass on abc')]}}, False),
    ({'comments': {'nodes': [dict(storm('2026-11-17T17:05:00Z'), author={'login': 'someone'})]}}, False),
    ({'reviews': {'nodes': [{'author': {'login': 'tangentstorm'}, 'submittedAt': '2026-11-17T17:05:00Z',
                             'body': '', 'comments': {'nodes': []}}]}}, False),  # an empty review body
    ({'commits': {'nodes': []}}, True),                                              # no date: the full gate decides
])
def test_a_later_human_request_takes_the_full_gate(fresh, blocks):
    """Codex on PR #138: row 8 (no later human request) is not proved by Ready either. The same
    rule as hr-gate: tangentstorm comments, reviews with a body, review comments and thread
    comments after the head commit, except "@codex review" and a MinBot pass."""
    d = pull()
    g = G()
    g.fresh = fresh
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('8 no later human request', False, '1 later')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    f.leave = lambda p, why, tell=True: None
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    if blocks:
        assert ran == [1] and g.moves == [] and any('8: ' in n and 'the full gate runs' in n for n in f.notes)
    else:
        assert ran == [] and g.moves == [('I7', ST)]


@pytest.mark.parametrize('broken,why', [
    (lambda gc: 'origin/work has conflict markers in web/a.ts', '6: origin/work has conflict markers'),
    (lambda gc: 'the latest work Redeploy run failed (work abcdef12)', '6: the latest work Redeploy run failed'),
    (None, '6: no work health check loaded'),                           # never a pass without it
    (lambda gc: (_ for _ in ()).throw(RuntimeError('no git')), '6: work health check failed'),
])
def test_a_broken_work_branch_takes_the_full_gate(broken, why):
    """Codex on PR #138: condition 6's entry safeguard (gatechecks.work_problem). The served
    .deploy-sha can hold the head while work is broken; then no PASS line, no move."""
    d = pull()
    g = G()
    if broken is None:
        del g['work_broken']
    else:
        g['work_broken'] = broken
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('6 deployed + served', False, 'work broken')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    f.leave = lambda p, why, tell=True: None
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert ran == [1] and g.moves == [] and g.logged == []
    assert any(why in n and 'the full gate runs' in n for n in f.notes)


def test_the_work_health_uses_hr_gates_gatechecks_and_skips_a_pr_with_no_served_file():
    from tangentswarm import gatechecks
    seen = []
    g = G(files=('docs/x.md',), body='Where to see: docs')
    g['work_broken'] = lambda gc: seen.append(gc) or 'broken'
    make(g).advance(pull(), full_hr(ready_card()), scratch=Scratch())
    assert seen == [] and g.moves == [('I7', ST)]
    g = G()
    g['work_broken'] = lambda gc: seen.append(gc) or None
    make(g).advance(pull(), full_hr(ready_card()), scratch=Scratch())
    assert seen == [gatechecks] and g.moves == [('I7', ST)]


def test_a_body_that_states_where_the_pr_stands_takes_the_full_gate():
    """tangentswarm #130 (merged on main while this PR was open): a body that says where the PR
    stands holds hr-gate --apply. The fast move must not skip that hold: the full path opens the
    incident and gives the worker the job."""
    d = pull()
    g = G(body=GOOD_BODY + '\n\nMerged into work.')
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('1 not draft', True, 'ok')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    f.worker_jobs = lambda *a, **k: None
    f.advance(d, full_hr(ready_card()), scratch=Scratch())
    assert ran == [1] and g.moves == [] and g.logged == []
    assert any('tangentswarm #130' in n and 'the full gate runs' in n for n in f.notes)


@pytest.mark.parametrize('scratch,clean,why', [
    (Scratch(), False, '2: head 00000000 conflicts with origin/main aaaaaaaa'),  # main moved during the pull
    (None, True, '2: no scratch repo for the merge check'),
    (Scratch(fail=RuntimeError('fetch failed')), True, '2: merge check failed: RuntimeError'),
])
def test_row_2_is_checked_again_against_the_current_main(scratch, clean, why):
    """Codex on PR #138: main can move while the pull waits for its deploy. The fast move checks
    the head onto CURRENT origin/main (local merge-tree, hr-gate row 2). A conflict, or no way
    to check, takes the full gate path: a row-2 conflict there goes to rework (merge-main)."""
    d = pull()
    g = G()
    g['merge_tree_clean'] = lambda dir_, a, b: clean
    ran = []
    g['evaluate'] = lambda pr, **k: ran.append(1) or ([('2 mergeable onto main', False, 'CONFLICTS with origin/main')], None)
    g['list_hr_prs'] = lambda refresh=False: []
    f = make(g)
    failed = []
    f.fail = lambda p, reason, relay=None, text=None: failed.append((p['number'], relay))
    f.advance(d, full_hr(ready_card()), scratch=scratch)
    assert ran == [1] and g.moves == [] and g.logged == [] and failed == [(7, 'merge-main')]
    assert any(why in n and 'the full gate runs' in n for n in f.notes)


def test_the_row_2_check_merges_the_pulled_head_onto_the_current_main():
    seen = []
    g = G()
    g['merge_tree_clean'] = lambda dir_, a, b: seen.append((a, b)) or True
    sc = Scratch()
    make(g).advance(pull(), full_hr(ready_card()), scratch=sc)
    assert seen == [('a' * 40, f'{7:040d}')] and f'{7:040d}' in sc.ensured and g.moves == [('I7', ST)]


def test_the_fast_move_reads_the_pr_fresh_and_uses_hr_gates_image_check():
    d = pull()
    g = G()
    seen = []
    g['shots_exists'] = lambda path: seen.append(path) or True
    make(g).advance(d, full_hr(ready_card()), scratch=Scratch())
    assert g.fetched[0] is True and g.moves == [('I7', ST)]


def test_the_pull_record_says_it_is_pre_merged():
    p = ready_card()
    assert flow.pull_record(p, tip='v', pre_merged=True)['pre_merged'] is True


# -- 4. the staging deploy starts at once ------------------------------------------------------

def test_the_start_is_exactly_the_allowed_sudo_line():
    seen = []
    ok = stagingdeploy.REAL_START('work abc', sh=lambda *a, **k: seen.append(a) or subprocess.CompletedProcess(a, 0, '', ''))
    assert seen == [('sudo', '-n', '/usr/bin/systemctl', 'start', '--no-block', 'ptw-staging-deploy.service')]
    assert ok == 'started ptw-staging-deploy (work abc)'


def test_a_failed_start_never_raises_and_says_the_timer_deploys(monkeypatch):
    bad = stagingdeploy.REAL_START('x', sh=lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'a password is required'))
    assert 'rc=1' in bad and 'password' in bad and 'timer' in bad

    def boom(*a, **k):
        raise OSError('no sudo')
    assert 'OSError' in stagingdeploy.REAL_START('x', sh=boom)
    monkeypatch.setenv('SWARM_STAGING_DEPLOY_START', '0')
    assert 'off' in stagingdeploy.REAL_START('x', sh=lambda *a, **k: pytest.fail('turned off'))


def test_each_push_to_work_starts_the_deploy(monkeypatch):
    pushed = []

    def git(scratch, *args, check=True):
        if args[0] == 'push':
            pushed.append(args[-1])
        return subprocess.CompletedProcess(args, 0, 'c' * 40 + '\n', '')
    monkeypatch.setattr(flow, 'git', git)
    monkeypatch.setattr(flow, 'merge_commit', lambda scratch, a, b, msg, why=None: 'm' * 40)
    f = flow.Flow(dry_run=False, out=lambda line: None, g={'origin_url': lambda: 'o'}, now=lambda: 1)
    monkeypatch.setattr(f, 'fetch_work', lambda scratch: 'w' * 40)
    monkeypatch.setattr(f, 'onto', lambda scratch, base, tip, msg: 't' * 40)
    monkeypatch.setattr(f, 'work_commit', lambda scratch, work, tree, tips, msg: 'p' * 40)

    f.merge_into_work(None, 7, 'v' * 40, 'b7', pre_merged=True, fm=('k', 'fm', None))
    assert pushed == ['p' * 40 + ':refs/heads/work'] and len(stagingdeploy.STARTED) == 1
    monkeypatch.setattr(flow, 'git', lambda scratch, *a, check=True: (
        pushed.append(a[-1]) if a[0] == 'push' else None) or subprocess.CompletedProcess(a, 1, '', ''))
    f.merge_into_work(None, 8, 'v' * 40, 'b8')  # a plain tip that is not in work yet
    assert len(stagingdeploy.STARTED) == 2 and 'work mmmmmmmm: #8' in stagingdeploy.STARTED[-1]


def test_a_dry_run_push_starts_nothing(monkeypatch):
    monkeypatch.setattr(flow, 'git', lambda scratch, *a, check=True: subprocess.CompletedProcess(a, 1, '', ''))
    monkeypatch.setattr(flow, 'merge_commit', lambda scratch, a, b, msg, why=None: 'm' * 40)
    f = flow.Flow(dry_run=True, out=lambda line: None, g={'origin_url': lambda: 'o'}, now=lambda: 1)
    monkeypatch.setattr(f, 'fetch_work', lambda scratch: 'w' * 40)
    f.merge_into_work(None, 8, 'v' * 40, 'b8')
    assert stagingdeploy.STARTED == []


def watcher(sites):
    p = P.Promoter(g={'served_all': lambda: [(k, f'{k}.example', v, None) for k, v in sites.items()]})
    queued, poked = [], []
    p.queue_cascade = lambda why, now=None: queued.append(why)
    p.poke = lambda: poked.append(1)
    return p, queued, poked


def test_a_deploy_end_with_a_pull_in_flight_queues_the_cascade_and_wakes_the_loop_at_once():
    """2026-11-17: 25 s from the new .deploy-sha to the cascade (the next 30 s poll)."""
    sites = {'ptw': 'a' * 40, 'work': 'a' * 40}
    p, queued, poked = watcher(sites)
    work = lambda: 'a' * 40  # noqa: E731
    p.deploy_watch(work_sha=work)  # the first look
    flow.save_pull(dict(pr=9, head='9' * 40, work='b' * 40, started=0))
    queued.clear(), poked.clear()
    assert p.deploy_watch(work_sha=work) == [] and queued == []  # nothing changed
    sites['ptw'] = 'b' * 40
    p.deploy_watch(work_sha=lambda: 'b' * 40)
    assert len(queued) == 1 and 'ptw bbbbbbbb' in queued[0] and poked == [1]
    assert stagingdeploy.STARTED == []  # ptw serves origin work: no start


def test_a_deploy_that_ended_behind_origin_work_is_started_again_once():
    """cornerbot 2026-11-17 1:07 PM ET: a start during a running deploy is a no-op, so when that
    deploy ends and origin work moved past what it built, the swarm starts the unit again."""
    sites = {'ptw': 'a' * 40}
    p, queued, poked = watcher(sites)
    started = []
    start = lambda why: started.append(why) or 'started (test)'  # noqa: E731
    p.deploy_watch(work_sha=lambda: 'c' * 40, start=start)  # the first look: never a start
    assert started == []
    sites['ptw'] = 'b' * 40  # a deploy ended, but work is at c
    p.deploy_watch(work_sha=lambda: 'c' * 40, start=start)
    assert len(started) == 1 and 'origin work is cccccccc' in started[0]
    sites['ptw'] = 'd' * 40  # another end, still behind the same work: no second start
    p.deploy_watch(work_sha=lambda: 'c' * 40, start=start)
    assert len(started) == 1
    sites['ptw'] = 'c' * 40
    p.deploy_watch(work_sha=lambda: 'c' * 40, start=start)
    assert len(started) == 1


def test_the_each_second_thread_runs_the_deploy_watch(monkeypatch):
    p = P.Promoter(g={}, dry_run=False)
    ran = []
    monkeypatch.setattr(p, 'deploy_watch', lambda: ran.append(1))
    monkeypatch.setattr(p, 'retry_p6launch', lambda: None)
    p.events = type('E', (), {'mn_pending': False, 'poll': lambda self: []})()
    p.each_second()
    assert ran == [1]


def test_the_deploy_watch_reads_the_deploy_sha_files_only(tmp_path):
    """Every second: never hr-gate's served_all (it runs git in each checkout)."""
    (tmp_path / 'ptw.sha').write_text('a' * 40 + '\n')
    g = {'SITES': {'ptw': ('work.pawntakesworld.com', str(tmp_path / 'ptw.sha'), '/x'),
                   'work': ('work.example', str(tmp_path / 'missing'), '/y')},
         'served_all': lambda: pytest.fail('no git per second')}
    p = P.Promoter(g=g)
    assert p.deploy_shas() == [('ptw', 'work.pawntakesworld.com', 'a' * 40, None),
                               ('work', 'work.example', None, f"cannot read {tmp_path / 'missing'}: No such file or directory")]
    assert p.deploy_watch(work_sha=lambda: 'a' * 40) == [] and p.deploy_seen == 'a' * 40
