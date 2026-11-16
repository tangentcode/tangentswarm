"""tangentswarm #65 (Michal 2026-11-16 3:17, 3:22 and 3:24 PM ET): one shared pick order, strict
order with no queue-jumping, fixed WIP targets, pre-merge onto future main as <branch>-vN, the
pre-merging and merge-ready labels, and a worker only for a real conflict."""
import subprocess

import pytest

from tangentswarm import board, flow, slots

RD, ST, HR, IP = board.READY, board.STAGING, board.HUMAN_REVIEW, board.IN_PROGRESS


# --- (a) the shared pick order --------------------------------------------------

def test_fast_track_labels_sort_first_by_number_then_due_date_then_number():
    """Two fast-track PRs, neither active (one by its own label, one by an issue it closes),
    sort before the soonest-milestone PR, lowest number first."""
    items = [dict(number=30, due='2026-11-20', milestone='soon'),
             dict(number=41, labels=['Fast-Track']),
             dict(number=35, closes=[dict(number=7, labels=['fast-track'])]),
             dict(number=20, milestone='undated'),
             dict(number=10),
             dict(number=25, due='2026-12-01', milestone='later'),
             dict(number=26, due='2026-11-20', milestone='soon')]
    assert [p['number'] for p in sorted(items, key=board.pick_key)] == [35, 41, 26, 30, 25, 20, 10]
    assert [p['number'] for p in flow.ready_order(items, None, None)] == [35, 41, 26, 30, 25, 20, 10]


def test_the_active_ticket_and_the_label_regex_count_as_fast_track():
    assert board.item_fast_track(dict(number=9, closes=[488]), active=488)
    assert board.item_fast_track(dict(number=9, labels=['high priority']))
    assert not board.item_fast_track(dict(number=9, labels=['fast-tracked-later']))


# --- a real repo: main, a Human Review head, and Ready heads ----------------------

class Scratch:
    def __init__(self, path):
        self.dir, self.main = str(path), None

    def ensure_main(self):
        return self.main

    def ensure(self, *a):
        pass


