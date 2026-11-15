"""swarm-advance: one step from the end of the line (promote, else pull, else MinBot pass)."""
import os

import pytest
from pathlib import Path

from tangentswarm import advance as A
from tangentswarm import board, flow, promoted

H = 'a' * 40


def receipt(head, login='chatgpt-codex-connector[bot]'):
    return {'author': {'login': login},
            'body': f"Codex Review: Didn't find any major issues. :+1:\n\n**Reviewed commit:** `{head[:10]}`"}


def codex_pr(head=H, comments=None, reviews=(), threads=(), draft=False, n=7):
    return {'number': n, 'isDraft': draft, 'headRefOid': head,
            'comments': {'nodes': list(comments if comments is not None else [receipt(head)])},
            'reviews': {'nodes': list(reviews)}, 'reviewThreads': {'nodes': list(threads)}}


def test_codex_clean_needs_a_receipt_for_the_exact_head():
    assert A.codex_clean(codex_pr())
    assert not A.codex_clean(codex_pr(comments=[receipt('b' * 40)]))
    assert not A.codex_clean(codex_pr(comments=[receipt(H, login='someone')]))
    state = {'author': {'login': 'chatgpt-codex-connector[bot]'},
             'body': '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"%s","has_any_findings":false,'
                     '"has_unresolved_findings":false}} -->' % H}
    assert A.codex_clean(codex_pr(comments=[state]))


