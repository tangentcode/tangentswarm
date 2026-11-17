"""swarm-flow: methodology#56 step: promote, pull one Ready PR, advance the pull, never touch other Ready PRs."""
import subprocess

import pytest

from tangentswarm import board, flow

IP, AR, RD, ST, HR = board.IN_PROGRESS, board.AUTO_REVIEW, board.READY, board.STAGING, board.HUMAN_REVIEW


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setattr(flow, 'LOG', tmp_path / 'flow.log')
    return tmp_path


# --- pure helpers ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def worker_runs(monkeypatch):
    """Flow.fail and the screenshot job need a worker: by default its Claude takes the relay
    (never a real tmux or systemd-run in a test)."""
    from tangentswarm import slots
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: out(f'sent to #{n}') or 0)


def test_gate_verdict_pass_wait_fail():
    assert flow.gate_verdict([('1 not draft', True, 'ready')]) == ('pass', [])
    v, why = flow.gate_verdict([('5 CI green (Linux)', False, 'Linux IN_PROGRESS'),
                                ('7a screenshots current', False, 'no image in body')])
    assert v == 'wait' and len(why) == 2
    assert flow.gate_verdict([('5 CI green (Linux)', False, 'rs-linux FAILURE')])[0] == 'fail'
    assert flow.gate_verdict([('3 Codex clean on head', False, 'open thread'),
                              ('7b where-to-see line', False, 'missing')])[0] == 'fail'


def test_ready_order_is_the_shared_pick_order():
    """tangentswarm #35: fast-track, then the soonest milestone due date, then the number
    (MVP PRs due Dec 1 went ahead of ptw 15 PRs due Nov 15)."""
    ready = [dict(number=9, due='2026-12-01', milestone='MVP'), dict(number=4, milestone='no date'),
             dict(number=7, due='2026-11-15', milestone='ptw 15'), dict(number=2),
             dict(number=8, due='2026-12-01', milestone='MVP', rank=board.FAST_TRACK),
             dict(number=3, due='2026-11-15', milestone='ptw 15')]
    assert [p['number'] for p in flow.ready_order(ready, {3: 5})] == [8, 3, 7, 9, 4, 2]


def test_event_filter_needs_no_api():
    st = {'action': 'edited', 'changes': {'field_value': {'field_node_id': board.STATUS_FIELD}}}
    other = {'action': 'edited', 'changes': {'field_value': {'field_node_id': 'PVTF_other'}}}
    assert flow.event_wants_step('projects_v2_item', st)
    assert not flow.event_wants_step('projects_v2_item', other)
    assert flow.event_wants_step('pull_request', {'action': 'closed', 'pull_request': {'merged': True}})
    assert not flow.event_wants_step('pull_request', {'action': 'closed', 'pull_request': {'merged': False}})
    assert not flow.event_wants_step('check_suite', {'action': 'completed'})


# --- stack merges in a real bare repo ------------------------------------------

class Scratch:
    def __init__(self, path):
        self.dir, self.main = str(path), None