def run(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    w = tmp_path / 'w'
    w.mkdir()
    run(w, 'init', '-q', '-b', 'main')
    run(w, 'config', 'user.email', 't@example.com')
    run(w, 'config', 'user.name', 't')
    (w / 'a.txt').write_text('1\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'base')
    main = run(w, 'rev-parse', 'HEAD')

    def branch(name, path, text, base=main):
        run(w, 'checkout', '-q', '-b', name, base)
        (w / path).write_text(text)
        run(w, 'add', '.')
        run(w, 'commit', '-qm', name)
        return run(w, 'rev-parse', 'HEAD')
    h = dict(main=main, hr1=branch('hr1', 'b.txt', 'hr1\n'), c=branch('c', 'b.txt', 'C\n'),
             z=branch('z', 'z.txt', 'z\n'), st2=branch('st2', 's.txt', 's\n'))
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', str(origin))
    run(w, 'push', '-q', str(origin), f"{h['hr1']}:refs/heads/work", f"{main}:refs/heads/main")
    s = Scratch(w / '.git')
    s.main = main
    return w, s, h, origin


def card(n, col, head, labels=()):
    return dict(number=n, state='OPEN', draft=False, branch=f'b{n}', head=head, title='t', col=col, item=f'I{n}',
                rank=1, parked=False, body='', labels=list(labels), own_labels=list(labels))


def make(world, dry=False, rcs=None):
    w, s, h, origin = world
    g = {'origin_url': lambda: str(origin), 'merge_tree_clean': lambda d, a, b: True}
    f = flow.Flow(dry_run=dry, out=lambda line: None, g=g, now=lambda: 10_000)
    sent = []

    def give(n, text, priority=False):
        sent.append((n, text, priority))
        return (rcs.pop(0) if rcs else 0), 'sent to its Claude'
    f.give_worker = give
    return f, sent


def remote(s, origin, ref):
    out = run(s.dir, 'ls-remote', str(origin), f'refs/heads/{ref}')
    return out.split()[0] if out else None


def resolve_on(w, origin, vn, head):
    """The worker: check out the -vN (future main), merge the PR head, resolve, push the -vN."""
    run(w, 'fetch', '-q', str(origin), f'+refs/heads/{vn}:refs/heads/{vn}')
    run(w, 'checkout', '-q', vn)
    subprocess.run(['git', 'merge', '-q', '--no-edit', head], cwd=w, capture_output=True)
    (w / 'b.txt').write_text('hr1\nC\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'resolve')
    run(w, 'push', '-q', str(origin), f'{vn}:refs/heads/{vn}')
    return run(w, 'rev-parse', 'HEAD')


def test_a_conflicting_next_pr_blocks_the_pull_until_its_worker_resolves_the_vn(world):
    """Strict order: #7 conflicts with future main (main + #1 in Human Review), so #9 behind it
    is not pulled. #7's worker gets ONE relay to resolve it on b7-v1 (= future main), never on
    b7. Once the worker pushes b7-v1 with the head merged in, #7 is merge-ready and both go."""
    w, s, h, origin = world
    f, sent = make(world)
    p7, p9 = card(7, RD, h['c']), card(9, RD, h['z'])
    cols = {HR: [card(1, HR, h['hr1'], ['merge-ready'])], RD: [p7, p9]}
    assert f.pull_batch({}, cols, s, room=2) == []
    assert any(n == '#7 is next in the pick order and is not merge-ready: nothing behind it is pulled' for n in f.notes)
    fm = remote(s, origin, 'b7-v1')
    assert fm and remote(s, origin, 'b7') is None  # the PR's own branch is never touched
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', h['hr1'], fm]).returncode == 0
    assert len(sent) == 1 and sent[0][0] == 7 and sent[0][2] is True
    assert 'b7-v1 ONLY' in sent[0][1] and 'Never merge main, work or staging into b7' in sent[0][1]
    assert (7, 'add', ['pre-merging']) in flow.LABELS_WRITTEN
    assert p7['own_labels'] == ['pre-merging']
    # the next cascade at the same head and future main: no second relay
    f.premerge(cols, s)
    assert len(sent) == 1
    # the worker resolves it on b7-v1
    tip = resolve_on(w, origin, 'b7-v1', h['c'])
    f.premerge(cols, s)
    assert (7, 'add', ['merge-ready']) in flow.LABELS_WRITTEN and (7, 'remove', ['pre-merging']) in flow.LABELS_WRITTEN
    batch = f.pull_batch({}, cols, s, room=2)
    assert [p['number'] for p in batch] == [7, 9]
    assert flow.load_pulls()[0]['tip'] == tip
    assert remote(s, origin, 'b9-v1')  # #9 was pre-merged mechanically, no worker
    assert len(sent) == 1


def test_a_clean_pr_is_pre_merged_mechanically_and_rebuilt_as_v2_when_future_main_changes(world):
    w, s, h, origin = world
    f, sent = make(world)
    p9 = card(9, RD, h['z'], ['pre-merging'])
    cols = {HR: [card(1, HR, h['hr1'])], RD: [p9]}
    tips = f.premerge(cols, s)
    v1 = remote(s, origin, 'b9-v1')
    assert tips == {9: v1} and sent == []
    assert (9, 'add', ['merge-ready']) in flow.LABELS_WRITTEN and (9, 'remove', ['pre-merging']) in flow.LABELS_WRITTEN
    assert (1, 'add', ['merge-ready']) in flow.LABELS_WRITTEN  # Human Review must carry merge-ready
    f.premerge(cols, s)
    assert remote(s, origin, 'b9-v2') is None  # same future main: no rebuild
    cols[ST] = [card(2, ST, h['st2'], ['merge-ready'])]  # the queue changed: future main changed
    f2, _ = make(world)
    tips = f2.premerge(cols, s)
    v2 = remote(s, origin, 'b9-v2')
    assert v2 and tips == {9: v2}
    for c in (h['hr1'], h['st2'], h['z']):
        assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', c, v2]).returncode == 0


def test_only_the_top_ready_prs_are_pre_merged(world, monkeypatch):
    w, s, h, origin = world
    monkeypatch.setattr(flow, 'PREMERGE_AHEAD', 1)
    f, _ = make(world)
    cols = {RD: [card(9, RD, h['z']), card(8, RD, h['st2'])]}
    assert list(f.premerge(cols, s)) == [8]
    assert remote(s, origin, 'b9-v1') is None


def test_a_pr_sent_back_loses_the_pre_merge_labels(world):
    w, s, h, origin = world
    f, _ = make(world)
    f.premerge({IP: [card(5, IP, h['z'], ['merge-ready', 'pre-merging'])]}, s)
    assert (5, 'remove', ['merge-ready', 'pre-merging']) in flow.LABELS_WRITTEN


def test_no_worker_gives_needs_worker_and_the_next_pass_tries_again(world):
    w, s, h, origin = world
    f, sent = make(world, rcs=[3, 0])
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(7, RD, h['c'])]}
    f.premerge(cols, s)
    assert len(sent) == 1
    assert any(n.startswith('NEEDS_WORKER #7: its pre-merge on b7-v1 conflicts; no worker took the relay')
               for n in f.notes)
    assert flow.load_premerge()['7']['relayed'] == ''
    f.premerge(cols, s)
    assert len(sent) == 2 and flow.load_premerge()['7']['relayed'] == 'b7-v1'
    assert remote(s, origin, 'b7-v2') is None  # the same -vN: nothing changed


def test_a_dry_run_pushes_nothing_and_writes_no_label(world):
    w, s, h, origin = world
    f, sent = make(world, dry=True)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(7, RD, h['c']), card(9, RD, h['z'])]}
    f.premerge(cols, s)
    assert remote(s, origin, 'b7-v1') is None and remote(s, origin, 'b9-v1') is None
    assert flow.LABELS_WRITTEN == [] and sent == [] and flow.load_premerge() == {}
    assert any('would add label pre-merging' in d for d in f.did)


