"""swarm-advance: one step from the end of the line (promote, else pull, else MinBot pass)."""
import json
import os

import pytest
from pathlib import Path

from tangentswarm import advance as A
from tangentswarm import board, flow, promoted

H = 'a' * 40


@pytest.fixture(autouse=True)
def worker_runs(monkeypatch):
    """Flow.fail and the screenshot job need a worker: by default its Claude takes the relay
    (never a real tmux or systemd-run in a test)."""
    from tangentswarm import slots
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: out(f'sent to #{n}') or 0)


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
                     '"has_unresolved_findings":false,"status":"completed"}} -->' % H}
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


@pytest.fixture(autouse=True)
def option_id_moves(monkeypatch):
    """A move hr-gate's move() does not know (Ready) goes by option id through tick.sh: record
    it on the last Gate, like hr-gate's move(), and never run gh."""
    from tangentswarm import tick
    by_id = {v: k for k, v in board.OPTION.items()}

    def sh(argv, check=True, **kw):
        assert argv[:3] == ['gh', 'project', 'item-edit'], argv
        Gate.last.moves.append((argv[argv.index('--id') + 1], by_id[argv[-1]]))
    monkeypatch.setattr(tick, 'sh', sh)


class Gate(dict):
    last = None

    def __init__(self, cols, details=None, rows=None):
        super().__init__()
        Gate.last = self
        self.moves = []
        self.cols = cols
        self.update(gql=self.gql, move=lambda item, col: self.moves.append((item, col)),
                    OPT={c: board.OPTION[c] for c in board.HR_GATE_COLS},
                    project_id=lambda: 'P', STATUS_FIELD='F',
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


def test_a_retried_minbot_pass_moves_the_card_without_a_second_comment(board_read):
    """platform #537 (2026-11-15 11:03 AM ET): the pass comment was posted, then the move to
    Ready failed. The retry moves the card and posts no second comment."""
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    d = codex_pr()
    d['comments']['nodes'].append({'author': {'login': 'tangentcode-minbot'}, 'body': '### MinBot pass\n' + A.pass_text(H)})
    g = Gate(cols, details={7: d})
    out, calls = run(g)
    assert calls == [] and g.moves == [('I7', board.READY)]


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


def test_a_failed_job_on_the_head_blocks_the_minbot_pass_and_says_why(board_read, monkeypatch):
    """tangentswarm #30, platform #516: "Linux mna tests" failed at 10:17 PM ET, and a MinBot
    pass at 10:20 PM ET still claimed "CI green (5 checks)"."""
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    runs = [{'name': 'Linux mna tests', 'status': 'completed', 'conclusion': 'failure'},
            {'name': 'Linux rs tests', 'status': 'completed', 'conclusion': 'success'},
            {'name': 'docs', 'status': 'completed', 'conclusion': 'skipped'}]
    monkeypatch.setattr(A, 'check_runs', lambda head, repo=None: runs)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g)
    assert g.moves == [] and len(calls) == 1
    assert calls[0][calls[0].index('--title') + 1] == 'MinBot check'  # not "MinBot pass"
    body = calls[0][calls[0].index('--body') + 1]
    assert body.startswith('CI is not green: "Linux mna tests" failure on this head.') and 'stays out of Ready' in body
    assert '#7 CI is not green' in out[0]
    _, again = run(g)
    assert again == []  # the check comment is posted once per head


def test_ci_problem_reads_every_job():
    ok = {'status': 'completed', 'conclusion': 'success', 'name': 'a'}
    assert A.ci_problem([ok, dict(ok, conclusion='skipped'), dict(ok, conclusion='neutral')]) is None
    assert A.ci_problem([ok, dict(ok, conclusion='cancelled', name='b')]) == 'CI is not green: "b" cancelled on this head'
    assert A.ci_problem([ok, {'status': 'in_progress', 'name': 'c'}]) == 'CI is still running: "c"'


def test_nothing_to_do_is_one_line(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7'), dict(number=8, head=H, item='I8')]}
    board_read(cols)
    cols[board.AUTO_REVIEW].append(dict(number=9, head=H, item='I9'))
    g = Gate(cols, details={7: codex_pr(comments=[]), 8: codex_pr(draft=True, n=8),
                            9: dict(codex_pr(n=9), body='## Questions for Michal\n1. which one?')})
    g['fetch'] = lambda n, refresh=False: pytest.fail('a skipped PR must not get the full read')
    out, calls = run(g)
    assert len(out) == 1 and calls == []
    assert out[0] == (f"nothing to do: Human Review 2/2; Staging 3/{flow.STAGING_MIN}, Ready 0; "
                      f"Auto Review: #7 not Codex-clean on {H[:8]}, #8 draft, "
                      "#9 the PR body has an open question for Michal")


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
    Ready PRs that conflict with origin/main (the main check sends them to rework).
    tangentswarm #144: a test that names no Human Review gets a full one (#901, #902), so it
    tests the Staging target; an empty Human Review pulls for it too (fill.plan)."""
    from tangentswarm import flow as F
    if board.HUMAN_REVIEW not in cols:
        cols = dict(cols, **{board.HUMAN_REVIEW: [pr(n, board.HUMAN_REVIEW) for n in (901, 902)]})
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch, kw.get('dirty', ()), kw.get('main_dirty', ()))
    pulled = []

    def pull_batch(self, prs, c, scratch, room):  # strict order (#65): the first dirty PR stops it
        fits = []
        for q in c.get(board.READY, []):
            if q['number'] in kw.get('dirty', ()) or len(fits) >= room:
                break
            fits.append(q)
        pulled.extend(q['number'] for q in fits)
        if fits:
            self.change(f"{', '.join('#' + str(q['number']) for q in fits)} pulled")
        return fits
    monkeypatch.setattr(F.Flow, 'pull_batch', pull_batch)
    monkeypatch.setattr(F.Flow, 'premerge', lambda self, cols, scratch: {})  # tested in test_flow
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {k: list(v) for k, v in cols.items()}))
    out = []
    c = A.Cascade(g=g, dry_run=dry, out=out.append, sleep=lambda s: None)
    c.pulled = []
    cols_after = c.fill()
    c.unblock(cols_after, c.pulled)
    c.signal(cols_after)
    return out, pulled


def test_strict_order_a_pr_that_is_not_merge_ready_stops_the_pull(monkeypatch, tmp_path):
    """tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): no queue-jumping. #5 is not merge-ready,
    so #6 behind it is not pulled even though it merges cleanly."""
    cols = {board.READY: [pr(n, board.READY) for n in (4, 5, 6, 7)],
            board.IN_PROGRESS: [pr(8, board.IN_PROGRESS)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(), dirty={5, 7})
    assert pulled == [4]
    assert 'fill: #4 pulled in one work merge: Staging 0 + 1, target 2' in out
    assert out.count('would run hr-gate --apply 4 (Ready -> Staging when it passes)') == 1
    assert not any('hr-gate --apply 6' in l for l in out)


def test_staging_empty_and_nothing_clean_pulls_nothing(monkeypatch, tmp_path):
    cols = {board.READY: [pr(n, board.READY) for n in range(6, 12)], board.HUMAN_REVIEW: []}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(), dirty=set(range(6, 12)))
    assert pulled == [] and out[:2] == [
        'fill: Staging 0: the next Ready PR is not merge-ready; nothing is pulled past it (strict order)',
        'STALLED: Human Review 0, Staging 0, no Ready PR was pulled; next in order #6: not pre-merged yet']
    assert not any('NEEDS_REWORK' in l or 'In Progress' in l for l in out)  # never a rework on its own branch


