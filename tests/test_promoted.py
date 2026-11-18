"""swarm-promoted: Human Review below 2 -> move the gated Staging head first, then the slow step."""
import pytest

from tangentswarm import board, promoted as P

H5, H6 = '5' * 40, '6' * 40


def page(*items):
    from boardfake import answer
    return answer(list(items))


def item(n, opt, head, state='OPEN', repo=P.REPO):
    return {'id': f'I{n}', 'fieldValueByName': {'name': 'whatever', 'optionId': opt},
            'content': {'__typename': 'PullRequest', 'number': n, 'state': state, 'headRefOid': head,
                        'repository': {'nameWithOwner': repo}}}


HR, ST = board.OPTION[board.HUMAN_REVIEW], board.OPTION[board.STAGING]


@pytest.fixture(autouse=True)
def own_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(P.gatelock, 'QUEUE_DIR', tmp_path / 'queue')
    return tmp_path / 'queue'


def test_read_columns_counts_open_prs_by_option_id_only():
    cols = P.read_columns(lambda q, **v: page(item(1, HR, 'a'), item(2, HR, 'b', state='MERGED'),
                                                 item(5, ST, H5), item(9, ST, 'z', repo='other/repo')))
    assert [p['number'] for p in cols[board.HUMAN_REVIEW]] == [1]
    assert [p['number'] for p in cols[board.STAGING]] == [5]
    assert P.needs_promotion(cols)


def test_no_promotion_when_human_review_is_full_or_staging_empty():
    assert not P.needs_promotion({board.HUMAN_REVIEW: [1, 2], board.STAGING: [3]})
    assert not P.needs_promotion({board.HUMAN_REVIEW: [1]})


LOG = ("2026-11-13T19:00:00-05:00\tapply\t#5\t55555555\tPENDING\tfail=-\tcaller=x\n"
       "2026-11-13T19:00:00-05:00\tapply\t#6\t66666666\tBLOCKED\tfail=4\tcaller=x\n"
       "2026-11-13T19:00:00-05:00\tapply\t#7\t77777777\tPASS\tfail=-\tcaller=x\n"
       "2026-11-13T19:05:00-05:00\tapply\t#7\t77777777\tRELAY\tfail=9\tcaller=x\n")
GATED = P._when('2026-11-13T19:00:00-05:00')


def test_gate_times_take_the_latest_line_per_head():
    assert P.gate_times(LOG) == {('#5', '55555555'): GATED}
    assert P.gated_heads(LOG) == {('#5', '55555555')}


def test_staging_head_follows_the_shared_pick_order():
    """tangentswarm #35: the soonest milestone due date goes first, not hr-gate's queue order."""
    cols = {board.STAGING: [dict(number=5, head=H5, item='I5', due='2026-12-01', milestone='MVP'),
                            dict(number=6, head=H6, item='I6', due='2026-11-15', milestone='ptw 15')]}
    assert P.staging_head(cols, [{'pr': 5}, {'pr': 6}])['number'] == 6
    cols = {board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}
    assert P.staging_head(cols, [{'pr': 6}, {'pr': 5}])['number'] == 5  # no milestone: the lower number


def test_staging_order_puts_only_the_active_fast_track_first():
    st = [dict(number=5), dict(number=6), dict(number=7, closes=[488])]
    det = {6: {'closingIssuesReferences': {'nodes': [{'number': 490}]}}}
    q = [{'pr': 5}, {'pr': 6}, {'pr': 7}]
    assert [p['number'] for p in P.staging_order(st, det, q, active=490)] == [6, 5, 7]
    assert [p['number'] for p in P.staging_order(st, det, q, active=488)] == [7, 5, 6]
    assert [p['number'] for p in P.staging_order(st, det, q, active=None)] == [5, 6, 7]
    det = {6: {'closingIssuesReferences': {'nodes': [{'number': 490, 'repository': {'nameWithOwner': 'o/other'}}]}}}
    assert [p['number'] for p in P.staging_order(st, det, q, active=490)] == [5, 6, 7]  # another repo's 490