# --- the hard limits: 9 live Claudes and MemAvailable -----------------------------

def test_conflict_capacity_is_nine_claudes_and_1_5_gb():
    assert slots.conflict_capacity(load=8, avail=2 * 1024 ** 3) == (True, '')
    ok, why = slots.conflict_capacity(load=9, avail=8 * 1024 ** 3)
    assert not ok and 'cap 9' in why
    ok, why = slots.conflict_capacity(load=1, avail=1024 ** 3)
    assert not ok and 'MemAvailable 1024 MB' in why
    assert slots.mem_available(lambda: 'MemTotal: 10 kB\nMemAvailable:    2048 kB\n') == 2048 * 1024


def test_a_priority_revive_waits_for_no_rework_slot_but_holds_at_the_limit(tmp_path, monkeypatch):
    """A conflict fix for the next PR does not wait for a rework slot; the Claude cap or the
    RAM limit gives NEEDS_WORKER (rc 3) and no launch."""
    import contextlib

    class Refused(Exception):
        pass

    class SP:
        RESUME_PARK_NEED_MB = RESUME_MIN_MB = 1
        MB = 1
        SLICE = 's'

        def resolve(self, host, key):
            return 'mn-7-b7', str(tmp_path), 'b7', None

        @contextlib.contextmanager
        def flocked(self, path, blocking=True):
            yield True

        def resume_lock(self, w):
            return w

        def live_pane(self, host, w):
            return None

        def scoped_command(self, w, resume=False):
            return ['claude'], 'unit'

        def launch_line(self, argv):
            return ' '.join(argv)

        def shell_for(self, host, w, wt, existing):
            pytest.fail('no launch over the limit')
    SP.Refused = Refused
    monkeypatch.setattr(slots, '_git', lambda wt, *a: 'b7')
    monkeypatch.setattr(slots, 'latest_session', lambda wt: 'S')
    path = tmp_path / 'slots.json'
    with slots.update(path) as d:  # every rework slot is in use
        for k in ('1', '2', '3'):
            d['rework'][k] = dict(worktree=f'/w{k}')
    claudes = [dict(cwd=f'/w{k}', pid=k) for k in (1, 2, 3)]
    lines = []
    rc = slots._revive(7, 'fix it', out=lines.append, sp=SP(), host=object(), claudes=claudes, path=path,
                       refuse=lambda wt: None, priority=True, capacity=lambda: (False, '9 Claudes run (cap 9)'))
    assert rc == 3 and lines[-1] == 'held: 9 Claudes run (cap 9); #7 resumes on the next pass'
    lines.clear()
    rc = slots._revive(7, 'fix it', out=lines.append, sp=SP(), host=object(), claudes=claudes, path=path,
                       refuse=lambda wt: None)
    assert rc == 3 and 'rework slots are in use' in lines[-1]  # without priority it waits for a slot