def test_staging_at_the_floor_pulls_nothing(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)],
            board.READY: [pr(n, board.READY) for n in range(10, 16)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert pulled == [] and out == [f'fill: Staging {A.FLOOR}, target {A.FLOOR}: no pull needed',
                                    'signal: no Backlog issue without a PR (new=0/3 rework=0/3 ready=6/6 backlog_without_pr=0 claudes=0/10 launching=0 unstarted=0): no START_CLAUDE']


@pytest.mark.parametrize('staging, want', [(0, [10, 11]), (1, [10]), (2, [])])
def test_the_staging_wip_target_caps_the_batch(monkeypatch, tmp_path, staging, want):
    """tangentswarm #65: fixed WIP. One work merge for the batch, never past the target."""
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, staging + 1)],
            board.READY: [pr(n, board.READY) for n in range(10, 17)]}
    _, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert A.FLOOR == 2 and pulled == want


def test_pulls_in_flight_count_toward_the_target(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.STAGING: [pr(1, board.STAGING)], board.READY: [pr(3, board.READY), pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s, served=False: self.note(f"#{d['pr']} pull waiting (1 min): CI"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [] and 'fill: Staging 1 + 1 in flight (#3), target 2: no pull needed' in out


def test_a_fast_track_pr_is_never_pulled_past_the_target(monkeypatch, tmp_path):
    """tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): never pull past the target; a fast-track
    PR only goes first in the order."""
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)],
            board.READY: [pr(10, board.READY), pr(12, board.READY, rank=board.FAST_TRACK), pr(13, board.READY)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [] and f'fill: Staging {A.FLOOR}, target {A.FLOOR}: no pull needed' in out


def test_fill_runs_the_pre_merge_every_pass_also_at_the_target(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    seen = []
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, A.FLOOR + 1)], board.READY: [pr(10, board.READY)]}
    cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    monkeypatch.setattr(F.Flow, 'premerge', lambda self, c, scratch: seen.append(sorted(c)) or {})
    c = A.Cascade(g=CascadeGate(served_files=False), dry_run=True, out=lambda l: None, sleep=lambda s: None)
    c.pulled = []
    c.fill()
    assert seen and board.READY in seen[0]


def test_the_pull_order_puts_the_fast_track_pr_first():
    ready = [pr(5, board.READY, rank=board.NORMAL), pr(9, board.READY, rank=board.FAST_TRACK),
             dict(pr(7, board.READY, rank=board.MILESTONE), due='2026-11-15', milestone='ptw 15')]
    assert [p['number'] for p in flow.ready_order(ready, {9: 3})] == [9, 7, 5]


def test_a_ready_pr_that_conflicts_with_main_is_fixed_in_place_and_the_fill_waits(monkeypatch, tmp_path):
    """(1) Michal 9:10 PM ET: #481 and #484 conflicted with origin/main and the cascade ended
    quietly. tangentswarm #144 (Michal 2026-11-18 2:09 PM ET): only the TOP Ready PR is checked.
    tangentswarm #146 (2:18 PM ET): it stays in Ready (pre-merging), its worker fixes it in
    place, and the fill waits: #484 and #486 are not checked, moved or pulled."""
    cols = {board.READY: [pr(481, board.READY), pr(484, board.READY), pr(486, board.READY)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False), main_dirty={481, 484})
    assert pulled == []
    assert 'NEEDS_FIX #481 f481.ts' in out and not any('#484' in l or '#486' in l for l in out)
    assert not any('In Progress' in l for l in out)
    assert any(l.startswith('pull: would: #481 stays in Ready (pre-merging) and its worker would fix it in place: '
                            'it conflicts with main in f481.ts') for l in out)
    assert any(l.startswith('main check: #481 conflicts with origin/main: it stays in Ready (pre-merging)') for l in out)
    assert any(l.startswith('fill: #481 (the top Ready PR) is fixed in place (pre-merging): the fill waits')
               for l in out)


def fix_case(monkeypatch, tmp_path, rc):
    from tangentswarm import flow as F, slots
    monkeypatch.setattr(F, 'log', lambda line: None)
    merge_fakes(monkeypatch, main_dirty={481})
    moved, revived = [], []
    g = CascadeGate()
    g['move'] = lambda item, col: moved.append((item, col))
    monkeypatch.setattr(slots, 'revive', lambda n, msg, out=print, **k: revived.append((n, msg, k))
                        or (rc and out('#481 has no surviving worktree')) or rc)
    out = []
    c = A.Cascade(g=g, dry_run=False, out=out.append, sleep=lambda s: None)
    cols = {board.READY: [pr(481, board.READY)]}
    assert [p['number'] for p in c.main_check(cols)[board.READY]] == [481] and c.waiting == 481
    return c, cols, out, moved, revived