def test_read_columns_keeps_the_active_fast_track_current(tmp_path):
    from tangentswarm import board as B
    issue = {'id': 'X', 'fieldValueByName': {'optionId': B.OPTION[B.ACTIVE]},
             'content': {'__typename': 'Issue', 'number': 488, 'state': 'OPEN',
                         'repository': {'nameWithOwner': P.REPO}, 'labels': {'nodes': [{'name': 'fast-track'}]}}}
    P.read_columns(lambda q, **v: page(item(5, ST, H5), issue))
    assert B.active_fast_track() == 488


def note(kind, who, typ, when):
    key = 'createdAt' if kind == 'comments' else 'submittedAt'
    return {key: when, 'author': {'__typename': typ, 'login': who}}


def test_read_details_is_one_query_for_all_prs():
    qs = []
    out = P.read_details(lambda q: qs.append(q) or {'repository': {'p5': {'number': 5}, 'p6': None}}, [5, 6])
    assert out == {5: {'number': 5}} and len(qs) == 1 and 'p6: pullRequest(number:6)' in qs[0]


class Scratch:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ensure_main(self):
        return 'main'

    def ensure(self, *revs):
        pass


def detail(n, head, draft=False, base='main', body='Where to see: staging.', notes=()):
    return {'number': n, 'state': 'OPEN', 'headRefOid': head, 'isDraft': draft, 'baseRefName': base,
            'body': body, 'comments': {'nodes': list(notes)}, 'reviews': {'nodes': []}}


class G(dict):
    def __init__(self, tmp_path, details=None, queue=({'pr': 5},)):
        super().__init__()
        (tmp_path / 'hr.log').write_text(LOG)
        self.moves, self.logs = [], []
        details = details if details is not None else {5: detail(5, H5)}
        self.update(LOG=str(tmp_path / 'hr.log'), read_queue_file=lambda: list(queue),
                    gql=lambda q, **v: {'repository': {f'p{n}': d for n, d in details.items()}},
                    ScratchRepo=Scratch, stacked_base=lambda b: 3 if b.startswith('# STACKED') else None,
                    move=lambda i, c: self.moves.append((i, c)), log=lambda *a: self.logs.append(a))


COLS = {board.HUMAN_REVIEW: [dict(number=1, head='a', item='I1')],
        board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}


@pytest.fixture
def clean(monkeypatch):
    from tangentswarm import flow
    monkeypatch.setattr(flow, 'merge_commit', lambda s, a, b, m: f'{a}+{b}')


def test_fast_path_moves_the_gated_head_and_logs_a_pass(tmp_path, clean):
    g = G(tmp_path)
    assert P.Promoter(g=g).fast_path(COLS) == [5]
    assert g.moves == [('I5', board.HUMAN_REVIEW)] and g.logs == [('apply', 5, '55555555', 'PASS', [])]


def test_fast_path_fills_every_free_slot(tmp_path, clean):
    cols = {board.STAGING: COLS[board.STAGING] + [dict(number=8, head='8' * 40, item='I8')]}
    g = G(tmp_path, details={5: detail(5, H5), 8: detail(8, '8' * 40)})
    (tmp_path / 'hr.log').write_text(LOG + "2026-11-13T19:00:00-05:00\tapply\t#8\t88888888\tPASS\tfail=-\tc\n")
    assert P.Promoter(g=g).fast_path(cols) == [5, 8]


def test_fast_path_dry_run_moves_nothing(tmp_path, clean):
    g = G(tmp_path)
    assert P.Promoter(g=g, dry_run=True).fast_path(COLS) == [5] and g.moves == []


def test_once_runs_fast_then_slow_under_the_lock(tmp_path, monkeypatch):
    order = []
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)
    moved = []

    def fast(cols):
        order.append('fast')
        p.left = [] if moved else [(5, H5, 'head 55555555 has no gate pass')]
        return list(moved)
    monkeypatch.setattr(p, 'fast_path', fast)
    monkeypatch.setattr(p, 'slow_path', lambda: order.append('slow'))

    class L:
        def __enter__(self):
            order.append('lock')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    assert p.once() == 'promoted' and order == ['lock', 'fast', 'slow']  # nothing moved: slow path
    order.clear()
    moved.append(5)
    assert p.once() == 'promoted' and order == ['lock', 'fast']  # slot filled: no re-gate