def run(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    w = tmp_path / 'w'
    w.mkdir()
    run(w, 'init', '-q', '-b', 'main')
    run(w, 'config', 'user.email', 't@example.com')
    run(w, 'config', 'user.name', 't')
    (w / 'a.txt').write_text('1\n2\n3\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'base')
    main = run(w, 'rev-parse', 'HEAD')

    def branch(name, path, text):
        run(w, 'checkout', '-q', '-b', name, main)
        (w / path).write_text(text)
        run(w, 'add', '.')
        run(w, 'commit', '-qm', name)
        return run(w, 'rev-parse', 'HEAD')
    heads = dict(main=main, hr1=branch('hr1', 'b.txt', 'hr1\n'), x=branch('x', 'a.txt', '1\nX\n3\n'),
                 y=branch('y', 'a.txt', '1\nY\n3\n'), z=branch('z', 'c.txt', 'z\n'))
    return Scratch(w / '.git'), heads


def test_stack_conflict_finds_the_base_it_clashes_with(repo):
    s, h = repo
    assert flow.stack_conflict(s, h['main'], [('#1', h['hr1'])], h['z']) is None
    assert flow.stack_conflict(s, h['main'], [('#1', h['hr1']), ('#2', h['x'])], h['y']) == '#2'


def test_merge_commit_has_both_parents(repo):
    s, h = repo
    c = flow.merge_commit(s, h['x'], h['z'], 'm')
    parents = run(s.dir, 'log', '-1', '--format=%P', c).split()
    assert parents == [h['x'], h['z']]
    why = []
    assert flow.merge_commit(s, h['x'], h['y'], 'm', why=why) is None
    assert why and 'conflict' in why[0].lower()


def test_merge_problem_text_reads_exit_conflict_lines_and_markers():
    assert flow.merge_problem_text(1, 'abc\n', '') == 'merge-tree exit 1 (conflict)'
    assert flow.merge_problem_text(0, 'abc\nCONFLICT (content): Merge conflict in a.txt\n', '').startswith('CONFLICT')
    assert 'marker' in flow.merge_problem_text(0, 'abc\n', '+ok\n+<<<<<<< origin/work\n')
    assert flow.merge_problem_text(0, 'abc\n', '+a line that names <<<<<<< in text\n') is None
    assert flow.merge_problem_text(0, 'abc\n', '+fine\n') is None


def test_a_clean_merge_that_adds_conflict_markers_is_refused(repo):
    """Like work 0d249b69: a tip that carries committed <<<<<<< markers."""
    s, h = repo
    w = s.dir[:-len('/.git')]
    run(w, 'checkout', '-q', '-b', 'bad', h['main'])
    with open(f'{w}/d.txt', 'w') as f:
        f.write('<<<<<<< origin/work\nA\n=======\nB\n>>>>>>> tip\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'bad')
    why = []
    assert flow.merge_commit(s, h['z'], run(w, 'rev-parse', 'HEAD'), 'm', why=why) is None
    assert 'conflict marker' in why[0]


def test_merge_work_refuses_a_conflict_and_leaves_work_alone(repo, tmp_path):
    s, h = repo
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', str(origin))
    run(s.dir, 'push', '-q', str(origin), f"{h['x']}:refs/heads/work")
    out = []
    g = {'origin_url': lambda: str(origin), 'ScratchRepo': lambda: _Ctx(s)}
    f = flow.Flow(out=out.append, g=g)
    s.ensure = lambda *a: None
    assert f.merge_work(447, h['y'], 'y-v2') == 1
    assert out[0].startswith('refused: y-v2') and 'work is unchanged' in out[0]
    assert run(s.dir, 'ls-remote', str(origin), 'refs/heads/work').split()[0] == h['x']
    out.clear()
    assert f.merge_work(447, h['z'], 'z') == 0 and out[0].startswith('pushed work')
    assert run(s.dir, 'ls-remote', str(origin), 'refs/heads/work').split()[0] != h['x']


class _Ctx:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


# --- the step with a fake hr-gate -------------------------------------------------

def card(n, col, head=None, rank=1, draft=False):
    """rank 0 (a priority milestone) is the soonest due date in the shared pick order (#35)."""
    due = dict(due='2026-11-15', milestone='ptw 15') if rank == board.MILESTONE else {}
    return dict(number=n, state='OPEN', draft=draft, branch=f'b{n}', head=head or f'{n:040d}', title='t',
                col=col, item=f'I{n}', rank=rank, parked=False, body='', **due)


class FakeG(dict):
    def __init__(self, prs, clean=lambda a, b: True, rows=None):
        super().__init__()
        self.prs, self.moves, self.rows = prs, [], rows or []

        class Sc:
            dir, main = '/x', 'main'

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def ensure_main(s):
                return 'main'

            def ensure(s, *a):
                pass
        self.update(ScratchRepo=Sc, merge_tree_clean=lambda d, a, b: clean(a, b),
                    move=lambda item, col: self.moves.append((item, col)), origin_url=lambda: 'origin',
                    queue_from_board_or_file=lambda: [], fetch=lambda n, refresh=False: {'number': n},
                    list_hr_prs=lambda refresh=False: [], evaluate=lambda pr, **k: (self.rows, None),
                    gql=None, PARKED=set())


def make_flow(prs, dry=True, **kw):
    g = FakeG(prs, **kw)
    f = flow.Flow(dry_run=dry, out=lambda line: None, g=g, now=lambda: 10_000)
    cols = {}
    for p in prs.values():
        cols.setdefault(p['col'], []).append(p)
    f.columns = lambda: (prs, cols)
    return f, g


def test_promote_when_human_review_drops_to_one(monkeypatch):
    prs = {1: card(1, HR), 2: card(2, ST), 3: card(3, ST), 4: card(4, ST)}
    f, _ = make_flow(prs)
    f.step()
    assert f.did[0].startswith('would: pending-promote --apply (Human Review 1/2, Staging 3)')


def test_full_staging_pulls_nothing_and_touches_no_ready_pr(monkeypatch):
    prs = {1: card(1, HR), 2: card(2, HR), 3: card(3, ST), 4: card(4, ST), 5: card(5, ST), 6: card(6, RD)}
    f, g = make_flow(prs)
    monkeypatch.setattr(flow, 'stack_conflict', lambda *a: pytest.fail('no check on a Ready PR'))
    f.step()
    assert f.did == [] and g.moves == []


def premerged(monkeypatch, f, tips):
    """Stub the pre-merge: tips {PR: tip or None}; returns the PRs asked, in order."""
    asked = []
    monkeypatch.setattr(f, 'future_main', lambda scratch, cols: ('key', 'fm', None))
    monkeypatch.setattr(f, 'premerge_one', lambda p, scratch, fm: asked.append(p['number']) or tips.get(p['number']))
    return asked


def test_pull_takes_only_the_next_ready_pr_in_the_pick_order(monkeypatch):
    """tangentswarm #65 (Codex on PR #66: swarm-flow step too): #8 is next and not merge-ready
    (it conflicts with the queue: its worker resolves its -vN), so #7 does not jump it."""
    prs = {1: card(1, HR), 2: card(2, HR), 3: card(3, ST), 7: card(7, RD), 8: card(8, RD, rank=0), 9: card(9, RD)}
    f, g = make_flow(prs)
    asked = premerged(monkeypatch, f, {7: 'v7', 9: 'v9'})
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s, tip=None, pre_merged=False, fm=None: started.append(p['number']))
    f.step()
    assert started == [] and asked == [8]
    assert '#8 is next in the pick order and is not merge-ready: nothing behind it is pulled' in f.notes

def test_pr_that_does_not_merge_onto_main_goes_back_with_a_relay(monkeypatch, tmp_path):
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD, rank=0), 9: card(9, RD)}
    f, g = make_flow(prs, dry=False, clean=lambda a, b: not (a == 'main' and b == prs[7]['head']))
    premerged(monkeypatch, f, {9: 'v9'})
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s, tip=None, pre_merged=False, fm=None: started.append((p['number'], tip, pre_merged)))
    f.step()
    assert g.moves == [('I7', IP)] and started == [(9, 'v9', True)]
    assert 'merge origin/main' in (flow.RELAY_DIR / 'relay-7-merge-main.md').read_text()