def test_a_live_fix_keeps_the_card_in_ready_and_relays_once_per_head(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    c, cols, out, moved, revived = fix_case(monkeypatch, tmp_path, rc=0)
    assert moved == []  # never In Progress (tangentswarm #146)
    assert (481, 'add', [F.PRE_MERGING]) in F.LABELS_WRITTEN
    n, text, kw = revived[0]
    assert n == 481 and 'conflicts with main in f481.ts' in text and 'stays in Ready' in text
    assert kw.get('priority') and 'no rebase' in text.lower() and '\n' not in text
    assert (tmp_path / 'relay-481-fix.md').read_text() == text + '\n'
    assert F.load_fixes()['481']['relayed'] == f'{481:040d}'
    c.main_check(cols)  # same head: no second relay, no move
    assert len(revived) == 1 and moved == [] and c.waiting == 481


def test_a_fix_with_no_worker_starts_one_and_the_card_stays_in_ready(monkeypatch, tmp_path):
    from tangentswarm import flow as F, slots
    monkeypatch.setattr(F.Flow, 'start_worker', lambda self, p, path, why: f"no worker ({why}); queued a worker start")
    c, cols, out, moved, revived = fix_case(monkeypatch, tmp_path, rc=1)
    assert moved == [] and F.fixing(481) and F.load_fixes()['481']['relayed'] == f'{481:040d}'
    assert slots.load()['queued'] == {} and len(revived) == 1


def test_a_fix_no_worker_takes_is_tried_again_next_pass(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    monkeypatch.setattr(F.Flow, 'start_worker', lambda self, p, path, why: 'no start: memory')
    c, cols, out, moved, revived = fix_case(monkeypatch, tmp_path, rc=1)
    assert moved == [] and 'relayed' not in F.load_fixes()['481']
    assert any('NEEDS_WORKER #481' in l for l in out)
    c.main_check(cols)
    assert len(revived) == 2


def test_a_new_head_of_a_fix_waits_for_codex_then_moves_on(monkeypatch, tmp_path):
    """tangentswarm #146: the worker pushed the fix (a new head, still in Ready). It waits for
    Codex on that head; once Codex is clean the record takes the head and the main check runs."""
    from tangentswarm import flow as F
    merge_fakes(monkeypatch)
    with F.fix_update() as recs:
        recs['481'] = dict(head='0' * 40, kind='conflict', relayed='0' * 40)
    g = CascadeGate()
    reviews = {'nodes': []}
    g['fetch'] = lambda n, refresh=False: {'number': n, 'headRefOid': f'{n:040d}', 'reviews': reviews}
    out = []
    c = A.Cascade(g=g, dry_run=False, out=out.append, sleep=lambda s: None)
    cols = {board.READY: [pr(481, board.READY), pr(482, board.READY)]}
    c.main_check(cols)
    assert c.waiting == 481 and any('its fixed head 00000000 waits for Codex' in l for l in out)
    assert 'waits for Codex' in c.f.blocked[481]
    reviews['nodes'] = [{'author': {'login': A.CODEX}, 'commit': {'oid': f'{481:040d}'},
                         'comments': {'totalCount': 0}}]
    c.main_check(cols)
    assert c.waiting is None and F.load_fixes()['481']['head'] == f'{481:040d}'


def test_a_fix_record_of_a_pr_that_left_ready_is_dropped(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    merge_fakes(monkeypatch)
    with F.fix_update() as recs:
        recs['481'] = dict(head='x', kind='conflict')
    c = A.Cascade(g=CascadeGate(), dry_run=False, out=lambda l: None, sleep=lambda s: None)
    c.main_check({board.IN_PROGRESS: [pr(481, board.IN_PROGRESS)], board.READY: []})
    assert not F.fixing(481)


def test_empty_ready_stops_the_fill_and_starts_coders_on_active_issues(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(1, board.STAGING)], board.HUMAN_REVIEW: []}
    g = CascadeGate(issues=[(n, board.BACKLOG) for n in (40, 41, 42, 43, 44)])
    out, pulled = cascade(monkeypatch, tmp_path, cols, g)
    assert pulled == [] and out == ['fill: Staging 1, Ready 0: Ready is empty',
                                    'STALLED: Human Review 0, Staging 1, Ready empty: new work must reach Ready',
                                    'START_CLAUDE n=4 issues=#40,#41,#42,#43 new=0/4 rework=0/3 ready=0/6 '
                                    'backlog_without_pr=5 claudes=0/10 launching=0 unstarted=0 keys=#40=(ft:no,due:none,n:40);'
                                    '#41=(ft:no,due:none,n:41);#42=(ft:no,due:none,n:42);#43=(ft:no,due:none,n:43)',
                                    'launch: would queue #40 (issue 40)', 'launch: would queue #41 (issue 41)',
                                    'launch: would queue #42 (issue 42)', 'launch: would queue #43 (issue 43)']


def test_start_claude_also_when_ready_is_full(monkeypatch, tmp_path):
    """(c) Not only when Ready is short: Ready is the buffer, new work never stops."""
    cols = {board.READY: [pr(n, board.READY) for n in range(10, 17)]}
    monkeypatch.setattr(A, 'FLOOR', 0)
    out, _ = cascade(monkeypatch, tmp_path, cols, CascadeGate(issues=[(50, board.BACKLOG), (51, board.BACKLOG)]))
    assert out[-3] == ('START_CLAUDE n=2 issues=#50,#51 new=0/3 rework=0/3 ready=7/6 backlog_without_pr=2 '
                       'claudes=0/10 launching=0 unstarted=0 keys=#50=(ft:no,due:none,n:50);#51=(ft:no,due:none,n:51)')
    assert out[-2:] == ['launch: would queue #50 (issue 50)', 'launch: would queue #51 (issue 51)']


def test_a_dry_run_shows_the_gate_after_every_site_serves_and_no_cornerbot_request(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(1, board.STAGING)], board.READY: [pr(9, board.READY)]}
    g = CascadeGate(served={'work': 'h' * 40, 'minavo': 'h' * 40, 'ptw': 'o' * 40}, has={'h' * 40})
    out, pulled = cascade(monkeypatch, tmp_path, cols, g)
    assert pulled == [9]
    assert not any('CORNERBOT' in l for l in out)  # ptw redeploys the work tip by itself (Michal 2026-11-15)
    assert ('would gate each one when its priority staging site serves the batch (no wait; the daemon queues a cascade '
            'on a .deploy-sha change) (now lagging: ptw.example)') in out
    assert 'would run hr-gate --apply 9 (Ready -> Staging when it passes)' in out


def live(monkeypatch, tmp_path, g, advance=None, ready=(9,), staging=(1,), hr=(901, 902)):
    """A live fill: Flow.pull_batch merges the clean Ready PRs into work ('w'*40) in one push.
    Returns (out, gated PRs, seconds slept, board)."""
    from tangentswarm import flow as F
    gated = []

    def batch(self, prs, c, scratch, room):
        got = c[board.READY][:room]
        F.save_pulls(F.load_pulls() + [dict(pr=p['number'], head=p['head'], branch=f"b{p['number']}", item='I',
                                            work='w' * 40, started=0) for p in got])
        return got

    def adv(self, d, prs, scratch, served=False):
        gated.append(d['pr'])
        F.drop_pull(d['pr'])  # hr-gate --apply moved it
        c[board.READY] = [q for q in c[board.READY] if q['number'] != d['pr']]
        c[board.STAGING].append(pr(d['pr'], board.STAGING))
        self.change(f"#{d['pr']} hr-gate --apply: moved")
    c = {board.STAGING: [pr(n, board.STAGING) for n in staging], board.READY: [pr(n, board.READY) for n in ready],
         board.HUMAN_REVIEW: [pr(n, board.HUMAN_REVIEW) for n in hr]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch)
    monkeypatch.setattr(F.Flow, 'pull_batch', batch)
    monkeypatch.setattr(F.Flow, 'premerge', lambda self, cols, scratch: {})  # tested in test_flow
    monkeypatch.setattr(F.Flow, 'advance', advance or adv)
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, c))
    out, slept = [], []
    A.Cascade(g=g, dry_run=False, out=out.append, sleep=slept.append).fill()
    return out, gated, sum(slept), c