def test_once_busy_then_retry_skips_the_outer_read(tmp_path, monkeypatch):
    reads = []
    g = G(tmp_path)
    g['gql'] = lambda q, **v: reads.append(1) or page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)

    class Busy:
        def __enter__(self):
            raise P.gatelock.Busy('pending-promote --apply pid 7')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: Busy())
    assert p.once() == 'busy' and len(reads) == 1
    assert p.once(retry=True) == 'busy' and len(reads) == 1


def test_once_idle_when_human_review_full(tmp_path):
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(2, HR, 'b'), item(5, ST, H5))
    assert P.Promoter(g=g).once() == 'idle'


def test_slow_path_only_regates_heads_without_a_gate_pass_and_never_merges(tmp_path, monkeypatch):
    from tangentswarm import flow
    monkeypatch.setattr(flow.Flow, 'promote', lambda self, cols: pytest.fail('no pending-promote'))
    p = P.Promoter(g=G(tmp_path))
    p.left = [(5, H5, 'head 55555555 has no gate pass'), (6, H6, 'is stacked: waits (no re-merge at promotion)'),
              (7, '7' * 40, 'conflicts with #1 in Human Review')]
    runs = []
    p.slow_path(run=lambda argv: runs.append(argv) or 0)
    assert runs == [[str(P.BIN / 'hr-gate'), '--apply', '5']]
    p.left = p.left[1:]          # stacked and conflicting PRs wait: nothing is due
    assert not p.slow_due(now=0) and p.regate() == []


def test_pull_path_never_promotes(tmp_path, monkeypatch):
    from tangentswarm import flow
    calls = []
    monkeypatch.setattr(flow.Flow, 'step', lambda self, promote=True: calls.append(f'step promote={promote}'))
    P.Promoter(g=G(tmp_path)).pull_path()
    assert calls == ['step promote=False']


def test_needs_pull_only_when_on_and_staging_short_with_ready(monkeypatch):
    monkeypatch.setattr(P, 'PULL', False)
    assert not P.needs_pull({board.READY: [1]}, None)
    monkeypatch.setattr(P, 'PULL', True)
    assert P.needs_pull({board.READY: [1]}, None)
    assert P.needs_pull({}, {'pr': 4})  # a running pull must advance
    full = {board.HUMAN_REVIEW: [5, 6]}
    assert not P.needs_pull(dict(full, **{board.READY: [1], board.STAGING: [1, 2, 3]}), None)
    # tangentswarm #144: two of the three Staging PRs fill Human Review, so Staging is short again
    assert P.needs_pull({board.READY: [1], board.STAGING: [1, 2, 3]}, None)
    assert not P.needs_pull({}, None)


def test_slow_path_runs_once_per_leftover_head_then_waits_for_the_backstop(tmp_path):
    p = P.Promoter(g=G(tmp_path))
    p.left = [(5, H5, 'head has no gate pass')]
    assert p.slow_due(now=1000)
    p.mark_tried(now=1000)
    assert not p.slow_due(now=1000 + P.BACKSTOP - 1)
    assert p.slow_due(now=1000 + P.BACKSTOP)
    p.left = [(5, 'f' * 40, 'head has no gate pass')]  # a new head runs at once
    assert p.slow_due(now=1001)


def test_once_does_not_rerun_the_slow_path_for_the_same_blocked_head(tmp_path, monkeypatch):
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)
    slow = []

    def fast(cols):
        p.left = [(5, H5, 'head has no gate pass')]
        return []
    monkeypatch.setattr(p, 'fast_path', fast)
    monkeypatch.setattr(p, 'slow_path', lambda: slow.append(1))
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: P._null())
    p.once()
    p.once()
    assert slow == [1]


def test_once_rechecks_staging_only_when_main_moves(tmp_path, monkeypatch):
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(2, HR, 'b'), item(5, ST, H5))
    p = P.Promoter(g=g)
    mains, passes = ['1' * 40], []
    monkeypatch.setattr(p, 'main_sha', lambda: mains[0])
    monkeypatch.setattr(p, 'main_pass', lambda cols, moved, main: passes.append(main))
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: P._null())
    assert p.once() == 'promoted' and passes == ['1' * 40]  # first sight of main
    assert p.once() == 'idle' and len(passes) == 1          # Human Review full, main unchanged
    mains[0] = '2' * 40
    assert p.once() == 'promoted' and passes[-1] == '2' * 40