def test_codex_clean_fails_on_findings_or_an_open_codex_thread():
    codex = {'login': 'chatgpt-codex-connector[bot]'}
    finding = {'author': codex, 'commit': {'oid': H}, 'comments': {'totalCount': 2}}
    assert not A.codex_clean(codex_pr(reviews=[finding]))
    thread = {'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'author': codex}]}}
    assert not A.codex_clean(codex_pr(threads=[thread]))
    assert A.codex_clean(codex_pr(threads=[dict(thread, isOutdated=True)]))


def test_minbot_order_puts_the_active_fast_track_first():
    prs = [dict(number=9), dict(number=4), dict(number=12, closes=[488])]
    assert [p['number'] for p in A.minbot_order(prs, 488)] == [12, 4, 9]
    assert [p['number'] for p in A.minbot_order(prs, None)] == [4, 9, 12]


class Gate(dict):
    def __init__(self, cols, details=None, rows=None):
        super().__init__()
        self.moves = []
        self.cols = cols
        self.update(gql=self.gql, move=lambda item, col: self.moves.append((item, col)),
                    fetch=lambda n, refresh=False: {'number': n},
                    list_hr_prs=lambda refresh=False: [],
                    evaluate=lambda pr, hr_peers=None, scratch=None: (rows or GOOD_ROWS, {}),
                    ScratchRepo=Scratch)
        self.details = details or {}

    def gql(self, q, **v):
        return {'repository': {f'p{n}': d for n, d in self.details.items()}}


class Scratch:
    main = 'M' * 40

    def __enter__(self):
        return self

    def ensure_main(self):
        pass

    def ensure(self, *revs):
        pass

    def __exit__(self, *a):
        return False


GOOD_ROWS = [('1 not draft', True, 'ready'), ('2 mergeable onto main', True, 'clean'),
             ('3 Codex clean on head', True, 'clean'), ('4 MinBot pass on head', False, 'never'),
             ('5 CI green (Linux)', True, 'green'), ('7a screenshots current', False, 'no image')]


@pytest.fixture
def board_read(monkeypatch):
    """Advance.step reads the columns once with promoted.read_columns."""
    def use(cols):
        monkeypatch.setattr(promoted, 'read_columns', lambda gql, ft_state=None: cols)
    monkeypatch.setattr(flow, 'load_pull', lambda: None)
    return use


def run(g, dry=False):
    out, calls = [], []
    adv = A.Advance(g=g, dry_run=dry, out=out.append, minbot=lambda *a: calls.append(a))
    assert adv.step() == 0
    return out, calls


def test_promote_goes_first_and_ends_the_step(board_read, monkeypatch):
    cols = {board.HUMAN_REVIEW: [], board.STAGING: [dict(number=5, head=H, item='I5')],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)

    def fast(self, c):
        promoted.say('moved #5 Staging -> Human Review (cheap check passed at aaaaaaaa)')
        return [5]
    monkeypatch.setattr(promoted.Promoter, 'fast_path', fast)
    out, calls = run(Gate(cols))
    assert out == ['moved #5 Staging -> Human Review (cheap check passed at aaaaaaaa)'] and calls == []


def test_pull_runs_when_no_promote_and_staging_is_short(board_read, monkeypatch):
    cols = {board.HUMAN_REVIEW: [1, 2], board.READY: [dict(number=6, head=H, item='I6', draft=False)]}
    board_read(cols)
    seen = []

    def step(self, promote=True):
        seen.append(promote)
        self.did.append('did: #6 pulled')
        return 0
    monkeypatch.setattr(flow.Flow, 'step', step)
    out, _ = run(Gate(cols))
    assert seen == [False] and out == ['pull: did: #6 pulled']


def test_minbot_pass_labels_comments_and_moves_to_ready(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g)
    assert len(calls) == 1 and not any(c[0] == 'label' for c in calls)  # the label is retired
    assert calls[0][:2] == ('evidence', 7) and H in calls[0] and A.pass_text(H) in calls[0]
    assert calls[0][calls[0].index('--title') + 1] == 'MinBot pass'  # what hr-gate condition 4 reads
    assert g.moves == [('I7', board.READY)]
    assert out == [f"MinBot-pass #7 at {H[:8]}: pass comment, Auto Review -> Ready"]


def test_minbot_pass_dry_run_moves_nothing(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g, dry=True)
    assert g.moves == [] and out[0].startswith('would MinBot-pass #7')
    assert len(calls) == 1  # minbot without --apply is its own dry run


def test_minbot_pass_refuses_a_failed_gate_row(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    rows = [r if not r[0].startswith('5 ') else ('5 CI green (Linux)', False, 'FAILURE') for r in GOOD_ROWS]
    g = Gate(cols, details={7: codex_pr()}, rows=rows)
    out, calls = run(g)
    assert calls == [] and g.moves == []
    assert out[0].startswith('nothing to do: Human Review 2/2') and '#7 5 CI green (Linux): FAILURE' in out[0]


def test_nothing_to_do_is_one_line(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7'), dict(number=8, head=H, item='I8')]}
    board_read(cols)
    cols[board.AUTO_REVIEW].append(dict(number=9, head=H, item='I9'))
    g = Gate(cols, details={7: codex_pr(comments=[]), 8: codex_pr(draft=True, n=8),
                            9: dict(codex_pr(n=9), mergeable='CONFLICTING')})
    g['fetch'] = lambda n, refresh=False: pytest.fail('a skipped PR must not get the full read')
    out, calls = run(g)
    assert len(out) == 1 and calls == []
    assert out[0] == (f"nothing to do: Human Review 2/2; Staging 3/{flow.STAGING_MIN}, Ready 0; "
                      f"Auto Review: #7 not Codex-clean on {H[:8]}, #8 draft, #9 conflicts with main (GitHub mergeable)")


# --- --cascade: the drain loop -------------------------------------------------------

def test_cascade_reruns_until_no_flag_is_left(tmp_path):
    flag, lock = tmp_path / 'rerun', tmp_path / 'lock'
    runs, out = [], []

    def body():  # two events arrive during the first pass, one during the second
        runs.append(1)
        if len(runs) < 3:
            A.flag_rerun(flag)
    assert A.run_locked(body, flag=flag, lock=lock, out=out.append, env={}) == 0
    assert len(runs) == 3 and not flag.exists()
    assert out == ['rerun flag found: cascade pass 2', 'rerun flag found: cascade pass 3']


def test_a_busy_caller_flags_queues_and_exits(tmp_path):
    import fcntl
    flag, lock = tmp_path / 'rerun', tmp_path / 'lock'
    out, queued = [], []
    with open(lock, 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        rc = A.run_locked(lambda: pytest.fail('busy: must not run'), flag=flag, lock=lock, out=out.append,
                          env={}, enqueue=lambda: queued.append(1) or 'queued for swarm-promoted')
    assert rc == 0 and flag.exists() and queued == [1]
    assert out[0].startswith('busy: ') and 'rerun flag set' in out[0]
    # the holder (here: the next run) drains the flag
    runs = []
    A.run_locked(lambda: runs.append(1), flag=flag, lock=lock, out=out.append, env={}, if_flagged=True)
    assert runs == [1] and not flag.exists()


def test_if_flagged_without_a_flag_does_nothing(tmp_path):
    out = []
    A.run_locked(lambda: pytest.fail('no flag: must not run'), flag=tmp_path / 'rerun', lock=tmp_path / 'lock',
                 out=out.append, env={}, if_flagged=True)
    assert out == ['no rerun flag: nothing to do']


def test_the_drain_loop_stops_after_max_passes(tmp_path):
    flag, runs, out = tmp_path / 'rerun', [], []
    A.run_locked(lambda: runs.append(1) or A.flag_rerun(flag), flag=flag, lock=tmp_path / 'lock',
                 out=out.append, env={}, max_passes=4)
    assert len(runs) == 4 and flag.exists() and out[-1].startswith('stopped after 4 cascade passes')


def test_a_flag_after_the_release_takes_the_lock_again(tmp_path, monkeypatch):
    """The race: a caller writes the flag, finds the lock busy and exits, and the holder had
    already looked. The holder looks again after the release."""
    flag, runs = tmp_path / 'rerun', []
    real = A.gatelock.gate_lock
    import contextlib

    @contextlib.contextmanager
    def lock_then_late_flag(*a, **kw):
        with real(*a, **kw):
            yield
            if len(runs) == 1:
                A.flag_rerun(flag)  # written after the inner look, before the release
    monkeypatch.setattr(A.gatelock, 'gate_lock', lock_then_late_flag)
    A.run_locked(lambda: runs.append(1), flag=flag, lock=tmp_path / 'lock', out=lambda s: None, env={})
    assert len(runs) == 2 and not flag.exists()


# --- --cascade: Staging and Ready limits ---------------------------------------------

def pr(n, col, **kw):
    return dict(dict(number=n, head=f'{n:040d}', item=f'I{n}', branch=f'b{n}', draft=False, rank=9,
                     col=col, state='OPEN', parked=False), **kw)


SITES3 = ('work', 'minavo', 'ptw')


class CascadeGate(dict):
    """served: {site key: static sha}; a sha in `has` contains the head."""
    def __init__(self, served_files=True, served=None, has=(), issues=()):
        super().__init__()
        from test_slots import board_items
        self['gql'] = lambda q, **v: board_items(issues)
        files = {'nodes': [{'path': 'web/a.ts' if served_files else 'README.md'}]}
        self.served = dict(served or {k: 'o' * 40 for k in SITES3})
        self.update(fetch=lambda n, refresh=False: {'number': n, 'files': files, 'headRefName': f'b{n}'},
                    ScratchRepo=Scratch, contains=lambda anc, desc: desc in has,
                    served_all=lambda: [(k, f'{k}.example', sha, None) for k, sha in self.served.items()])


def merge_fakes(monkeypatch, dirty=(), main_dirty=()):
    """Flow merge-tree checks without git: a Ready head n conflicts with origin/main when n is in
    main_dirty, and with every other base when n is in dirty."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F.Flow, 'sha_of', staticmethod(lambda scratch, ref: ref))
    monkeypatch.setattr(F.Flow, 'fetch_work', lambda self, scratch: 'W' * 40)

    def clean(self, scratch, a, b):
        n = int(b)
        return n not in main_dirty if a == Scratch.main else n not in dirty
    monkeypatch.setattr(F.Flow, 'clean', clean)
    monkeypatch.setattr(F.Flow, 'files', lambda self, scratch, a, b: [f'f{int(b)}.ts'])


def cascade(monkeypatch, tmp_path, cols, g, dry=True, **kw):
    """kw dirty: Ready PRs that Flow.pull strict skips (they do not merge cleanly); main_dirty:
    Ready PRs that conflict with origin/main (the main check sends them to rework)."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch, kw.get('dirty', ()), kw.get('main_dirty', ()))
    pulled = []

    def pull_batch(self, prs, c, scratch, room):
        fits = [q for q in c.get(board.READY, []) if q['number'] not in kw.get('dirty', ())][:room]
        pulled.extend(q['number'] for q in fits)
        if fits:
            self.change(f"{', '.join('#' + str(q['number']) for q in fits)} pulled")
        return fits
    monkeypatch.setattr(F.Flow, 'pull_batch', pull_batch)
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {k: list(v) for k, v in cols.items()}))
    out = []
    c = A.Cascade(g=g, dry_run=dry, out=out.append, sleep=lambda s: None)
    c.pulled = []
    cols_after = c.fill()
    c.unblock(cols_after, c.pulled)
    c.signal(cols_after)
    return out, pulled


def test_empty_staging_gets_one_pull_then_only_clean_extras(monkeypatch, tmp_path):
    cols = {board.READY: [pr(n, board.READY) for n in (4, 5, 6, 7)],
            board.IN_PROGRESS: [pr(8, board.IN_PROGRESS)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(), dirty={5, 7})
    assert pulled == [4, 6]  # #5 and #7 do not merge cleanly: skipped, not reworked
    assert 'fill: #4, #6 pulled in one work merge: Staging 0 + 2, target 2' in out  # one batch, one push
    assert out.count('would run hr-gate --apply 4 (Ready -> Staging when it passes)') == 1
    assert out.count('would run hr-gate --apply 6 (Ready -> Staging when it passes)') == 1
    assert out[-1] == 'signal: no Active issue without a PR (new=0/4 rework=0/3 ready=2/6 active_without_pr=0): no START_CLAUDE'


def test_staging_empty_and_nothing_clean_pulls_nothing(monkeypatch, tmp_path):
    cols = {board.READY: [pr(n, board.READY) for n in range(6, 12)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(), dirty=set(range(6, 12)))
    assert pulled == [] and out[:3] == [
        'fill: Staging 0: no Ready PR to pull without rework',
        'STALLED: Human Review 0, Staging 0, no Ready PR can be pulled; best candidate #6 (1 conflict(s))',
        'NEEDS_REWORK #6 f6.ts']
    assert out[3].startswith('would move #6 Ready -> In Progress and send: Coordinator: #6 cannot enter Staging: '
                             'it conflicts with work: f6.ts. Merge origin/main into your branch')


def test_staging_at_the_floor_pulls_nothing(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)],
            board.READY: [pr(n, board.READY) for n in range(10, 16)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert pulled == [] and out == [f'fill: Staging {A.FLOOR}, target {A.FLOOR}: no pull needed',
                                    'signal: no Active issue without a PR (new=0/3 rework=0/3 ready=6/6 active_without_pr=0): no START_CLAUDE']


@pytest.mark.parametrize('staging, want', [(0, [10, 12, 14, 15, 16]), (1, [10, 12, 14, 15, 16]), (2, [])])
def test_below_the_target_one_batch_takes_every_clean_ready_pr(monkeypatch, tmp_path, staging, want):
    """Michal 2026-11-15: one work merge for all clean Ready PRs, so one redeploy covers them.
    The target (FLOOR) says WHEN to pull; the batch is capped by flow.STAGING_MAX."""
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, staging + 1)],
            board.READY: [pr(n, board.READY) for n in range(10, 17)]}
    _, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False), dirty={11, 13})
    assert pulled == want[:flow.STAGING_MAX - staging]