W, O = 'w' * 40, 'o' * 40
PTW_LAGS = {'work': W, 'minavo': W, 'ptw': O}
ALL_SERVE = {'work': W, 'minavo': W, 'ptw': W}


def test_a_batch_waits_on_no_redeploy(monkeypatch, tmp_path):
    """Michal 2026-11-15: Human Review sat empty about 15 min twice while single pulls of #495 and
    #514 each waited about 10 min on a PTW redeploy, holding the serial lock."""
    from tangentswarm import flow as F
    monkeypatch.setattr(A.time, 'time', lambda: 60)  # pulled 1 min ago: a normal redeploy, no warning
    out, gated, slept, _ = live(monkeypatch, tmp_path, CascadeGate(served=PTW_LAGS), ready=(9, 10), staging=())
    assert slept == 0 and gated == []  # no sleep, no gate: the lock is free for a promote at once
    assert 'fill: #9, #10 pulled in one work merge: Staging 0 + 2, target 2' in out
    assert [d['pr'] for d in F.load_pulls()] == [9, 10]
    assert any(l.startswith('waiting: #10 00000000 not served yet by ptw.example (oooooooo); pulled ') for l in out)
    assert not any(l.startswith(('CORNERBOT', 'REDEPLOY_LATE')) for l in out)


def test_the_next_cascade_gates_the_whole_batch_once_ptw_serves_it(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pulls([dict(pr=n, head=f'{n}' * 40, branch='b', item='I', work=W, started=0) for n in (9, 10)])
    g = CascadeGate(served=ALL_SERVE, has={W})
    out, gated, slept, c = live(monkeypatch, tmp_path, g, ready=(9, 10), staging=())
    assert gated == [9, 10] and slept == 0 and F.load_pulls() == []
    assert 'served: the priority staging site serves #9 99999999 (the others roll out in the background)' in out
    assert 'fill: Staging 2, target 2: no pull needed' in out


def test_a_promote_starts_the_next_pull_while_a_batch_still_waits(monkeypatch, tmp_path):
    """(2) A promote frees a Staging slot: the next Ready pull starts in the same pass, and the
    waiting batch gets one check."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=9, head='9' * 40, branch='b', item='I', work=W, started=0))
    out, gated, slept, _ = live(monkeypatch, tmp_path, CascadeGate(served=PTW_LAGS), ready=(9, 11), staging=())
    assert slept == 0 and gated == []
    assert 'fill: #11 pulled in one work merge: Staging 0 + 1 in flight (#9) + 1, target 2' in out
    assert [d['pr'] for d in F.load_pulls()] == [9, 11]


def test_a_pull_without_served_files_is_gated_at_once(monkeypatch, tmp_path):
    out, gated, _, _ = live(monkeypatch, tmp_path, CascadeGate(served_files=False, served=PTW_LAGS), ready=(9,),
                            staging=())
    assert gated == [9] and not any(l.startswith(('REDEPLOY_LATE', 'waiting')) for l in out)


def test_a_web_site_that_never_serves_never_holds_the_lock(monkeypatch, tmp_path):
    g = CascadeGate(served={'work': O, 'minavo': W, 'ptw': W})
    out, gated, slept, _ = live(monkeypatch, tmp_path, g, advance=lambda *a: pytest.fail('no gate'))
    assert gated == [] and slept == 0


def test_a_dry_run_goes_on_after_a_running_pull_would_end(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.READY: [pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s, served=False: self.change(f"#{d['pr']} pull finished"))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(served_files=False))
    assert pulled == [9] and 'pull: would: #3 pull finished' in out
    assert F.load_pull()['pr'] == 3  # a dry run changes nothing


def test_a_dry_run_stops_at_a_waiting_pull(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.READY: [pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=3, head='3' * 40, branch='b', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s, served=False: self.note(f"#{d['pr']} pull waiting (1 min): CI"))
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
    monkeypatch.setattr(F.Flow, 'advance', lambda self, d, prs, s, served=False: self.change(f"#{d['pr']} hr-gate --apply {d['pr']}"))
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


def lag_pass(monkeypatch, tmp_path, age, logs='ptw=/log/ptw-deploy.log'):
    """One pass over pull #480 (pulled `age` s ago) while ptw serves an older work commit."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setenv('SWARM_DEPLOY_LOGS', logs)
    F.save_pull(dict(pr=480, head='5f6e09a4' + '0' * 32, branch='b', item='I', work='w' * 40, started=1000))
    monkeypatch.setattr(A.time, 'time', lambda: 1000 + age)
    monkeypatch.setattr(F.Flow, 'advance', lambda *a, **k: pytest.fail('not served: no gate'))
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {}))
    out, slept = [], []
    g = CascadeGate(served={'work': 'w' * 40, 'minavo': 'w' * 40, 'ptw': 'o' * 40})
    A.Cascade(g=g, out=out.append, sleep=slept.append).fill()
    assert slept == []  # 7:32 PM ET: pass after pass waited 430 s for one ptw redeploy, holding the lock
    return out