def test_no_held_merge_main_retry_without_a_new_main(tmp_path, monkeypatch):
    """tangentswarm #101 (drop parking): the recheck never holds a Staging PR, so the daemon
    has no merge-main retry: with main unchanged it stays idle."""
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(2, HR, 'b'), item(5, ST, H5))
    p = P.Promoter(g=g)
    passes = []
    monkeypatch.setattr(p, 'main_sha', lambda: '1' * 40)
    monkeypatch.setattr(p, 'main_pass', lambda cols, moved, main: passes.append(main))
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: P._null())
    assert p.once() == 'promoted' and len(passes) == 1
    assert p.once() == 'idle' and len(passes) == 1
    assert not hasattr(p, 'main_retry_due')


def test_the_daemon_start_clears_the_retired_held_file(tmp_path, monkeypatch):
    """tangentswarm #101: held.json is retired. The daemon deletes it once at start and names
    the PRs it held; the next passes find each conflict again."""
    import json
    from tangentswarm import flow
    flow.HELD.write_text(json.dumps({'516': {'head': 'h' * 40, 'why': 'conflicts'}, '7': {}}))
    said = []
    monkeypatch.setattr(P, 'say', said.append)
    monkeypatch.setattr(P, 'Promoter', lambda dry_run=False: type('X', (), {'once': lambda self: 'idle'})())
    assert P.main(['--once']) == 0
    assert not flow.HELD.exists()
    assert any(l.startswith('parking retired: cleared held.json (#7, #516)') for l in said)
    assert flow.clear_held() == []  # no file: nothing to do


def test_main_sha_reads_ls_remote_and_survives_errors(tmp_path):
    import subprocess
    g = G(tmp_path)
    g['origin_url'] = lambda: 'url'
    g['sh'] = lambda *a, check=True: subprocess.CompletedProcess(a, 0, 'c' * 40 + '\trefs/heads/main\n', '')
    assert P.Promoter(g=g).main_sha() == 'c' * 40
    g['sh'] = lambda *a, check=True: subprocess.CompletedProcess(a, 128, '', 'fatal')
    assert P.Promoter(g=g).main_sha() is None


def test_once_drains_queued_requests_under_its_lock(tmp_path, monkeypatch, own_queue):
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(2, HR, 'b'))  # nothing else to do
    p = P.Promoter(g=g)
    monkeypatch.setattr(p, 'main_sha', lambda: None)
    order = []

    class L:
        def __enter__(self):
            order.append('lock')

        def __exit__(self, *a):
            order.append('unlock')
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    monkeypatch.setattr(P.gatelock, 'run_held', lambda argv: order.append(argv[-1]) or 0)
    assert p.once() == 'idle'
    P.gatelock.enqueue(['/b/hr-gate', '--apply', '9'], qdir=own_queue)
    assert p.once() == 'promoted' and order == ['lock', '9', 'unlock']
    assert P.gatelock.queued(own_queue) == []


def test_an_empty_human_review_queues_the_cascade_at_once_then_after_a_backoff(tmp_path):
    """Michal 9:10 PM ET: Human Review 0 must run the cascade (it escalates if it cannot fill)."""
    from tangentswarm import advance
    p = P.Promoter(g={}, dry_run=False)
    hr1 = {board.HUMAN_REVIEW: [{'number': 1}]}
    assert p.cascade_due(hr1, False, now=0) is None
    assert p.cascade_due({}, False, now=10) == 'Human Review dropped to 0 (was 1)'
    p.queue_cascade('Human Review dropped to 0 (was 1)', now=10)
    assert advance.RERUN.exists()  # the queue call itself is stubbed (conftest): no real cascade
    assert p.cascade_due({}, False, now=20) is None                       # still empty: wait
    assert p.cascade_due({}, False, now=10 + P.CASCADE_EMPTY_HR) == 'Human Review is empty'
    p.last_main = 'a' * 40
    assert p.cascade_due(hr1, True, now=30) == 'main moved'