def test_pulls_in_flight_count_toward_the_target(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.STAGING: [pr(1, board.STAGING)], board.READY: [pr(3, board.READY), pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s: self.note(f"#{d['pr']} pull waiting (1 min): CI"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [] and 'fill: Staging 1 + 1 in flight (#3), target 2: no pull needed' in out


def test_a_fast_track_pr_is_pulled_past_the_floor(monkeypatch, tmp_path):
    """(d) Michal 8:43 PM ET: the PR marked urgent is the next Staging pull, also at the floor."""
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)],
            board.READY: [pr(10, board.READY), pr(12, board.READY, rank=board.FAST_TRACK), pr(13, board.READY)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [12]  # only the fast-track PR; #10 and #13 wait for the floor
    assert f'fill: Staging {A.FLOOR}, target {A.FLOOR}: fast-track #12 is pulled past the target' in out
    assert f'fill: #12 pulled in one work merge (fast-track): Staging {A.FLOOR} + 1, target {A.FLOOR}' in out


def test_a_fast_track_pr_that_does_not_merge_cleanly_is_not_pulled_past_the_floor(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)],
            board.READY: [pr(10, board.READY), pr(12, board.READY, rank=board.FAST_TRACK)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False), dirty={12})
    assert pulled == [] and f'fill: Staging {A.FLOOR}: no extra Ready PR to pull without rework' in out