def test_a_lagging_pull_gets_one_check_and_no_warning_before_ten_minutes(monkeypatch, tmp_path):
    out = lag_pass(monkeypatch, tmp_path, A.REDEPLOY_LATE - 1)
    assert out[0] == ('waiting: #480 5f6e09a4 not served yet by ptw.example (oooooooo); pulled 9m59s ago; '
                      'no wait: a .deploy-sha change queues the next cascade')


def test_a_ptw_lag_over_ten_minutes_warns_and_names_the_deploy_log(monkeypatch, tmp_path):
    """Michal 2026-11-15: warn only when the ptw .deploy-sha lags the work merge by more than
    10 min, and point at the ptw deploy log (its path comes from the wrapper's environment)."""
    out = lag_pass(monkeypatch, tmp_path, A.REDEPLOY_LATE + 60)
    assert out[0] == ('REDEPLOY_LATE: #480 5f6e09a4 (work wwwwwwww) is not served by ptw.example (oooooooo) '
                      '11m00s after the work merge; see /log/ptw-deploy.log')
    assert lag_pass(monkeypatch, tmp_path, A.REDEPLOY_LATE + 60, logs='')[0].endswith('after the work merge')


def test_deploy_logs_reads_site_equals_path_pairs():
    assert A.deploy_logs({'SWARM_DEPLOY_LOGS': 'ptw=/a.log, work=/b.log,bad'}) == {'ptw': '/a.log', 'work': '/b.log'}
    assert A.deploy_logs({}) == {}


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


def test_a_ui_pr_that_reaches_ready_without_screenshots_gets_the_job_at_once(board_read, monkeypatch):
    """Michal 2026-11-15 2:23 AM ET: #519 was demoted 90 min after it reached Ready with no
    image, while no Claude was on it."""
    from tangentswarm import slots
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    sent = []
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: sent.append((n, text)) or out('sent') or 0)
    g = Gate(cols, details={7: codex_pr()})
    g['fetch'] = lambda n, refresh=False: {'number': n, 'body': 'where to see it: staging',
                                           'files': {'nodes': [{'path': 'web/src/badge.tsx'}]}}
    out, calls = run(g)
    assert g.moves == [('I7', board.READY)] and sent and sent[0][0] == 7
    assert 'needs screenshots before Human Review' in sent[0][1] and '\n' not in sent[0][1]
    assert any('#7 screenshot job sent to its worker' in l for l in out)
    run(g)
    assert len(sent) == 1  # once per head


@pytest.mark.parametrize('paths', [['api/routes/a.py', 'api/x.ts'], ['docs/flow.md', 'README.md']])
def test_api_only_and_docs_only_prs_get_no_screenshot_job(board_read, monkeypatch, paths):
    from tangentswarm import slots
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    sent = []
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: sent.append(n) or 0)
    g = Gate(cols, details={7: codex_pr()})
    g['fetch'] = lambda n, refresh=False: {'number': n, 'body': '', 'files': {'nodes': [{'path': p} for p in paths]}}
    run(g)
    assert g.moves == [('I7', board.READY)] and sent == []


def test_codex_clean_on_the_head_outweighs_an_open_thread_from_an_older_head():
    """MethBot 2026-11-15: the worker pushed the fix but did not resolve the thread."""
    head, old = 'a' * 40, 'b' * 40

    def thread(commit, tid='T1'):
        return {'id': tid, 'isResolved': False, 'isOutdated': False,
                'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'},
                                        'originalCommit': {'oid': commit} if commit else None}]}}
    receipt = {'author': {'login': 'chatgpt-codex-connector'},
               'body': f"Reviewed commit: {head[:8]} Didn't find any major issues."}
    pr = {'headRefOid': head, 'comments': {'nodes': [receipt]}, 'reviews': {'nodes': []},
          'reviewThreads': {'nodes': [thread(old)]}}
    assert A.codex_clean(pr) and A.addressed_threads(pr) == ['T1']
    pr['reviewThreads']['nodes'] = [thread(head)]
    assert not A.codex_clean(pr) and A.addressed_threads(pr) == []  # a thread on the head blocks
    pr['reviewThreads']['nodes'] = [thread(None)]
    assert not A.codex_clean(pr)  # unknown commit: counts as on the head
    pr['reviewThreads']['nodes'] = [thread(old)]
    pr['comments']['nodes'] = []
    assert not A.codex_clean(pr)  # no clean Codex result on the head yet


def test_the_minbot_pass_resolves_the_addressed_threads(monkeypatch):
    out, mutations = [], []
    adv = A.Advance(g={'gql': lambda q, **v: mutations.append((q.split('(')[0], v['id']))}, out=out.append,
                    minbot=lambda *a: None)
    pr = {'headRefOid': 'a' * 40, 'reviews': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'},
                                                          'commit': {'oid': 'a' * 40}, 'comments': {'totalCount': 0}}]},
          'comments': {'nodes': []},
          'reviewThreads': {'nodes': [{'id': 'T9', 'isResolved': False, 'isOutdated': False,
                                       'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'},
                                                               'originalCommit': {'oid': 'b' * 40}}]}}]}}
    adv.resolve_addressed(7, pr)
    assert mutations == [('mutation', 'T9')] and out == ['#7 resolved 1 addressed Codex thread(s) from an older head']