def test_a_deploy_sha_change_with_pulls_in_flight_queues_the_cascade(tmp_path):
    """Michal 2026-11-15: the cascade checks a redeploy once and never waits, so the daemon
    starts the next cascade when a staging .deploy-sha changes. File reads only."""
    from tangentswarm import flow
    sites = {'work': 'a' * 40, 'ptw': 'o' * 40}
    p = P.Promoter(g={'served_all': lambda: [(k, f'{k}.example', v, None) for k, v in sites.items()]})
    assert p.deploy_due() is None  # no pull in flight
    flow.save_pull(dict(pr=9, head='9' * 40, work='a' * 40, started=0))
    assert p.deploy_due() is None  # the same .deploy-sha values as last time
    sites['ptw'] = 'a' * 40
    assert p.deploy_due() == 'a staging .deploy-sha changed (ptw aaaaaaaa) with pulls in flight (#9)'
    assert p.deploy_due() is None
    fresh = P.Promoter(g=p.g)  # a daemon restart: one look at once
    assert fresh.deploy_due() == 'pulls in flight (#9): first .deploy-sha check'


def test_a_daemon_promote_queues_a_cascade_to_restock_staging(tmp_path, monkeypatch):
    """(2) A promote frees a Staging slot: the next pull starts at once, in the drain that
    follows under the same lock."""
    from tangentswarm import advance
    g = G(tmp_path)
    g['gql'] = lambda q, **v: page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)
    order = []
    monkeypatch.setattr(p, 'fast_path', lambda cols: [5])
    monkeypatch.setattr(p, 'queue_cascade', lambda why, now=None: order.append(why))
    monkeypatch.setattr(p, 'drain', lambda: order.append('drain') or [])

    class L:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    assert p.once() == 'promoted'
    assert order[-2:] == ['promoted #5: restock Staging', 'drain']


def test_the_spool_thread_serves_while_the_main_loop_is_busy(monkeypatch):
    """A drain under the lock can take minutes: worker requests must not wait for it."""
    import threading
    served = threading.Event()
    p = P.Promoter(g={}, dry_run=False)
    monkeypatch.setattr(p, 'each_second', served.set)
    stop = threading.Event()
    t = P.start_spool_thread(p, interval=0.01, stop=stop)
    try:
        assert served.wait(2)
    finally:
        stop.set()
        t.join(2)
    assert not t.is_alive()


def test_a_body_edit_that_removes_the_evidence_blocks_the_promotion():
    """Codex on PR #26: a body edit after the gate pass removed the screenshots, and the
    fast path still promoted the PR."""
    from tangentswarm import gatechecks
    ui = ['web/app.ts']
    assert gatechecks.body_problem(ui, 'Where to see: staging.\n![a](https://example.test/a.png)') is None
    assert gatechecks.body_problem(ui, 'Where to see: staging.') == 'no image in body (edited after the gate pass)'
    assert gatechecks.body_problem(['api/x.py'], 'Where to see: staging.') is None
    assert 'where-to-see' in gatechecks.body_problem(['api/x.py'], 'no line')


def test_a_staging_deploy_lag_over_ten_minutes_wakes_swarmbot_once_per_work_sha(tmp_path):
    """tangentswarm #37: the deploy-lag exception wake that replaces the deleted tick's check."""
    import subprocess
    g = G(tmp_path)
    work = ['w' * 40]
    sites = [('work', 'work.example.test', 'w' * 40, None), ('ptw', 'ptw.example.test', 'o' * 40, None)]
    g.update(origin_url=lambda: 'url', served_all=lambda: list(sites),
             sh=lambda *a, check=True: subprocess.CompletedProcess(a, 0, work[0] + '\trefs/heads/work\n', ''))
    p = P.Promoter(g=g)
    wakes = []

    def wake(n, why, key=None, source=''):
        wakes.append((key, why))
        return 'woke swarmbot'
    assert p.deploy_lag(now=1000, wake=wake) == []                       # first sight of this work SHA
    assert p.deploy_lag(now=1030, wake=wake) == []                       # within LAG_EVERY: not checked
    assert p.deploy_lag(now=1000 + P.DEPLOY_LAG - 1, wake=wake) == []    # under 10 minutes
    lag = p.deploy_lag(now=1000 + P.DEPLOY_LAG + 60, wake=wake)
    assert [k for k, _, _ in lag] == ['ptw'] and wakes[0][0] == 'deploy-lag:' + 'w' * 40
    assert 'ptw.example.test (oooooooo)' in wakes[0][1] and 'work.example.test' not in wakes[0][1]
    assert p.deploy_lag(now=1000 + P.DEPLOY_LAG + 3600, wake=wake) == [] and len(wakes) == 1  # once per SHA
    sites[1] = ('ptw', 'ptw.example.test', 'w' * 40, None)                 # caught up
    assert p.deploy_lag(now=1000 + P.DEPLOY_LAG + 3700, wake=wake) == [] and len(wakes) == 1
    work[0] = 'x' * 40                                                   # a new work SHA starts its own clock
    assert p.deploy_lag(now=5000, wake=wake) == []