def test_the_pull_order_puts_the_fast_track_pr_first():
    ready = [pr(5, board.READY, rank=board.NORMAL), pr(9, board.READY, rank=board.FAST_TRACK),
             pr(7, board.READY, rank=board.MILESTONE)]
    assert [p['number'] for p in flow.ready_order(ready, {9: 3})] == [9, 7, 5]


def test_a_ready_pr_that_conflicts_with_main_goes_to_rework_with_its_files(monkeypatch, tmp_path):
    """(1) Michal 9:10 PM ET: #481 and #484 conflicted with origin/main and the cascade ended quietly."""
    cols = {board.READY: [pr(481, board.READY), pr(484, board.READY), pr(486, board.READY)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False), main_dirty={481, 484})
    assert pulled == [486]
    assert 'NEEDS_REWORK #481 f481.ts' in out and 'NEEDS_REWORK #484 f484.ts' in out
    assert any(l.startswith('would move #481 Ready -> In Progress and send: Coordinator: #481 cannot enter '
                            'Staging: it conflicts with origin/main: f481.ts. Merge origin/main') for l in out)
    assert 'main check: #481, #484 conflict with origin/main: sent to rework' in out


def test_a_live_rework_moves_the_card_revives_the_claude_and_sends_one_relay_per_head(monkeypatch, tmp_path):
    from tangentswarm import flow as F, slots
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setattr(F, 'log', lambda line: None)
    merge_fakes(monkeypatch, main_dirty={481})
    moved, revived = [], []
    g = CascadeGate()
    g['move'] = lambda item, col: moved.append((item, col))
    monkeypatch.setattr(slots, 'revive', lambda n, msg, out=print: revived.append((n, msg)) or 3)  # slots full
    monkeypatch.setattr(A, 'Path', lambda p: tmp_path / Path(p).name)
    out = []
    c = A.Cascade(g=g, dry_run=False, out=out.append, sleep=lambda s: None)
    cols = {board.READY: [pr(481, board.READY)]}
    assert c.main_check(cols)[board.READY] == []
    assert moved == [('I481', board.IN_PROGRESS)]
    assert revived[0][0] == 481 and 'conflicts with origin/main: f481.ts' in revived[0][1]
    assert 'do not rebase' in revived[0][1].lower() and '\n' not in revived[0][1]
    assert f'RELAY: swarm-resume 481 {tmp_path}/relay-481-merge-main.md' in out
    assert (tmp_path / 'relay-481-merge-main.md').read_text() == revived[0][1] + '\n'
    c.main_check(cols)  # same head: no second relay, no second move
    assert len(revived) == 1 and len(moved) == 1
    assert 'rework: #481 already has a relay for 00000000: not sent again' in out