def test_empty_ready_is_a_refill_note():
    f, _ = make_flow({1: card(1, HR), 2: card(2, HR)})
    f.step()
    assert any('Ready is empty' in n for n in f.notes)


def pull_state(n=7, head=None, started=10_000):
    flow.save_pull(dict(pr=n, head=head or f'{n:040d}', branch=f'b{n}', item=f'I{n}', work='w', started=started))


def test_advance_waits_for_ci_and_screenshots():
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('7a screenshots current', False, 'no image in body')])
    f.step()
    assert flow.load_pull()['pr'] == 7 and any('#7 pull waiting' in n for n in f.notes) and g.moves == []


@pytest.fixture
def tick_state(tmp_path, monkeypatch):
    from tangentswarm import tick
    monkeypatch.setattr(tick, 'STATE_DIR', tmp_path / 'tick')
    return tmp_path / 'tick'


def told(monkeypatch):
    """Record each text a worker gets (slots.revive)."""
    from tangentswarm import slots
    sent = []
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: sent.append((n, text)) or out('sent') or 0)
    return sent


def test_advance_timeout_goes_back_to_ready_not_in_progress(monkeypatch, tick_state):
    """tangentswarm #83: a timeout is not a conflict: the card stays in Ready."""
    pull_state(started=10_000 - flow.PULL_TIMEOUT - 60)
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('5 CI green (Linux)', False, 'Linux QUEUED')])
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and flow.load_pull() is None
    assert '#7 left the Staging pull, no conflict; the card stays in Ready' in f.did[0]