def test_a_live_signal_queues_the_launches_and_respects_the_claude_cap(monkeypatch, tmp_path):
    """MethBot 2026-11-15 3:35 AM ET: START_CLAUDE named issues seven times; none started.
    tangentswarm #44: capacity is live state: running Claudes, launch and start requests in
    the spool, and the worker memory; never reservations or a held list."""
    from tangentswarm import launches, slots, spool
    g = CascadeGate(issues=[(n, board.BACKLOG) for n in (40, 41, 42, 43)])
    out = []
    c = A.Cascade(g=g, dry_run=False, out=out.append)
    monkeypatch.setattr(slots, 'live_claudes', lambda: 8)  # 2 under the cap of 10
    c.signal({})
    reqs = sorted(json.loads(p.read_text())['issue'] for p in spool.SPOOL.glob('*.req.json'))
    assert reqs == [40, 41]
    assert out[-1] == 'launch: queued #40, #41 for swarm-promoted (branch, draft PR, brief, worker)'
    assert slots.load()['named'] == {}  # named only once a draft PR exists
    out.clear()
    c.signal({})  # the 2 launch requests in the spool are 2 Claudes about to start
    assert not any(l.startswith('START_CLAUDE') for l in out) and 'launching=2' in out[-1], out
    monkeypatch.setattr(slots, 'live_claudes', lambda: 6)
    out.clear()
    c.signal({})  # room again: the picker skips the issues being launched
    assert any(l.startswith('START_CLAUDE n=2 issues=#42,#43') for l in out), out
    for p in spool.SPOOL.glob('*.req.json'):
        p.unlink()  # the launches ended
    slots.save(dict(slots.load(), launching={}))
    monkeypatch.setattr(slots, 'worker_memory', lambda: dict(current=4500 * 1024 ** 2, high=4608 * 1024 ** 2,
                                                             max=5120 * 1024 ** 2, avail=4 * 1024 ** 3))
    out.clear()
    c.signal({})  # the worker memory holds starts: no launch, read live
    assert any('no capacity: worker memory holds starts' in l for l in out), out
    monkeypatch.setattr(slots, 'worker_memory', lambda: None)
    monkeypatch.setattr(slots, 'live_claudes', lambda: 10)
    out.clear()
    c.signal({})
    assert any('no capacity: 10 Claudes run or start (cap 10)' in l for l in out), out


def test_no_minbot_pass_while_the_pr_has_an_open_question_for_michal():
    """platform #541 (2026-11-15): the MinBot pass moved a PR to Ready while its worker waited
    for Michal's decision."""
    from tangentswarm.stophook import QUESTION_MARK
    assert A.open_question({'body': '## Question for Michal\nOld or new colors?'}) == \
        'the PR body has an open question for Michal'
    issue = {'number': 419, 'comments': {'nodes': [{'body': QUESTION_MARK + '\nQuestion from the worker'}]}}
    assert A.open_question({'body': '', 'closingIssuesReferences': {'nodes': [issue]}}) == \
        'issue #419 has an unanswered question for Michal'
    answered = {'number': 419, 'comments': {'nodes': [{'body': 'Use the new colors.'}]}}  # Michal replied last
    assert A.open_question({'body': 'Fixes #419', 'closingIssuesReferences': {'nodes': [answered]}}) is None


def test_a_minbot_pass_from_green_ci_queues_a_cascade(board_read, monkeypatch):
    """Codex on PR #26: a CI-driven pass moved the PR to Ready and nothing restocked Staging."""
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    queued = []
    monkeypatch.setattr(A, 'queue_cascade', lambda qdir=None: queued.append(1) or 'queued')
    monkeypatch.setattr('tangentswarm.promoted.read_columns', lambda gql, max_age=0: cols)
    out, calls = [], []
    adv = A.Advance(g=Gate(cols, details={7: codex_pr()}), out=out.append, minbot=lambda *a: calls.append(a))
    adv.minbot_one(7)
    assert queued == [1] and A.RERUN.exists() and 'reached Ready: cascade queued' in out[-1]


# --- tangentswarm #102: Auto Review PRs held by a Codex thread or a conflict go back to rework ---

CODEX = {'login': 'chatgpt-codex-connector[bot]'}


def head_thread(commit=H, outdated=False, body='P1: the retry drops the last page', path='api/x.py', tid='T1'):
    return {'id': tid, 'isResolved': False, 'isOutdated': outdated,
            'comments': {'nodes': [{'author': CODEX, 'originalCommit': {'oid': commit}, 'path': path, 'body': body}]}}


class Seen(list):
    pass


@pytest.fixture
def revives(monkeypatch):
    """slots.revive stand-in: records (n, text, kwargs) and returns the next rc (default 0)."""
    from tangentswarm import slots
    seen, rcs = Seen(), []

    def fake(n, text, out=print, **k):
        seen.append((n, text, k))
        rc = rcs.pop(0) if rcs else 0
        out({0: f'sent to #{n}', 3: 'held: the 3 rework slots are in use', 1: 'refused: worktree is gone'}[rc])
        return rc
    monkeypatch.setattr(slots, 'revive', fake)
    seen.rcs = rcs
    return seen