def test_empty_ready_stops_the_fill_and_starts_coders_on_active_issues(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(1, board.STAGING)]}
    g = CascadeGate(issues=[(n, board.ACTIVE) for n in (40, 41, 42, 43, 44)])
    out, pulled = cascade(monkeypatch, tmp_path, cols, g)
    assert pulled == [] and out == ['fill: Staging 1, Ready 0: Ready is empty',
                                    'STALLED: Human Review 0, Staging 1, Ready empty: new work must reach Ready',
                                    'START_CLAUDE n=4 issues=#40,#41,#42,#43 new=0/4 rework=0/3 ready=0/6 '
                                    'active_without_pr=5']


def test_start_claude_also_when_ready_is_full(monkeypatch, tmp_path):
    """(c) Not only when Ready is short: Ready is the buffer, new work never stops."""
    cols = {board.READY: [pr(n, board.READY) for n in range(10, 17)]}
    monkeypatch.setattr(A, 'FLOOR', 0)
    out, _ = cascade(monkeypatch, tmp_path, cols, CascadeGate(issues=[(50, board.ACTIVE), (51, board.ACTIVE)]))
    assert out[-1] == 'START_CLAUDE n=2 issues=#50,#51 new=0/3 rework=0/3 ready=7/6 active_without_pr=2'


def test_a_dry_run_shows_the_cornerbot_request_and_the_wait_for_every_site(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(1, board.STAGING)], board.READY: [pr(9, board.READY)]}
    g = CascadeGate(served={'work': 'h' * 40, 'minavo': 'h' * 40, 'ptw': 'o' * 40}, has={'h' * 40})
    out, pulled = cascade(monkeypatch, tmp_path, cols, g)
    assert pulled == [9]
    assert any(l.startswith('CORNERBOT_REQUEST (dry run): redeploy ptw.example') and '#9 b9' in l for l in out)
    assert ('would gate each one when every staging site serves the batch (no wait; the daemon queues a cascade '
            'on a .deploy-sha change) (now lagging: ptw.example)') in out
    assert 'would run hr-gate --apply 9 (Ready -> Staging when it passes)' in out


def live(monkeypatch, tmp_path, g, advance=None, ready=(9,), staging=(1,), hr=()):
    """A live fill: Flow.pull_batch merges the clean Ready PRs into work ('w'*40) in one push.
    Returns (out, gated PRs, seconds slept, board)."""
    from tangentswarm import flow as F
    gated = []

    def batch(self, prs, c, scratch, room):
        got = c[board.READY][:room]
        F.save_pulls(F.load_pulls() + [dict(pr=p['number'], head=p['head'], branch=f"b{p['number']}", item='I',
                                            work='w' * 40, started=0) for p in got])
        return got

    def adv(self, d, prs, scratch):
        gated.append(d['pr'])
        F.drop_pull(d['pr'])  # hr-gate --apply moved it
        c[board.READY] = [q for q in c[board.READY] if q['number'] != d['pr']]
        c[board.STAGING].append(pr(d['pr'], board.STAGING))
        self.change(f"#{d['pr']} hr-gate --apply: moved")
    c = {board.STAGING: [pr(n, board.STAGING) for n in staging], board.READY: [pr(n, board.READY) for n in ready],
         board.HUMAN_REVIEW: [pr(n, board.HUMAN_REVIEW) for n in hr]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch)
    monkeypatch.setattr(A, 'CORNERBOT_FILE', tmp_path / 'cornerbot')
    monkeypatch.setattr(F.Flow, 'pull_batch', batch)
    monkeypatch.setattr(F.Flow, 'advance', advance or adv)
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, c))
    out, slept = [], []
    A.Cascade(g=g, dry_run=False, out=out.append, sleep=slept.append).fill()
    return out, gated, sum(slept), c