def test_advance_hard_failure_with_no_conflict_stays_in_ready(monkeypatch, tick_state):
    """tangentswarm #83 (platform #447): condition 8 failed at the gate, no conflict. The card
    went to In Progress and its Claude found nothing to fix. Now it stays in Ready, it is not
    held (it keeps its queue place), and its worker gets the reason once per head."""
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    rows = [('2 mergeable onto main', True, 'clean onto origin/main (GitHub vs base: MERGEABLE)'),
            ('8 no later human request', False, '1 later comment')]
    f, g = make_flow(prs, dry=False, rows=rows)
    sent = told(monkeypatch)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and flow.load_pull() is None and 7 not in flow.held()
    assert '8 no later human request' in f.did[0] and 'stays in Ready' in f.did[0]
    assert [n for n, _ in sent] == [7] and 'no conflict' in sent[0][1] and 'merge origin/main' not in sent[0][1]
    pull_state()  # the same head fails again: no second relay
    f.step()
    assert g.moves == [] and len(sent) == 1


def test_advance_no_conflict_with_no_worker_still_stays_in_ready(monkeypatch, tick_state):
    from tangentswarm import slots
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('7b where-to-see line', False, 'missing'),
                                           ('8 no later human request', False, '1 later comment')])
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: out('no slot') or 3)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and flow.load_pull() is None and 7 not in flow.held()
    assert any('no worker took the reason' in n for n in f.notes)


def test_advance_conflict_with_main_goes_to_in_progress_with_merge_main(monkeypatch, tick_state):
    """tangentswarm #83: only a real conflict at the final mergeability check is rework."""
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('2 mergeable onto main', False,
                                            'CONFLICTS with origin/main (GitHub vs base: MERGEABLE)')])
    sent = told(monkeypatch)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [('I7', IP)] and flow.load_pull() is None
    assert 'git merge origin/main' in sent[0][1]
    assert 'git merge origin/main' in (flow.RELAY_DIR / 'relay-7-merge-main.md').read_text()


def test_advance_github_conflicting_is_a_conflict(monkeypatch, tick_state):
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('2 mergeable onto main', False, 'GitHub mergeable is CONFLICTING'),
                                           ('8 no later human request', False, '1 later comment')])
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [('I7', IP)] and '8 no later' not in f.did[0]


def test_gate_conflicts_only_row_2_conflicts():
    assert flow.gate_conflicts([('2 mergeable onto main', False, 'CONFLICTS with origin/main')]) == [
        '2 mergeable onto main: CONFLICTS with origin/main']
    assert flow.gate_conflicts([('2 mergeable onto main', False, 'cannot check merge: no scratch repo')]) == []
    assert flow.gate_conflicts([('2 mergeable onto main', True, 'clean onto origin/main')]) == []
    assert flow.gate_conflicts([('8 no later human request', False, 'CONFLICT words in a comment')]) == []
    assert flow.gate_conflicts([('9 stack-clean', False, 'conflicts with #5 in Human Review')]) == []


def test_advance_pass_runs_hr_gate_apply(monkeypatch):
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('1 not draft', True, 'ready')])
    calls = []
    monkeypatch.setattr(f, 'tool', lambda argv: calls.append([str(a) for a in argv]) or (0, '  moved #7 to Pending'))
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert calls[0][-2:] == ['--apply', '7'] and flow.load_pull() is None