def test_a_merge_to_main_removes_the_pre_merge_labels_and_record():
    from tangentswarm import webhook as W
    flow.save_premerge({'7': {'head': 'h'}, '9': {'head': 'g'}})
    ex = W.Executor.__new__(W.Executor)
    notes = []
    ex.note = notes.append
    ex.drop_premerge_labels(7)
    assert (7, 'remove', ['merge-ready']) in flow.LABELS_WRITTEN
    assert (7, 'remove', ['pre-merging']) in flow.LABELS_WRITTEN
    assert list(flow.load_premerge()) == ['9'] and notes == []


def test_future_main_takes_a_queued_pr_as_its_resolved_vn_tip(world):
    """#7 was resolved on b7-v1 and pulled into Staging: future main for the next Ready PR
    is built from that tip, not from #7's raw head (which conflicts with #1)."""
    w, s, h, origin = world
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(7, RD, h['c'])]}
    f.premerge(cols, s)
    tip = resolve_on(w, origin, 'b7-v1', h['c'])
    f.premerge(cols, s)
    assert flow.load_premerge()['7']['tip'] == tip
    cols = {HR: [card(1, HR, h['hr1'])], ST: [card(7, ST, h['c'])], RD: [card(9, RD, h['z'])]}
    f2, _ = make(world)
    key, fm, why = f2.future_main(s, cols)
    assert why is None and subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', tip, fm]).returncode == 0
    assert f2.premerge(cols, s)[9] == remote(s, origin, 'b9-v1')


# --- Codex on PR #66 -------------------------------------------------------------

def test_a_head_missing_from_the_scratch_repo_is_not_a_conflict(world):
    """A commit the scratch repo cannot fetch is never read as a conflict: no -vN, no label,
    no worker; the next pass tries again."""
    w, s, h, origin = world
    f, sent = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(7, RD, '1' * 40)]}
    assert f.premerge(cols, s) == {7: None}
    assert remote(s, origin, 'b7-v1') is None and sent == [] and flow.LABELS_WRITTEN == [(1, 'add', ['merge-ready'])]
    assert any(n.startswith('#7: head 11111111 is not in the scratch repo') for n in f.notes)
    key, fm, why = f.future_main(s, {HR: [card(1, HR, '2' * 40)]})
    assert fm is None and 'is not in the scratch repo' in why


def test_a_ready_record_whose_tip_is_gone_is_rebuilt_as_the_next_vn(world):
    w, s, h, origin = world
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z'])]}
    f.premerge(cols, s)
    recs = flow.load_premerge()
    recs['9']['tip'] = '3' * 40  # its tip cannot be fetched any more
    flow.save_premerge(recs)
    tips = f.premerge(cols, s)
    assert tips == {9: remote(s, origin, 'b9-v2')} and flow.load_premerge()['9']['vbranch'] == 'b9-v2'