W, O = 'w' * 40, 'o' * 40
PTW_LAGS = {'work': W, 'minavo': W, 'ptw': O}
ALL_SERVE = {'work': W, 'minavo': W, 'ptw': W}


def test_a_batch_waits_on_no_redeploy_and_asks_cornerbot_once(monkeypatch, tmp_path):
    """Michal 2026-11-15: Human Review sat empty about 15 min twice while single pulls of #495 and
    #514 each waited about 10 min on a PTW redeploy, holding the serial lock."""
    from tangentswarm import flow as F
    out, gated, slept, _ = live(monkeypatch, tmp_path, CascadeGate(served=PTW_LAGS), ready=(9, 10), staging=())
    assert slept == 0 and gated == []  # no sleep, no gate: the lock is free for a promote at once
    assert 'fill: #9, #10 pulled in one work merge: Staging 0 + 2, target 2' in out
    reqs = [l for l in out if l.startswith('CORNERBOT_REQUEST')]
    assert reqs == ['CORNERBOT_REQUEST: redeploy ptw.example from platform work wwwwwwww (#9 b9 00000000 is in work) '
                    'with #10']  # ONE request for the whole batch
    assert [d['asked'] for d in F.load_pulls()] == [True, True]
    assert any(l.startswith('waiting: #10 00000000 is not served yet by ptw.example (oooooooo); pulled ') for l in out)