@pytest.fixture
def ar7(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    return cols


def test_an_open_codex_thread_on_the_head_sends_the_pr_back_to_rework(ar7, revives, monkeypatch):
    """methodology #106: platform #528 had an open Codex P2 thread on its head and a later clean
    receipt on the same head; it sat in Auto Review for hours."""
    g = Gate(ar7, details={7: codex_pr(threads=[head_thread()])})  # the clean receipt is there too
    g['fetch'] = lambda n, refresh=False: pytest.fail('no full read for a PR that goes back to rework')
    out, calls = run(g)
    assert calls == [] and g.moves == [('I7', board.IN_PROGRESS)] and board.DRAFTED == [(7, A.REPO)]
    (n, text, kw), = revives
    assert n == 7 and kw.get('queue') is True
    assert 'open Codex thread(s) on your head' in text and '[api/x.py] P1: the retry drops the last page' in text
    assert 'ROOT-CAUSE RULE' in text
    assert f"NEEDS_REWORK #7 has 1 open Codex thread(s) on {H[:8]}: Auto Review -> In Progress, draft" in out
    from tangentswarm import moves
    assert 'Auto Review' in moves.LOG.read_text() and 'In Progress' in moves.LOG.read_text()


def test_a_conflict_with_main_sends_the_pr_back_to_rework(ar7, revives):
    """methodology #106: platform #539 was Codex-clean but CONFLICTING with main."""
    g = Gate(ar7, details={7: dict(codex_pr(), mergeable='CONFLICTING')})
    out, calls = run(g)
    assert g.moves == [('I7', board.IN_PROGRESS)] and board.DRAFTED == [(7, A.REPO)]
    (n, text, kw), = revives
    assert 'Merge origin/main and resolve the conflicts' in text and 'Do not rebase' in text
    assert out[-1].endswith('#7 conflicts with main (GitHub mergeable): sent back to rework')


def test_a_held_revive_is_queued_and_the_card_still_moves(ar7, revives):
    revives.rcs.append(3)
    g = Gate(ar7, details={7: dict(codex_pr(), mergeable='CONFLICTING')})
    out, _ = run(g)
    assert g.moves == [('I7', board.IN_PROGRESS)]
    assert any('WAITING_FOR_CLAUDE: waiting for a Claude: the revive is queued' in line for line in out)
    assert revives[0][2] == {'queue': True}


def test_one_revive_per_head_and_reason(ar7, revives):
    """A failed card move is retried on the next check without a second feedback."""
    g = Gate(ar7, details={7: codex_pr(threads=[head_thread()])})
    run(g)
    run(g)
    assert len(revives) == 1 and g.moves == [('I7', board.IN_PROGRESS)] * 2


def test_no_worker_still_sends_the_card_to_in_progress_to_wait(ar7, revives, tmp_path):
    """tangentswarm #101 (drop parking): a conflicting Auto Review PR whose worktree is gone
    goes to In Progress anyway. Its relay is saved as the brief of a worker start, which the
    cascade's start retry runs when capacity frees."""
    from tangentswarm import slots
    revives.rcs.append(1)  # 'refused: worktree is gone'
    # Codex on PR #109: the board record has no branch; the start takes headRefName
    g = Gate(ar7, details={7: dict(codex_pr(), mergeable='CONFLICTING', headRefName='ptw/b7')})
    out, _ = run(g)
    assert g.moves == [('I7', board.IN_PROGRESS)] and board.DRAFTED == [(7, A.REPO)]
    assert any('WAITING_FOR_CLAUDE' in line and 'a worker start with the relay relay-7-conflict.md' in line
               for line in out)
    rec = slots.load()['unstarted']['7']
    assert rec['brief'] == str(tmp_path / 'relay-7-conflict.md') and rec['setup'] == 'done'
    assert rec['branch'] == 'ptw/b7' and 'headRefName' in A.CODEX_FIELDS
    assert 'merge origin/main' in (tmp_path / 'relay-7-conflict.md').read_text().lower()
    run(g)  # one revive per head and reason
    assert len(revives) == 1

def test_an_outdated_or_older_head_thread_is_no_rework(ar7, revives):
    """Only an open, not outdated thread on the head counts; an older-head thread with Codex
    clean on the head is resolved by the MinBot pass (addressed_threads)."""
    for t in (head_thread(outdated=True), head_thread(commit='b' * 40)):
        g = Gate(ar7, details={7: codex_pr(threads=[t])})
        out, _ = run(g)
        assert revives == [] and g.moves == [('I7', board.READY)], out


def test_a_draft_or_an_open_question_is_no_rework(ar7, revives):
    for d in (codex_pr(draft=True, threads=[head_thread()]),
              dict(codex_pr(threads=[head_thread()]), body='Questions for Michal: which?')):
        g = Gate(ar7, details={7: d})
        run(g)
        assert revives == [] and g.moves == []


def test_a_dry_run_sends_nothing_back(ar7, revives):
    g = Gate(ar7, details={7: dict(codex_pr(), mergeable='CONFLICTING')})
    out, _ = run(g, dry=True)
    assert revives == [] and g.moves == [] and board.DRAFTED == []
    assert out[0].startswith('would send #7 back to rework (conflicts with main')


def test_the_cascade_sends_auto_review_prs_back_to_rework(ar7, revives):
    """The cascade checks every Auto Review PR, not only the one a Stop hook names."""
    ar7[board.AUTO_REVIEW].append(dict(number=8, head=H, item='I8'))
    ar7[board.AUTO_REVIEW].append(dict(number=9, head=H, item='I9'))
    g = Gate(ar7, details={7: codex_pr(threads=[head_thread()]), 8: codex_pr(n=8),
                           9: dict(codex_pr(n=9), mergeable='CONFLICTING')})
    out = []
    adv = A.Advance(g=g, out=out.append, minbot=lambda *a: pytest.fail('no MinBot pass in this step'))
    A.Cascade.auto_review(adv, ar7)
    assert [r[0] for r in revives] == [7, 9]
    assert g.moves == [('I7', board.IN_PROGRESS), ('I9', board.IN_PROGRESS)]


def test_the_cascade_runs_the_revive_queue_after_park(monkeypatch):
    """A park frees a rework slot: the queued revives run right after it, in the same pass."""
    from tangentswarm import slots
    order = []
    monkeypatch.setattr(slots, 'run_queued', lambda out=print, prs=None, dry=False: order.append(('queue', prs)))
    c = A.Cascade.__new__(A.Cascade)
    c.out, c.dry, c.prs, c.why, c.g = (lambda line: None), False, {7: {'state': 'OPEN'}}, [], {'gql': None}
    c.clock = lambda: 0
    monkeypatch.setattr(promoted, 'read_columns', lambda gql, max_age=None: {})
    for name in ('promote', 'unblock', 'auto_review', 'park', 'flush', 'signal'):
        monkeypatch.setattr(A.Cascade, name, lambda self, *a, _n=name: order.append(_n) or True)
    monkeypatch.setattr(A.Cascade, 'fill', lambda self: order.append('fill') or {})
    assert c._run(0) == 0
    assert order == ['promote', 'fill', 'unblock', 'auto_review', 'park', ('queue', {7: {'state': 'OPEN'}}),
                     'flush', 'signal']


def test_the_thread_feedback_fits_one_revive_line():
    from tangentswarm import slots
    threads = [head_thread(body='x' * 5000, tid=f'T{i}') for i in range(10)]
    text = A.thread_rework_text(7, H, threads)
    assert slots.one_line(text) and len(text) <= slots.FEEDBACK_MAX


def test_the_start_retry_starts_waiting_workers_in_pick_order():
    """tangentswarm #101: a conflicting PR with no worktree waits as an unstarted record; when
    capacity frees, the starts go in the shared pick order, not by record age."""
    a = A.Cascade.__new__(A.Cascade)
    out = []
    a.out, a.dry = out.append, True
    a.prs = {n: dict(number=n, state='OPEN', labels=[], closes=[]) for n in (5, 6)}
    a.prs[6].update(due='2026-11-20', milestone='m')
    unstarted = {'5': dict(branch='b5', at=1), '6': dict(branch='b6', at=2)}
    assert a.retry_starts(unstarted, 1) == 0
    assert out == ['start: would queue the worker start of #6 (b6)']


def test_an_auto_review_pr_with_no_branch_anywhere_keeps_its_feedback_queued(ar7, revives):
    """Codex on PR #109: no branch in the record or the read: no start on an empty branch.
    The feedback stays queued (with its relay), and the card still waits in In Progress."""
    from tangentswarm import slots
    revives.rcs.append(1)  # 'refused: worktree is gone'
    g = Gate(ar7, details={7: dict(codex_pr(), mergeable='CONFLICTING')})
    run(g)
    assert g.moves == [('I7', board.IN_PROGRESS)]
    assert slots.load()['unstarted'] == {} and slots.load()['queued']['7']['relay'].endswith('relay-7-conflict.md')


# -- tangentswarm #111: a merge checks only the top Ready PR ----------------------------------

def test_the_main_check_looks_only_at_the_top_ready_pr(monkeypatch, tmp_path):
    """tangentswarm #111 (Michal 2026-11-17 10:45 AM ET): #484 behind the top conflicts with
    main, but it is not the top, so it is not checked and stays in Ready."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch, main_dirty={484})
    seen = []
    clean = F.Flow.clean
    monkeypatch.setattr(F.Flow, 'clean', lambda self, s, a, b: seen.append(int(b)) or clean(self, s, a, b))
    out = []
    c = A.Cascade(g=CascadeGate(served_files=False), dry_run=True, out=out.append, sleep=lambda s: None)
    cols = {board.READY: [pr(484, board.READY), pr(481, board.READY), pr(490, board.READY, draft=True)]}
    assert [p['number'] for p in c.main_check(cols)[board.READY]] == [484, 481, 490]
    assert seen == [481] and not any('NEEDS_FIX' in l for l in out)


def test_a_pr_in_a_pull_is_not_the_top_for_the_main_check(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pulls([dict(pr=3, head='3' * 40, branch='b3', item='I3', work='w' * 40, started=0)])
    merge_fakes(monkeypatch, main_dirty={3, 9})
    out = []
    c = A.Cascade(g=CascadeGate(served_files=False), dry_run=True, out=out.append, sleep=lambda s: None)
    left = c.main_check({board.READY: [pr(3, board.READY), pr(9, board.READY), pr(10, board.READY)]})
    assert [p['number'] for p in left[board.READY]] == [3, 9, 10]  # tangentswarm #146: #9 stays in Ready
    assert 'NEEDS_FIX #9 f9.ts' in out and 'NEEDS_FIX #3 f3.ts' not in out


def test_a_human_review_merge_moves_the_next_pr_up_and_checks_no_other_ready_pr(monkeypatch, tmp_path):
    """tangentswarm #111 done-when: #544 in Human Review merged. The promote moves the
    pre-merged #447 Staging -> Human Review, and the same pass pulls the next Ready PR (#528)
    into Staging. Only #528 is checked against main, only the new top (#539) is pre-merged,
    and #541 and #546 (both conflict with the new main) keep their labels and column."""
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    merge_fakes(monkeypatch, main_dirty={541, 546})
    checked, premerged, labelled = [], [], []
    clean = F.Flow.clean
    monkeypatch.setattr(F.Flow, 'clean', lambda self, s, a, b: checked.append(int(b)) or clean(self, s, a, b))
    monkeypatch.setattr(F.Flow, 'future_main', lambda self, s, cols: ('M F', 'F' * 40, None))
    monkeypatch.setattr(F.Flow, 'premerge_one', lambda self, p, s, fm: premerged.append(p['number']) or 'T' * 40)
    monkeypatch.setattr(F.Flow, 'set_labels', lambda self, p, want, drop=True: labelled.append(p['number']))
    pulled = []

    def pull_batch(self, prs, c, scratch, room):
        fits = F.ready_order(c.get(board.READY, []))[:room]
        pulled.extend(q['number'] for q in fits)
        return fits
    monkeypatch.setattr(F.Flow, 'pull_batch', pull_batch)
    ready = [pr(n, board.READY, own_labels=['merge-ready']) for n in (546, 541, 539, 528)]
    cols = {board.HUMAN_REVIEW: [pr(600, board.HUMAN_REVIEW)],
            board.STAGING: [pr(447, board.STAGING), pr(518, board.STAGING)], board.READY: ready}
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {k: list(v) for k, v in cols.items()}))
    out = []
    c = A.Cascade(g=CascadeGate(served_files=False), dry_run=True, out=out.append, sleep=lambda s: None)
    c.moved, c.pulled = [447], []  # the promote step moved #447 up (dry run)
    after = c.fill()
    assert [p['number'] for p in after[board.HUMAN_REVIEW]] == [600, 447]
    assert pulled == [528] and [p['number'] for p in after[board.STAGING]] == [518, 528]
    assert checked == [528] and premerged == [539]
    assert 541 not in labelled and 546 not in labelled
    assert [p['number'] for p in after[board.READY]] == [546, 541, 539]
    assert not any('NEEDS_REWORK' in l or 'would move' in l for l in out)