def test_a_tip_that_conflicts_only_with_stale_work_gives_work_the_future_main_tree(world):
    """work holds a PR that was pulled and then sent back: the next tip merges cleanly onto
    future main but not onto work. work becomes future main + the PR in a fast-forward push
    (no force, no worker), instead of blocking the queue forever."""
    w, s, h, origin = world
    run(w, 'checkout', '-q', '-b', 'stale', h['hr1'])
    (w / 'z.txt').write_text('stale\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'a PR that was sent back')
    old = run(w, 'rev-parse', 'HEAD')
    run(w, 'push', '-q', str(origin), f'{old}:refs/heads/work')
    f, sent = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z']), card(11, RD, h['st2'])]}
    batch = f.pull_batch({}, cols, s, room=2)
    assert [p['number'] for p in batch] == [9, 11] and sent == []
    new = remote(s, origin, 'work')
    v9 = remote(s, origin, 'b9-v1')
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', old, new]).returncode == 0
    assert run(w, 'show', f'{new}:z.txt') == 'z' and run(w, 'show', f'{new}:s.txt') == 's'
    assert run(w, 'show', f'{new}:z.txt') == 'z'  # the sent-back PR's stale z.txt is gone
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', v9, new]).returncode == 0


def test_a_pr_pulled_sent_back_and_pulled_again_gets_all_its_content_back(world):
    """Codex on PR #66: work kept the sent-back PR's commits as ancestors; when it came back,
    a merge treated its old changes as already there. work's tree is future main + the batch."""
    w, s, h, origin = world
    run(w, 'checkout', '-q', '-b', 'a', h['main'])
    (w / 'q.txt').write_text('q\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'PR A')
    a = run(w, 'rev-parse', 'HEAD')
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(5, RD, a)]}
    assert [p['number'] for p in f.pull_batch({}, cols, s, room=1)] == [5]
    flow.save_pulls([])  # #5 is sent back: not in the queue any more
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z'])]}
    f.pull_batch({}, cols, s, room=1)
    assert subprocess.run(['git', '--git-dir', s.dir, 'cat-file', '-e', f"{remote(s, origin, 'work')}:q.txt"]).returncode != 0
    flow.save_pulls([])
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1']), card(9, HR, h['z'])], RD: [card(5, RD, a)]}
    assert [p['number'] for p in f.pull_batch({}, cols, s, room=1)] == [5]
    work = remote(s, origin, 'work')
    assert run(w, 'show', f'{work}:q.txt') == 'q' and run(w, 'show', f'{work}:z.txt') == 'z'


def test_a_queued_tip_built_on_a_pr_that_left_the_queue_is_not_used(world):
    """Codex on PR #66: B was pre-merged onto future main with A; A is sent back while B
    stays in Staging. Future main must not carry A in through B's old tip."""
    w, s, h, origin = world
    run(w, 'checkout', '-q', '-b', 'a', h['main'])
    (w / 'q.txt').write_text('q\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'PR A')
    a = run(w, 'rev-parse', 'HEAD')
    f, _ = make(world)
    tips = f.premerge({ST: [card(5, ST, a)], RD: [card(9, RD, h['z'])]}, s)
    assert subprocess.run(['git', '--git-dir', s.dir, 'cat-file', '-e', f'{tips[9]}:q.txt']).returncode == 0
    f, _ = make(world)
    key, fm, why = f.future_main(s, {ST: [card(9, ST, h['z'])]})  # A left; B (#9) is in Staging
    assert why is None
    assert subprocess.run(['git', '--git-dir', s.dir, 'cat-file', '-e', f'{fm}:q.txt']).returncode != 0
    assert run(w, 'show', f'{fm}:z.txt') == 'z'
    f, _ = make(world)  # with A still ahead of it, B's tip is used as is
    key, fm, why = f.future_main(s, {ST: [card(5, ST, a), card(9, ST, h['z'])]})
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', tips[9], fm]).returncode == 0