def test_advance_head_moved_drops_the_pull_and_moves_no_card(monkeypatch, tick_state):
    """tangentswarm #83: a new head is not a conflict. The pull ends; the card is not sent to
    In Progress (the webhook moves a new head to Auto Review)."""
    pull_state(head='f' * 40)
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False)
    sent = told(monkeypatch)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and flow.load_pull() is None and sent == []
    assert 'head moved' in f.did[0] and 'no conflict' in f.did[0]


def test_advance_head_moved_with_the_card_in_auto_review_is_not_moved(monkeypatch, tick_state):
    pull_state(head='f' * 40)
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, AR)}
    f, g = make_flow(prs, dry=False)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and 'is in ' in f.did[0] and 'not moved' in f.did[0]


def test_advance_dry_run_says_it_would_leave(monkeypatch, tick_state):
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=True, rows=[('8 no later human request', False, '1 later comment')])
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [] and flow.load_pull()['pr'] == 7 and 'would leave the Staging pull' in f.did[0]


def test_advance_finished_when_card_reached_staging():
    pull_state()
    f, g = make_flow({1: card(1, HR), 2: card(2, HR), 7: card(7, ST), 3: card(3, ST), 4: card(4, ST)}, dry=False)
    f.step()
    assert flow.load_pull() is None and 'pull finished' in f.did[0]


def test_main_busy_exits_75(monkeypatch):
    class B:
        def __enter__(self):
            raise flow.gatelock.Busy('hr-gate --apply 3 pid 1')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(flow.gatelock, 'gate_lock', lambda name: B())
    assert flow.main(['step']) == flow.gatelock.EXIT_BUSY


def test_fast_track_goes_first_in_the_pick_order(monkeypatch):
    monkeypatch.setattr(flow, 'STAGING_MIN', 3)
    prs = {1: card(1, HR), 2: card(2, HR), 3: card(3, ST), 4: card(4, ST),
           7: card(7, RD, rank=0), 8: card(8, RD, rank=board.FAST_TRACK)}
    f, g = make_flow(prs)
    asked = premerged(monkeypatch, f, {7: 'v7', 8: 'v8'})
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s, tip=None, pre_merged=False, fm=None: started.append((p['number'], tip)))
    f.step()
    assert started == [(8, 'v8')] and asked == [8]

def test_step_without_promote_never_runs_pending_promote(monkeypatch):
    calls = []

    class S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    f = flow.Flow(dry_run=True, out=lambda line: None, g={'ScratchRepo': S})
    monkeypatch.setattr(f, 'columns', lambda: ({}, {board.HUMAN_REVIEW: []}))
    monkeypatch.setattr(f, 'promote', lambda cols: calls.append('promote'))
    monkeypatch.setattr(f, 'pull', lambda prs, cols, scratch: calls.append('pull'))
    f.step(promote=False)
    f.step()
    assert calls == ['pull', 'promote', 'pull']


def test_merge_work_says_when_the_tip_is_already_in_work(repo, tmp_path):
    s, h = repo
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', str(origin))
    run(s.dir, 'push', '-q', str(origin), f"{h['x']}:refs/heads/work")
    out = []
    s.ensure = lambda *a: None
    f = flow.Flow(out=out.append, g={'origin_url': lambda: str(origin), 'ScratchRepo': lambda: _Ctx(s)})
    assert f.merge_work(447, h['main'], 'main') == 0 and 'already in work' in out[0]