def test_wake_exception_is_shared_and_deduped(monkeypatch):
    from tangentswarm import webhook as W
    posts = []
    t = [1_000_000.0]
    assert W.wake_exception(None, 'lag', key='k', now=lambda: t[0], post=posts.append, source='swarm-promoted') \
        == 'woke swarmbot: lag'
    assert W.wake_exception(None, 'lag', key='k', now=lambda: t[0] + 5, post=posts.append).startswith('exception already')
    assert posts[0]['source'] == 'swarm-promoted' and posts[0]['why'] == 'lag'



def _wakes(seen, base):
    from tangentswarm import webhook as W
    W.SEEN = seen
    for i in range(15):
        W.wake_exception(None, f'e{base + i}', key=f'k{base + i}', now=lambda: 1_000_000.0, post=lambda p: None)


def test_wake_state_is_safe_across_processes(tmp_path):
    """Codex on PR #41: swarm-webhook and swarm-promoted both write the wake state."""
    import json as _json
    import multiprocessing
    seen = tmp_path / 'seen.json'
    ctx = multiprocessing.get_context('fork')
    procs = [ctx.Process(target=_wakes, args=(seen, k * 100)) for k in range(4)]
    for pr_ in procs:
        pr_.start()
    for pr_ in procs:
        pr_.join(30)
        assert pr_.exitcode == 0
    st = _json.loads(seen.read_text())
    assert len(st['wake']) == 60  # every key recorded, none lost to a stale rewrite


# -- tangentswarm #67: fill Human Review until full, re-reading the board after each move ----

H8 = '8' * 40
LOG8 = LOG + "2026-11-13T19:00:00-05:00\tapply\t#8\t88888888\tPASS\tfail=-\tc\n"


def test_a_merge_seen_on_the_post_move_re_read_is_filled_in_the_same_pass(tmp_path, clean):
    """(b) Human Review is 1/2: #5 fills it. The re-read after that move shows #1 merged
    (a free slot again), so #8 moves in the same pass."""
    g = G(tmp_path, details={5: detail(5, H5), 8: detail(8, H8)})
    (tmp_path / 'hr.log').write_text(LOG8)
    cols = {board.HUMAN_REVIEW: [dict(number=1, head='a', item='I1')],
            board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=8, head=H8, item='I8')]}
    reads = []

    def reread():
        reads.append(1)
        return {board.HUMAN_REVIEW: [dict(number=5, head=H5, item='I5')] if len(reads) == 1 else [],
                board.STAGING: [dict(number=8, head=H8, item='I8')]}
    assert P.Promoter(g=g).fast_path(cols, reread=reread) == [5, 8]
    assert g.moves == [('I5', board.HUMAN_REVIEW), ('I8', board.HUMAN_REVIEW)] and len(reads) == 2


def test_a_pass_fills_every_free_slot_and_re_reads_after_each_move(tmp_path, clean):
    """(b) Two free slots: two moves, one board read after each. A lagging board that still
    shows the moved PR in Staging never moves it twice, and the cap holds."""
    g = G(tmp_path, details={5: detail(5, H5), 8: detail(8, H8)})
    (tmp_path / 'hr.log').write_text(LOG8)
    stale = {board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=8, head=H8, item='I8'),
                             dict(number=7, head='7' * 40, item='I7')]}
    reads = []
    assert P.Promoter(g=g).fast_path(stale, reread=lambda: reads.append(1) or stale) == [5, 8]
    assert g.moves == [('I5', board.HUMAN_REVIEW), ('I8', board.HUMAN_REVIEW)] and len(reads) == 2