def test_the_next_cascade_gates_the_whole_batch_once_ptw_serves_it(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pulls([dict(pr=n, head=f'{n}' * 40, branch='b', item='I', work=W, started=0, asked=True) for n in (9, 10)])
    (tmp_path / 'cornerbot').write_text('x CORNERBOT_REQUEST: redeploy ptw.example ...\n')
    g = CascadeGate(served=ALL_SERVE, has={W})
    out, gated, slept, c = live(monkeypatch, tmp_path, g, ready=(9, 10), staging=())
    assert gated == [9, 10] and slept == 0 and F.load_pulls() == []
    assert 'CORNERBOT_DONE: every cornerbot site serves #9 99999999; request cleared' in out
    assert not (tmp_path / 'cornerbot').exists()
    assert 'fill: Staging 2, target 2: no pull needed' in out


def test_a_promote_starts_the_next_pull_while_a_batch_still_waits(monkeypatch, tmp_path):
    """(2) A promote frees a Staging slot: the next Ready pull starts in the same pass, and the
    waiting batch (asked before) gets one check and no second request."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=9, head='9' * 40, branch='b', item='I', work=W, started=0, asked=True))
    out, gated, slept, _ = live(monkeypatch, tmp_path, CascadeGate(served=PTW_LAGS), ready=(9, 11), staging=())
    assert slept == 0 and gated == []
    assert 'fill: #11 pulled in one work merge: Staging 0 + 1 in flight (#9) + 1, target 2' in out
    assert [d['pr'] for d in F.load_pulls()] == [9, 11]
    assert not any(l.startswith('CORNERBOT_REQUEST') and '#9' in l for l in out)  # asked before


def test_a_pull_without_served_files_is_gated_at_once(monkeypatch, tmp_path):
    out, gated, _, _ = live(monkeypatch, tmp_path, CascadeGate(served_files=False, served=PTW_LAGS), ready=(9,),
                            staging=())
    assert gated == [9] and not any(l.startswith(('CORNERBOT', 'waiting')) for l in out)


def test_a_web_site_that_never_serves_never_holds_the_lock(monkeypatch, tmp_path):
    g = CascadeGate(served={'work': O, 'minavo': W, 'ptw': W})
    out, gated, slept, _ = live(monkeypatch, tmp_path, g, advance=lambda *a: pytest.fail('no gate'))
    assert gated == [] and slept == 0
    assert not any(l.startswith('CORNERBOT_REQUEST') for l in out)


def test_a_dry_run_goes_on_after_a_running_pull_would_end(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.READY: [pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s: self.change(f"#{d['pr']} pull finished"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [9] and 'pull: would: #3 pull finished' in out
    assert F.load_pull()['pr'] == 3  # a dry run changes nothing


def test_a_dry_run_stops_at_a_waiting_pull(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.READY: [pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s: self.note(f"#{d['pr']} pull waiting (1 min): CI"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    # a waiting pull no longer blocks the fill: Staging 0 + 1 in flight is below the target
    assert pulled == [9] and 'note: #3 pull waiting (1 min): CI' in out


def test_a_dry_run_counts_a_gated_running_pull_in_staging(monkeypatch, tmp_path):
    """Live dry run 2026-11-15: #476 (the running pull) would pass hr-gate --apply, and the
    fill then pulled #476 again because it was still in the Ready list."""
    from tangentswarm import flow as F
    cols = {board.READY: [pr(3, board.READY), pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s: self.change(f"#{d['pr']} hr-gate --apply {d['pr']}"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [9] and 'fill: #9 pulled in one work merge: Staging 1 + 1, target 2' in out


# --- --cascade: run budget, timeouts, clean stop (7:02 PM ET tick ran past 600 s, no output) ---

def test_a_timeout_keeps_the_rerun_flag_and_releases_the_lock(tmp_path):
    flag, lock, out = tmp_path / 'rerun', tmp_path / 'lock', []

    def body():
        raise A.CascadeTimeout('run budget used up')
    rc = A.run_locked(body, flag=flag, lock=lock, out=out.append, env={})
    assert rc == A.EXIT_TIMEOUT and flag.exists()
    assert out[-1] == 'stopped: CascadeTimeout: run budget used up; lock released, rerun flag kept'
    assert not A.gatelock.is_busy(lock)


def test_an_error_keeps_the_rerun_flag_releases_the_lock_and_raises(tmp_path):
    flag, lock, out = tmp_path / 'rerun', tmp_path / 'lock', []

    def body():
        raise RuntimeError('gh api graphql: rate limit')
    with pytest.raises(RuntimeError):
        A.run_locked(body, flag=flag, lock=lock, out=out.append, env={})
    assert flag.exists() and not A.gatelock.is_busy(lock) and 'rerun flag kept' in out[-1]


def test_the_watchdog_turns_sigterm_and_the_alarm_into_a_timeout(tmp_path):
    import os
    import signal
    old = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
    try:
        A.watchdog(budget=0)
        with pytest.raises(A.CascadeTimeout, match='signal'):
            os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(A.CascadeTimeout, match='budget'):
            signal.raise_signal(signal.SIGALRM)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old[0])
        signal.signal(signal.SIGTERM, old[1])


def test_progress_lines_carry_a_time_and_go_to_the_log(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(A, 'CASCADE_LOG', tmp_path / 'cascade.log')
    A._say('gate: hr-gate on #9')
    printed = capsys.readouterr().out
    import re
    assert re.fullmatch(r'\d\d:\d\d:\d\d gate: hr-gate on #9\n', printed)
    assert (tmp_path / 'cascade.log').read_text().endswith(printed)


def test_a_cornerbot_site_asked_before_gets_one_check_and_no_wait(monkeypatch, tmp_path):
    """7:32 PM ET: pass after pass waited 430 s for the same ptw redeploy of #480, holding the lock."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=480, head='5f6e09a4' + '0' * 32, branch='b', item='I', work='w' * 40, started=0, asked=True))
    monkeypatch.setattr(F.Flow, 'advance', lambda *a: pytest.fail('not served: no gate'))
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {}))
    out, now = [], [0]
    g = CascadeGate(served={'work': 'w' * 40, 'minavo': 'w' * 40, 'ptw': 'o' * 40})
    A.Cascade(g=g, out=out.append, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0]).fill()
    assert now[0] == 0 and not any(l.startswith('CORNERBOT_REQUEST') for l in out)
    assert out[0].startswith('waiting: #480 5f6e09a4 is not served yet by ptw.example (oooooooo); pulled ')


def test_the_deadline_is_per_process_not_per_pass(monkeypatch):
    import signal
    import time
    old = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
    try:
        A.watchdog(budget=500)
        first = A.Cascade(g=CascadeGate()).deadline
        assert abs(first - (time.monotonic() + 500)) < 5
        assert A.Cascade(g=CascadeGate()).deadline == first  # a rerun pass keeps the same deadline
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old[0])
        signal.signal(signal.SIGTERM, old[1])
        monkeypatch.setattr(A, 'DEADLINE', None)


# -- CORNERBOT_REQUEST on every run until the site serves the head (Michal 2026-11-15) -------

def corner_setup(tmp_path, pr=495, asked_age=3000, served=None, pull=True):
    from tangentswarm import flow as F
    W, O = 'w' * 40, 'o' * 40
    if pull:
        F.save_pull(dict(pr=pr, head='4' * 40, branch='avo', item='I', work=W, started=0, asked=True))
    A.CORNERBOT_FILE.write_text("2026-11-15T03:20:18 CORNERBOT_REQUEST: redeploy ptw.example from platform "
                                "work wwwwwwww (#495 avo 44444444 is in work)\n")
    at = 1_000_000.0
    os.utime(A.CORNERBOT_FILE, (at, at))
    g = CascadeGate(served=served or {'work': W, 'minavo': W, 'ptw': O})
    return g, lambda: at + asked_age


def test_the_cornerbot_request_is_printed_again_with_its_age_while_ptw_lags(tmp_path):
    g, now = corner_setup(tmp_path)
    out = []
    line = A.cornerbot_reminder(g, out=out.append, now=now)
    assert out == [line]
    assert line.startswith('CORNERBOT_REQUEST: redeploy ptw.example from platform work wwwwwwww (#495 avo 44444444 is in work)')
    assert '; open 50m00s (asked ' in line and line.endswith('; not served yet')
    assert A.CORNERBOT_FILE.exists()  # the age stays the age of the first request


def test_the_cornerbot_request_is_cleared_once_ptw_serves_the_head(tmp_path):
    W = 'w' * 40
    g, now = corner_setup(tmp_path, asked_age=4000, served={'work': W, 'minavo': W, 'ptw': W})
    out = []
    A.cornerbot_reminder(g, out=out.append, now=now)
    assert out == ['CORNERBOT_DONE: every cornerbot site serves #495 44444444 1h06m after the request; request cleared']
    assert not A.CORNERBOT_FILE.exists()


def test_a_cornerbot_request_without_its_pull_is_cleared(tmp_path):
    for kw in ({'pull': False}, {'pr': 496}):
        g, now = corner_setup(tmp_path, **kw)
        out = []
        A.cornerbot_reminder(g, out=out.append, now=now)
        assert out[0].startswith('CORNERBOT_REQUEST cleared: no running pull waits on it (')
        assert not A.CORNERBOT_FILE.exists()


def test_a_dry_run_prints_the_cornerbot_reminder_and_keeps_the_file(tmp_path):
    W = 'w' * 40
    g, now = corner_setup(tmp_path, served={'work': W, 'minavo': W, 'ptw': W})
    out = []
    A.cornerbot_reminder(g, out=out.append, dry=True, now=now)
    assert out[0].endswith('request cleared (dry run: file kept)') and A.CORNERBOT_FILE.exists()


def test_a_failed_served_check_still_prints_the_request(tmp_path):
    g, now = corner_setup(tmp_path)
    g['served_all'] = lambda: (_ for _ in ()).throw(OSError('no route'))
    out = []
    A.cornerbot_reminder(g, out=out.append, now=now)
    assert out[0].startswith('CORNERBOT_REQUEST: redeploy ptw.example ') and 'served check failed (OSError: no route)' in out[0]
    assert A.CORNERBOT_FILE.exists()


def test_no_cornerbot_file_prints_nothing(tmp_path):
    out = []
    assert A.cornerbot_reminder(CascadeGate(), out=out.append) is None and out == []


@pytest.mark.parametrize('case', ['busy', 'no-flag', 'dry'])
def test_every_cascade_run_prints_the_cornerbot_request(monkeypatch, tmp_path, case):
    """10:20-11:11 PM ET: the line for #495 was printed once, then busy runs and the drained
    `--if-flagged` runs said nothing."""
    g, _ = corner_setup(tmp_path)
    out = []
    monkeypatch.setattr(A, '_say', out.append)
    monkeypatch.setattr(A, 'watchdog', lambda budget=None: None)
    monkeypatch.setattr(A, 'hr_gate', lambda: g)

    class Busy:
        def __enter__(self):
            raise A.gatelock.Busy('held by pid 1')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(A.gatelock, 'gate_lock', lambda *a, **k: Busy())
    monkeypatch.setattr(A.Cascade, 'run', lambda self: 0)
    monkeypatch.setattr(A.Cascade, '__init__', lambda self, *a, **k: None)
    argv = {'busy': ['--cascade'], 'no-flag': ['--cascade', '--if-flagged'], 'dry': ['--cascade', '--dry-run']}[case]
    assert A.main(argv) == 0
    assert out[0].startswith('CORNERBOT_REQUEST: redeploy ptw.example ') and 'not served yet' in out[0]
    if case == 'dry':
        assert len(out) == 1
    else:
        assert out[1].startswith({'busy': 'busy: ', 'no-flag': 'no rerun flag'}[case])