def test_strict_pull_takes_only_the_next_pr_and_only_when_it_is_merge_ready(monkeypatch):
    """swarm-advance --cascade, tangentswarm #65: the next PR in the pick order is pulled with
    its pre-merged tip; when it is not merge-ready, nothing behind it is pulled. Nothing is
    moved and no relay is written by the pull."""
    prs = {1: card(1, HR), 3: card(3, ST), 6: card(6, RD, rank=0), 9: card(9, RD, rank=board.FAST_TRACK)}
    f, g = make_flow(prs, dry=False)
    ready = {9: None, 6: 'v6' * 20}
    seen = []
    monkeypatch.setattr(f, 'future_main', lambda scratch, cols: ('key', 'fm', None))
    monkeypatch.setattr(f, 'premerge_one', lambda p, scratch, fm: seen.append(p['number']) or ready[p['number']])
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s, tip=None, pre_merged=False, fm=None: started.append((p['number'], tip, pre_merged)) or True)
    _, cols = f.columns()
    assert f.pull(prs, cols, g['ScratchRepo'](), limit=5, strict=True) is None
    assert seen == [9] and started == [] and g.moves == []
    assert '#9 is next in the pick order and is not merge-ready: nothing behind it is pulled' in f.notes
    ready[9] = 'v9' * 20
    assert f.pull(prs, cols, g['ScratchRepo'](), limit=5, strict=True)['number'] == 9
    assert started == [(9, 'v9' * 20, True)]


def test_pull_batch_merges_the_pre_merged_tips_into_work_in_one_push(repo, tmp_path):
    """Staging stocking: one work push for the whole batch, so one staging redeploy covers it.
    tangentswarm #65: each tip is a <branch>-v1 = future main (main + #1 in Human Review) + the
    head. x and y each pre-merge cleanly but not with each other: x enters the batch, y stops
    it, and z behind y is not pulled (strict order)."""
    s, h = repo
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', str(origin))
    run(s.dir, 'push', '-q', str(origin), f"{h['hr1']}:refs/heads/work")
    s.main = h['main']
    s.ensure_main = s.ensure = lambda *a: None
    g = {'origin_url': lambda: str(origin), 'merge_tree_clean': lambda d, a, b: True,
         'queue_from_board_or_file': lambda: []}
    f = flow.Flow(out=lambda line: None, g=g, now=lambda: 10_000)
    prs = {n: card(n, RD, head=h[k]) for n, k in ((7, 'x'), (8, 'y'), (9, 'z'))}
    cols = {HR: [card(1, HR, head=h['hr1'])], RD: [prs[7], prs[8], prs[9]]}
    batch = f.pull_batch(prs, cols, s, room=5)
    assert [p['number'] for p in batch] == [7]
    assert any(n.startswith('#8 is next in the pick order and its tip') and 'nothing behind it is pulled' in n
               for n in f.notes)
    v7 = run(s.dir, 'ls-remote', str(origin), 'refs/heads/b7-v1').split()[0]
    tip = run(s.dir, 'ls-remote', str(origin), 'refs/heads/work').split()[0]
    assert tip == f.last_work
    for c in (h['x'], h['hr1'], v7):
        assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', c, tip]).returncode == 0
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', h['z'], tip]).returncode != 0
    assert [(d['pr'], d['tip'], d['work']) for d in flow.load_pulls()] == [(7, v7, tip)]
    assert f.did[-1].startswith(f"did: #7 {v7[:8]} pulled: 1 pre-merged tip(s) in work") \
        and 'in one push' in f.did[-1]
    assert run(s.dir, 'rev-parse', f'{tip}^{{tree}}') == run(s.dir, 'rev-parse', f'{v7}^{{tree}}')
    assert run(s.dir, 'ls-remote', str(origin), 'refs/heads/b7') == ''  # the PR's own branch is never pushed


def test_pull_batch_dry_run_pushes_nothing_and_saves_nothing(repo, tmp_path):
    s, h = repo
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', str(origin))
    run(s.dir, 'push', '-q', str(origin), f"{h['hr1']}:refs/heads/work")
    s.main = h['main']
    s.ensure_main = s.ensure = lambda *a: None
    g = {'origin_url': lambda: str(origin), 'merge_tree_clean': lambda d, a, b: True,
         'queue_from_board_or_file': lambda: []}
    f = flow.Flow(dry_run=True, out=lambda line: None, g=g, now=lambda: 10_000)
    prs = {9: card(9, RD, head=h['z'])}
    assert [p['number'] for p in f.pull_batch(prs, {RD: [prs[9]]}, s, room=5)] == [9]
    assert run(s.dir, 'ls-remote', str(origin), 'refs/heads/work').split()[0] == h['hr1']
    assert flow.load_pulls() == []


