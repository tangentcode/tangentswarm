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


def test_a_clean_pr_is_pre_merged_mechanically_and_kept_when_future_main_changes(world):
    """tangentswarm #69: an existing -vN that still merges cleanly onto a changed future main
    is kept: no new -vN, no new remote branch. A pull puts future main + that tip in work."""
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
    assert tips == {9: v1} and remote(s, origin, 'b9-v2') is None  # v1 still merges: kept
    assert flow.load_premerge()['9']['vbranch'] == 'b9-v1'
    assert any(d.startswith('did: #9 keeps b9-v1') for d in f2.did)
    f3, _ = make(world)
    f3.premerge(cols, s)
    assert not any('keeps' in d for d in f3.did)  # checked once per future main
    batch = f3.pull_batch({}, cols, s, room=1)
    assert [p['number'] for p in batch] == [9] and flow.load_pulls()[0]['tip'] == v1
    work = remote(s, origin, 'work')
    # work's tree = future main (with #2) + #9, not v1's tree alone
    assert [run(w, 'show', f'{work}:{x}') for x in ('b.txt', 's.txt', 'z.txt')] == ['hr1', 's', 'z']


def test_the_step_pull_of_a_kept_vn_puts_future_main_and_the_tip_in_work(world):
    """tangentswarm #69, the same class in swarm-flow step (Flow.pull): a kept -vN is built
    on an older future main, so work gets future main + the tip, never the tip's tree alone."""
    w, s, h, origin = world
    f, _ = make(world)
    f.premerge({HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z'])]}, s)
    v1 = remote(s, origin, 'b9-v1')
    f2, _ = make(world)
    f2.fail = lambda *a, **k: pytest.fail('no relay')
    cols = {HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}
    assert f2.pull({}, cols, s, limit=5)['number'] == 9
    assert flow.load_pulls()[0]['tip'] == v1 and remote(s, origin, 'b9-v2') is None
    work = remote(s, origin, 'work')
    assert [run(w, 'show', f'{work}:{x}') for x in ('b.txt', 's.txt', 'z.txt')] == ['hr1', 's', 'z']


def test_a_vn_built_on_a_pr_that_left_the_queue_is_rebuilt_as_v2(world):
    """The -vN carries #1 (Human Review); #1 is sent back, so the tip is not kept: a new -vN
    on the new future main, without #1."""
    w, s, h, origin = world
    f, _ = make(world)
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z'])]}
    v1 = f.premerge(cols, s)[9]
    f2, _ = make(world)
    tips = f2.premerge({ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}, s)
    v2 = remote(s, origin, 'b9-v2')
    assert v2 and tips == {9: v2} and v2 != v1
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', h['hr1'], v2]).returncode != 0
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', h['st2'], v2]).returncode == 0


def test_unchanged_pr_head_pairs_across_two_passes_build_no_new_vn(world):
    """tangentswarm #69: two passes with the same (PR, head) pairs, in any column order, build
    no new -vN."""
    w, s, h, origin = world
    cols = {HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}
    f, _ = make(world)
    v1 = f.premerge(cols, s)[9]
    cols2 = {HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}
    f2, _ = make(world)
    assert f2.premerge(cols2, s) == {9: v1}
    assert remote(s, origin, 'b9-v2') is None and not any('pre-merged onto' in d for d in f2.did)


def test_a_pulled_fast_track_pr_keeps_its_future_main_place_until_staging(world):
    """tangentswarm #69 (platform #518 -v4, -v5, -v6): a fast-track PR in flight sorts with
    Staging by pick_key, so its card reaching Staging does not change future main."""
    w, s, h, origin = world
    run(w, 'checkout', '-q', '-b', 'ft', h['main'])
    (w / 'f.txt').write_text('f\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'fast-track PR')
    ft = run(w, 'rev-parse', 'HEAD')
    pulled = card(15, RD, ft, ['fast-track'])
    cols = {HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [pulled, card(9, RD, h['z'])]}
    flow.save_pulls([flow.pull_record(pulled, tip=ft, work='w' * 40, started=0)])
    f, _ = make(world)
    before = f.future_main(s, cols)
    assert before[0].split()[1:] == [f"#1@{h['hr1'][:12]}", f"#15@{ft[:12]}", f"#2@{h['st2'][:12]}"]
    # the record alone (no card in the columns) gives the same place
    f, _ = make(world)
    assert f.future_main(s, {HR: cols[HR], ST: cols[ST], RD: [card(9, RD, h['z'])]})[0] == before[0]
    # the card lands in Staging and the pull ends
    flow.save_pulls([])
    f, _ = make(world)
    after = f.future_main(s, {HR: cols[HR], ST: [card(2, ST, h['st2']), card(15, ST, ft, ['fast-track'])],
                              RD: [card(9, RD, h['z'])]})
    assert after[0] == before[0] and after[1] is not None
    # and #9's -vN, built while #15 was in flight, is the one used after it reached Staging
    f, _ = make(world)
    flow.save_pulls([flow.pull_record(pulled, tip=ft, work='w' * 40, started=0)])
    v1 = f.premerge(cols, s)[9]
    flow.save_pulls([])
    f, _ = make(world)
    cols3 = {HR: cols[HR], ST: [card(2, ST, h['st2']), card(15, ST, ft, ['fast-track'])], RD: [card(9, RD, h['z'])]}
    assert f.premerge(cols3, s) == {9: v1} and remote(s, origin, 'b9-v2') is None
    assert not any('#9' in d for d in f.did if 'label' not in d)


def test_a_pre_merge_after_a_pull_in_the_same_pass_uses_the_queue_with_the_pull(world):
    """tangentswarm #69 (platform #518 -v4): the pass pulls #9; the pre-merge of #11 later in
    that pass is built on future main WITH #9, so the next pass keeps it."""
    w, s, h, origin = world
    run(w, 'checkout', '-q', '-b', 'e', h['main'])
    (w / 'e.txt').write_text('e\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'PR E')
    e = run(w, 'rev-parse', 'HEAD')
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z']), card(11, RD, e)]}
    f, _ = make(world)
    assert [p['number'] for p in f.pull_batch({}, cols, s, room=1)] == [9]
    tips = f.premerge(cols, s)
    assert list(tips) == [11]
    v11 = remote(s, origin, 'b11-v1')
    assert tips[11] == v11
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', h['z'], v11]).returncode == 0
    f2, _ = make(world)
    assert f2.premerge(cols, s) == {11: v11} and remote(s, origin, 'b11-v2') is None
    assert not any('keeps' in d or 'pre-merged onto' in d for d in f2.did)


def test_a_dry_run_pre_merge_after_a_dry_pull_counts_the_pull(world):
    """A dry run saves no pull; its pre-merge still sees the dry pull in future main."""
    w, s, h, origin = world
    cols = {HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z']), card(11, RD, h['st2'])]}
    f, _ = make(world, dry=True)
    assert [p['number'] for p in f.pull_batch({}, cols, s, room=1)] == [9]
    assert flow.load_pulls() == []
    f.premerge(cols, s)
    assert any(d.startswith('would: #11 pre-merged onto future main') and '#9@' in d for d in f.did)


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


def test_fast_tracked_items_sort_by_number_alone():
    """Codex on PR #66: fast-track #9 with a dated milestone went ahead of fast-track #7."""
    items = [dict(number=9, labels=['fast-track'], due='2026-11-17', milestone='soon'),
             dict(number=7, labels=['fast-track']), dict(number=3, due='2026-11-17', milestone='soon')]
    assert [p['number'] for p in sorted(items, key=board.pick_key)] == [7, 9, 3]
    assert slots.key_text(dict(number=9, labels=['fast-track'], due='2026-11-17', milestone='soon')) \
        == '#9=(ft:yes,due:2026-11-17 soon,n:9)'


def test_pre_merge_state_writes_are_locked_and_never_lose_another_record(tmp_path):
    """Codex on PR #66: the webhook's cleanup and the cascade's save raced on one file."""
    import threading
    flow.save_premerge({'7': {'head': 'h'}})
    with flow.premerge_update() as recs:
        done = []
        t = threading.Thread(target=lambda: done.append(flow.set_premerge(9, {'head': 'g'})))
        t.start()
        t.join(0.3)
        assert t.is_alive() and not done  # the second writer waits for the lock
        recs['8'] = {'head': 'x'}
    t.join(5)
    assert done == [True] and sorted(flow.load_premerge()) == ['7', '8', '9']
    assert flow.set_premerge(9, {'head': 'z'}, only_if=lambda old: old.get('head') == 'nope') is False
    flow.set_premerge(7, None)
    assert sorted(flow.load_premerge()) == ['8', '9'] and flow.load_premerge()['9'] == {'head': 'g'}
    assert not list(tmp_path.glob('premerge.json.*.tmp'))


def test_a_worker_tip_with_conflict_markers_is_never_merge_ready_or_pushed_to_work(world):
    """Codex on PR #66: a -vN tip with both parents and unresolved markers was accepted and its
    tree went to work. It stays pre-merging, the worker is told once, and work is refused."""
    w, s, h, origin = world
    f, sent = make(world)
    p7 = card(7, RD, h['c'])
    cols = {HR: [card(1, HR, h['hr1'])], RD: [p7]}
    f.premerge(cols, s)
    run(w, 'fetch', '-q', str(origin), '+refs/heads/b7-v1:refs/heads/b7-v1')
    run(w, 'checkout', '-q', 'b7-v1')
    subprocess.run(['git', 'merge', '-q', '--no-edit', h['c']], cwd=w, capture_output=True)
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'merge with the markers left in')
    bad = run(w, 'rev-parse', 'HEAD')
    run(w, 'push', '-q', str(origin), 'b7-v1:refs/heads/b7-v1')
    assert f.premerge(cols, s) == {7: None}
    assert len(sent) == 2 and 'still has conflict markers' in sent[1][1]
    f.premerge(cols, s)
    assert len(sent) == 2  # told once per bad tip
    assert f.pull_batch({}, cols, s, room=1) == [] and remote(s, origin, 'work') == h['hr1']
    (w / 'b.txt').write_text('hr1\nC\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'resolved')
    run(w, 'push', '-q', str(origin), 'b7-v1:refs/heads/b7-v1')
    f.premerge(cols, s)  # the next cascade pass
    assert [p['number'] for p in f.pull_batch({}, cols, s, room=1)] == [7]
    assert run(w, 'show', f"{remote(s, origin, 'work')}:b.txt") == 'hr1\nC'
    # a cached ready record whose tip has markers is not used either
    recs = flow.load_premerge()
    recs['7'].update(status='ready', tip=bad)
    flow.save_premerge(recs)
    flow.save_pulls([])  # #7 is back in Ready, not in flight
    f2, _ = make(world)
    good = remote(s, origin, 'b7-v1')
    assert f2.premerge(cols, s) == {7: good} and good != bad  # the bad cached tip is skipped
    assert f2.work_commit(s, h['hr1'], bad, [bad], 'm') is None
    assert any('work NOT pushed' in n and 'conflict marker' in n for n in f2.notes)


# -- tangentswarm #111: a merge of a PR ahead keeps the pre-merge with no check ------------------

def merge_into_main(w, s, sha, name):
    """main gets a merge commit of sha (first parent: main, second: sha), as GitHub merges a PR."""
    run(w, 'checkout', '-q', '-B', 'main', s.main)
    run(w, 'merge', '-q', '--no-ff', '-m', f'Merge {name}', sha)
    s.main = run(w, 'rev-parse', 'HEAD')
    return s.main


def no_probe(f):
    f.kept_tip = lambda *a, **k: pytest.fail('a merge it assumed must not probe the tip')
    return f


def test_a_merge_of_the_pr_ahead_keeps_the_top_pre_merge_with_no_probe_and_no_write(world):
    """#1 (Human Review) merges into main as its head. #9's b9-v1 was built on main + #1, so
    nothing changed for it: no probe merge, no new -vN, no record write, no did line."""
    w, s, h, origin = world
    f, _ = make(world)
    v1 = f.premerge({HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}, s)[9]
    rec = dict(flow.load_premerge()['9'])
    merge_into_main(w, s, h['hr1'], '#1')
    f2 = no_probe(make(world)[0])
    mr = ['merge-ready']
    assert f2.premerge({ST: [card(2, ST, h['st2'], mr)], RD: [card(9, RD, h['z'], mr)]}, s) == {9: v1}
    assert flow.load_premerge()['9'] == rec and remote(s, origin, 'b9-v2') is None
    assert f2.did == [] and f2.notes == []  # no label, branch or record change


def test_a_merge_of_both_prs_ahead_keeps_the_pre_merge(world):
    w, s, h, origin = world
    f, _ = make(world)
    v1 = f.premerge({HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}, s)[9]
    merge_into_main(w, s, h['st2'], '#2')  # any order
    merge_into_main(w, s, h['hr1'], '#1')
    assert no_probe(make(world)[0]).premerge({RD: [card(9, RD, h['z'])]}, s) == {9: v1}


@pytest.mark.parametrize('change', ['direct push', 'new head ahead', 'pr ahead removed'])
def test_a_change_it_did_not_assume_re_checks_the_top_pr(world, change):
    """tangentswarm #111 rule 4: a commit on main that is not a queued head, a new head on a PR
    ahead, or a PR ahead that left without a merge: the tip is checked again (kept_tip or a new -vN)."""
    w, s, h, origin = world
    f, _ = make(world)
    f.premerge({HR: [card(1, HR, h['hr1'])], ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}, s)
    if change == 'direct push':
        merge_into_main(w, s, h['hr1'], '#1')
        run(w, 'checkout', '-q', 'main')
        (w / 'm.txt').write_text('m\n')
        run(w, 'add', '.')
        run(w, 'commit', '-qm', 'direct')
        s.main = run(w, 'rev-parse', 'HEAD')
        cols = {ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}
    elif change == 'new head ahead':
        merge_into_main(w, s, h['hr1'], '#1')
        cols = {ST: [card(2, ST, h['c'])], RD: [card(9, RD, h['z'])]}
    else:
        cols = {ST: [card(2, ST, h['st2'])], RD: [card(9, RD, h['z'])]}
    f2, _ = make(world)
    probed = []
    real = f2.kept_tip
    f2.kept_tip = lambda *a: probed.append(a[-1]) or real(*a)
    f2.premerge(cols, s)
    assert h['z'] in probed


def test_a_resolved_tip_is_never_kept_without_a_check(world):
    """A tip its worker resolved (a seed) does not prove the head merges: it is checked."""
    w, s, h, origin = world
    f, _ = make(world)
    f.premerge({HR: [card(1, HR, h['hr1'])], RD: [card(9, RD, h['z'])]}, s)
    flow.set_premerge(9, dict(flow.load_premerge()['9'], seed=h['main']))
    merge_into_main(w, s, h['hr1'], '#1')
    f2, _ = make(world)
    assert f2.assumed(s, flow.load_premerge()['9'], f2.future_main(s, {RD: []})[0], h['z']) is None