def test_a_pass_that_starts_at_cap_fills_a_slot_freed_during_the_pass(tmp_path, monkeypatch, own_queue, clean):
    """(b) Under the lock Human Review is 2/2; a merge lands while the queued work drains.
    The read after the drain sees the free slot, and #5 moves in this same pass."""
    full = page(item(1, HR, 'a'), item(2, HR, 'b'), item(5, ST, H5))
    free = page(item(1, HR, 'a'), item(5, ST, H5))
    boards = [full, full]  # the idle read and the read under the lock; then the drain frees a slot
    g = G(tmp_path)
    det = {'repository': {'p5': detail(5, H5)}}
    g['gql'] = lambda q, **v: det if 'pullRequest(number:' in q else (boards.pop(0) if boards else free)
    p = P.Promoter(g=g)
    monkeypatch.setattr(p, 'main_sha', lambda: None)
    monkeypatch.setattr(p, 'queue_cascade', lambda why, now=None: None)

    class L:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    from tangentswarm import boardcache
    # tangentswarm #116: the merge event during the drain patches the cache; no board read
    merged = {'action': 'closed', 'repository': {'full_name': P.REPO},
              'pull_request': {'number': 2, 'merged': True}}
    monkeypatch.setattr(P.gatelock, 'run_held', lambda argv: boardcache.apply_event('pull_request', merged) and 0)
    P.gatelock.enqueue(['/b/hr-gate', '--apply', '9'], qdir=own_queue)
    assert p.once() == 'promoted'
    assert g.moves == [('I5', board.HUMAN_REVIEW)]


def test_a_pr_moved_in_this_pass_that_merges_frees_its_slot_again(tmp_path, clean):
    """Codex on PR #68: #5 moves (the re-read lags: it is still in Staging, so it counts),
    then #8 moves. The re-read after #8 shows #5 in no column (merged, not lag): its slot
    is free again, so #9 moves in the same pass."""
    H9 = '9' * 40
    g = G(tmp_path, details={5: detail(5, H5), 8: detail(8, H8), 9: detail(9, H9)})
    (tmp_path / 'hr.log').write_text(LOG8 + "2026-11-13T19:00:00-05:00\tapply\t#9\t99999999\tPASS\tfail=-\tc\n")
    s5, s8, s9 = (dict(number=n, head=h, item=f'I{n}') for n, h in ((5, H5), (8, H8), (9, H9)))
    reads = [{board.STAGING: [s5, s8, s9]},  # after #5: the board lags
             {board.STAGING: [s8, s9]},      # after #8: #5 merged; #8 still lags
             {board.STAGING: [s8, s9]}]
    p = P.Promoter(g=g)
    assert p.fast_path({board.STAGING: [s5, s8, s9]}, reread=lambda: reads.pop(0)) == [5, 8, 9]
    assert [m[0] for m in g.moves] == ['I5', 'I8', 'I9'] and reads == []


def test_the_slow_path_sees_the_slots_freed_during_the_fast_path(tmp_path, monkeypatch, clean):
    """Codex on PR #68: Human Review is [#1]; #5 moves, and the re-read shows #1 and #5
    merged. once() must count 0 of 2 (not 1 + 1), so the slow path re-gates #7 now."""
    H7 = '7' * 40
    boards = [page(item(1, HR, 'a'), item(5, ST, H5), item(7, ST, H7))] * 2 + [page(item(7, ST, H7))]
    det = {'repository': {'p5': detail(5, H5), 'p7': detail(7, H7)}}
    g = G(tmp_path)
    g['gql'] = lambda q, **v: det if 'pullRequest(number:' in q else (boards.pop(0) if len(boards) > 1 else boards[0])
    p = P.Promoter(g=g)
    from tangentswarm import boardcache

    def move(i, c):  # tangentswarm #116: #1 and #5 merge right after the move; the events patch the cache
        g.moves.append((i, c))
        for n in (1, 5):
            boardcache.apply_event('pull_request', {'action': 'closed', 'repository': {'full_name': P.REPO},
                                                    'pull_request': {'number': n, 'merged': True}})
    g['move'] = move
    slow = []
    monkeypatch.setattr(p, 'main_sha', lambda: None)
    monkeypatch.setattr(p, 'queue_cascade', lambda why, now=None: None)
    monkeypatch.setattr(p, 'slow_path', lambda: slow.append([n for n, _, _ in p.regate()]))

    class L:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    assert p.once() == 'promoted'
    assert g.moves == [('I5', board.HUMAN_REVIEW)] and p.hr_count == 0 and slow == [[7]]