def test_pulls_state_reads_the_old_single_pull_file_and_drops_one_pr(tmp_path):
    flow.STATE.write_text('{"pr": 495, "head": "4e", "work": "5b", "asked": true}')
    assert [d['pr'] for d in flow.load_pulls()] == [495] and flow.pulled_prs() == {495}
    flow.save_pull(dict(pr=514, head='f0', work='5b'))
    assert flow.pulled_prs() == {495, 514} and flow.load_pull()['pr'] == 495
    flow.drop_pull(495)
    assert flow.pulled_prs() == {514}
    flow.drop_pull(514)
    assert not flow.STATE.exists()


def test_a_pull_waiting_only_on_screenshots_is_never_failed_and_gets_the_job(monkeypatch):
    """#519 at 2:23 AM ET: a 7a-only wait failed after PULL_TIMEOUT into In Progress with no worker."""
    from tangentswarm import slots
    prs = {7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('7a screenshots current', False, 'no image in body')])
    sent = []
    monkeypatch.setattr(slots, 'revive', lambda n, text, out=print, **k: sent.append(n) or out('sent') or 0)
    pull_state(7, started=10_000 - flow.PULL_TIMEOUT - 60)
    f.advance(flow.load_pull(), prs, g['ScratchRepo']())
    assert g.moves == [] and sent == [7]
    assert any(n.startswith('SCREENSHOTS_NEEDED #7: still no screenshots after') for n in f.notes)
    assert flow.load_pull()['pr'] == 7  # the pull stays open


def test_needs_screenshots_exempts_api_docs_and_tooling():
    from tangentswarm.gatechecks import needs_screenshots as ns
    assert ns(['web/src/a.tsx'])
    assert ns(['app/view.ts', 'api/x.py'])
    assert not ns(['api/routes/x.ts', 'api/models.py'])
    assert not ns(['docs/a.md', 'AGENTS.md'])
    assert not ns(['.github/workflows/ci.yml', 'scripts/x.ts', 'tests/a.test.ts'])
    assert ns(['web/src/a.tsx'], body='This is code-only.')  # web/ files still need shots


def test_a_held_ready_head_is_handed_off_again_when_a_worker_can_take_it(tmp_path, monkeypatch):
    """Codex on PR #26: a gate failure that found no worker held the head for good."""
    from tangentswarm import board, flow as F
    rel = tmp_path / 'relay-7-gate.md'
    rel.write_text('Coordinator: #7 fix the gate\n')
    F.hold(7, 'h' * 40, 'a human comment after the head', relay=rel, tried=0)
    moves, rcs = [], [3, 0]
    f = F.Flow(dry_run=False, out=lambda l: None, g={'OPT': {}, 'project_id': lambda: 'P', 'STATUS_FIELD': 'S'})
    f.give_worker = lambda n, text: (rcs.pop(0), 'x')
    monkeypatch.setattr(board, 'move_card', lambda g, item, col, sh=None, n=None, **k: moves.append((item, col)))
    cols = {board.READY: [dict(number=7, head='h' * 40, item='I7')]}
    f.retry_held(cols, now=10_000)
    assert moves == [] and F.held()[7]['tried'] == 10_000
    f.retry_held(cols, now=10_100)  # inside HELD_RETRY: not tried
    assert rcs == [0]
    f.retry_held(cols, now=10_000 + F.HELD_RETRY)
    assert moves == [('I7', board.IN_PROGRESS)] and 7 not in F.held()


def test_shots_relay_tells_the_worker_to_use_a_throwaway_user_or_scratch_root():
    """#64: the screenshot relay sends a worker to staging, so it carries the scratch rule."""
    from tangentswarm.gatechecks import SCRATCH_RULE
    text = flow.shots_text(7)
    assert SCRATCH_RULE in text and 'staging' in text and text.isascii()
