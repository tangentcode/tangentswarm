"""swarm-advance: one step from the end of the line (promote, else pull, else MinBot pass)."""
import pytest

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
    def __enter__(self):
        return self

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
    assert calls[0] == ('label', 'add', 7, 'MinBot-passed', '--repo', A.REPO)
    assert calls[1][:2] == ('evidence', 7) and H in calls[1] and A.pass_text(H) in calls[1]
    assert g.moves == [('I7', board.READY)]
    assert out == [f"MinBot-pass #7 at {H[:8]}: label, pass comment, Auto Review -> Ready"]


def test_minbot_pass_dry_run_moves_nothing(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g, dry=True)
    assert g.moves == [] and out[0].startswith('would MinBot-pass #7')
    assert len(calls) == 2  # minbot without --apply is its own dry run


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


class CascadeGate(dict):
    def __init__(self, site='work', served_files=True, served_sha=None):
        super().__init__()
        files = {'nodes': [{'path': 'web/a.ts' if served_files else 'README.md'}]}
        self.update(fetch=lambda n, refresh=False: {'number': n, 'files': files, 'headRefName': f'b{n}'},
                    site_for=lambda full, o: site, ScratchRepo=Scratch, SITES={'ptw': ('ptw.example',)},
                    served=lambda s: ('host', served_sha, None, None, None),
                    contains=lambda anc, desc: False)


def cascade(monkeypatch, tmp_path, cols, g, dry=True, **kw):
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    pulled = []

    def pull(self, prs, c, scratch, limit=None):
        assert limit == A.STAGING_LIMIT
        p = c[board.READY][0]
        pulled.append(p['number'])
        self.change(f"#{p['number']} pulled")
        return p
    monkeypatch.setattr(F.Flow, 'pull', pull)
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {k: list(v) for k, v in cols.items()}))
    out = []
    c = A.Cascade(g=g, dry_run=dry, out=out.append, sleep=lambda s: None, **kw)
    c.signal(c.fill())
    return out, pulled


def test_fill_pulls_until_staging_holds_five(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in (1, 2, 3)],
            board.READY: [pr(n, board.READY) for n in (4, 5, 6, 7)],
            board.IN_PROGRESS: [pr(8, board.IN_PROGRESS)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert pulled == [4, 5]
    assert 'fill: Staging 5/5, Ready 2: Staging is full' in out
    assert out.count('would run hr-gate --apply 4 (Ready -> Staging when it passes)') == 1
    assert out[-1] == 'START_CLAUDE n=1 ready=2/6 working=1/2'


def test_full_staging_pulls_nothing(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, 6)],
            board.READY: [pr(n, board.READY) for n in range(6, 12)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert pulled == [] and out == ['fill: Staging 5/5, Ready 6: Staging is full', 'signal: Ready 6/6: no new coder']


def test_empty_ready_stops_the_fill_and_asks_for_two_coders(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(1, board.STAGING)]}
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert pulled == [] and out == ['fill: Staging 1/5, Ready 0: Ready is empty', 'START_CLAUDE n=2 ready=0/6 working=0/2']


def test_no_start_claude_when_enough_coders_work(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, 6)],
            board.IN_PROGRESS: [pr(7, board.IN_PROGRESS), pr(8, board.IN_PROGRESS)]}
    out, _ = cascade(monkeypatch, tmp_path, cols, CascadeGate())
    assert out[-1] == 'signal: Ready 0/6, 2 PR(s) In Progress (aim 2): no START_CLAUDE'


def test_a_ptw_pull_prints_the_cornerbot_request(monkeypatch, tmp_path):
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, 5)], board.READY: [pr(9, board.READY)]}
    cols[board.READY].append(pr(10, board.READY))
    out, pulled = cascade(monkeypatch, tmp_path, cols, CascadeGate(site='ptw'))
    assert pulled == [9]  # a live run waits for the redeploy here, so the dry run stops too
    assert any(l.startswith('CORNERBOT_REQUEST (dry run): redeploy ptw.example') and '#9 b9' in l for l in out)
    assert 'would stop the fill here: #9 waits for the cornerbot redeploy; the next cascade gates it' in out
    assert out[-1] == 'START_CLAUDE n=2 ready=1/6 working=0/2'


def test_a_live_pull_waits_for_the_served_sha_then_gates(monkeypatch, tmp_path):
    """Live: the pull merged into work; the second poll serves the work commit; then advance."""
    from tangentswarm import flow as F
    g = CascadeGate()
    polls = iter([None, 'w' * 40])
    g['served'] = lambda s: ('host', next(polls), None, None, None)
    advanced = []

    def start(self, prs, c, scratch, limit=None):
        p = c[board.READY].pop(0)
        F.save_pull(dict(pr=p['number'], head=p['head'], branch='b', item='I', work='w' * 40, started=0))
        return p

    def advance(self, d, prs, scratch):
        advanced.append(d['pr'])
        F.save_pull(None)  # hr-gate --apply moved it
        c[board.STAGING].append(pr(d['pr'], board.STAGING))
    c = {board.STAGING: [pr(n, board.STAGING) for n in range(1, 5)], board.READY: [pr(9, board.READY)]}
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setattr(F.Flow, 'pull', start)
    monkeypatch.setattr(F.Flow, 'advance', advance)
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, c))
    out = []
    casc = A.Cascade(g=g, dry_run=False, out=out.append, sleep=lambda s: None)
    casc.fill()
    assert advanced == [9]
    assert 'served: host .deploy-sha wwwwwwww contains #9 ' + f'{9:040d}'[:8] in out
    assert 'fill: Staging 5/5, Ready 0: Ready is empty' in out


def test_a_live_ptw_pull_stops_the_fill_until_cornerbot_redeploys(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    monkeypatch.setattr(F, 'STATE', tmp_path / 'pull.json')
    F.save_pull(dict(pr=9, head='9' * 40, branch='ptw/x', item='I', work='w' * 40, started=0))
    monkeypatch.setattr(F.Flow, 'advance', lambda *a: pytest.fail('not served yet: no gate'))
    monkeypatch.setattr(F.Flow, 'columns', lambda self: ({}, {board.READY: [pr(4, board.READY)]}))
    out = []
    A.Cascade(g=CascadeGate(site='ptw', served_sha='o' * 40), out=out.append, sleep=lambda s: None).fill()
    assert out[0].startswith('CORNERBOT_REQUEST: redeploy ptw.example from platform work wwwwwwww')
    assert out[1].startswith('waiting: host .deploy-sha oooooooo does not contain #9')
    assert F.load_pull()['asked'] is True  # asked once; the next cascade does not ask again


def test_a_dry_run_goes_on_after_a_running_pull_would_end(monkeypatch, tmp_path):
    from tangentswarm import flow as F
    cols = {board.STAGING: [pr(n, board.STAGING) for n in range(1, 5)], board.READY: [pr(9, board.READY)]}
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
    assert pulled == [] and 'note: #3 pull waiting (1 min): CI' in out
