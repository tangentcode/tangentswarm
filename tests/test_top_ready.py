"""tangentswarm #146 (Michal 2026-11-18 2:09-2:18 PM ET): the fill deals only with the top Ready
PR, updates it onto main after every merge and runs its tests again, fixes it in place (never
In Progress), and tells Michal at once when the flow stalls. No network: GitHub, the workflow
runs, the workers and the webhooks are fakes; the git repos are local."""
import subprocess

import pytest

from tangentswarm import advance as A, board, flow, stall, webhook as W

from test_advance import CascadeGate, pr
from test_premerge import card, make, remote, run, world  # noqa: F401 (world is a fixture)
from test_webhook import Fake, codex_pr

RD, ST, HR, IP = board.READY, board.STAGING, board.HUMAN_REVIEW, board.IN_PROGRESS
GREEN = [{'name': 'rs tests', 'status': 'completed', 'conclusion': 'success'}]
RUNNING = [{'name': 'rs tests', 'status': 'in_progress', 'conclusion': None}]
RED = [{'name': 'rs tests', 'status': 'completed', 'conclusion': 'failure'}]


# -- 2. update the top Ready PR onto main and run its tests again ---------------------------------

@pytest.fixture
def ci(monkeypatch):
    """The tip tests on, with fake workflow runs: runs[tip] is what the check-run read sees."""
    monkeypatch.setattr(flow, 'PREMERGE_TESTS', ('rs.yml',))
    state = dict(dispatched=[], runs={}, default=RUNNING)

    def install(f):
        f.g['dispatch_tests'] = lambda ref: state['dispatched'].append(ref) or []
        f.g['tip_runs'] = lambda tip: state['runs'].get(tip, state['default'])
        return f
    state['install'] = install
    return state


def test_the_top_pr_moves_up_only_when_its_tests_pass_on_its_update_onto_main(world, ci):
    w, s, h, origin = world
    f, sent = make(world)
    ci['install'](f)
    p9 = card(9, RD, h['z'])
    cols = {HR: [card(1, HR, h['hr1'])], RD: [p9]}
    tip = f.premerge(cols, s)[9]
    assert ci['dispatched'] == ['b9-v1'] and tip == remote(s, origin, 'b9-v1')
    assert p9['own_labels'] == ['pre-merging']  # something is happening to it (Michal 2:18 PM ET)
    assert 'its tests run on b9-v1' in f.blocked[9]
    # the next pass: the tests still run; no second dispatch, no pull
    f2, _ = make(world)
    ci['install'](f2)
    assert f2.pull_batch({}, cols, s, room=2) == []
    assert ci['dispatched'] == ['b9-v1']
    assert any(n.startswith('#9 is next in the pick order: it is being updated onto main') for n in f2.notes)
    # green: merge-ready, and it moves up
    ci['runs'][tip] = GREEN
    f3, _ = make(world)
    ci['install'](f3)
    assert [p['number'] for p in f3.pull_batch({}, cols, s, room=2)] == [9]
    assert flow.load_premerge()['9']['tested'] == tip and (9, 'add', ['merge-ready']) in flow.LABELS_WRITTEN
    # a green tip is never read again
    f4, _ = make(world)
    f4.g['tip_runs'] = lambda t: pytest.fail('a tested tip is not read again')
    assert f4.tip_tests(p9, tip) is None


def test_a_merge_into_main_updates_the_top_pr_onto_the_new_main_and_tests_it_again(world, ci):
    """Michal 2026-11-18 2:16 PM ET: "rebased onto main ... preemptively as we merge things".
    The #547 branch was two days old and never tested with #543: after a merge, the kept -vN
    is not enough; the top PR gets a new -vN on the new main and its tests run again."""
    w, s, h, origin = world
    f, _ = make(world)
    ci['install'](f)
    ci['default'] = GREEN
    p9 = card(9, RD, h['z'])
    v1 = f.premerge({RD: [p9]}, s)[9]
    f.premerge({RD: [p9]}, s)  # read green
    assert flow.load_premerge()['9']['tested'] == v1
    # #543 merges into main
    run(w, 'checkout', '-q', 'main')
    (w / 'm.txt').write_text('543\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'merge #543')
    s.main = run(w, 'rev-parse', 'HEAD')
    ci['default'] = RUNNING
    f2, _ = make(world)
    ci['install'](f2)
    v2 = f2.premerge({RD: [p9]}, s)[9]
    assert v2 != v1 and remote(s, origin, 'b9-v2') == v2
    assert subprocess.run(['git', '--git-dir', s.dir, 'merge-base', '--is-ancestor', s.main, v2]).returncode == 0
    assert ci['dispatched'] == ['b9-v1', 'b9-v2'] and p9['own_labels'] == ['pre-merging']


def test_failing_tests_after_the_update_are_fixed_in_place(world, ci):
    w, s, h, origin = world
    f, sent = make(world)
    ci['install'](f)
    ci['default'] = RED
    p9 = card(9, RD, h['z'])
    f.premerge({RD: [p9]}, s)
    f2, sent2 = make(world)
    ci['install'](f2)
    f2.premerge({RD: [p9]}, s)
    assert flow.fixing(9) and flow.load_fixes()['9']['kind'] == 'tests'
    assert len(sent2) == 1 and sent2[0][2] is True and 'stays in Ready' in sent2[0][1]
    assert '"rs tests" failure' in sent2[0][1] and p9['own_labels'] == ['pre-merging']
    f2.premerge({RD: [p9]}, s)
    assert len(sent2) == 1  # once per failed tip


def test_tests_that_do_not_start_are_tried_again_and_block_the_pull(world, ci):
    w, s, h, origin = world
    f, _ = make(world)
    f.g['dispatch_tests'] = lambda ref: ['rs.yml: HTTP 403']
    p9 = card(9, RD, h['z'])
    assert f.pull_batch({}, {RD: [p9]}, s, room=1) == []
    assert 'its tests did not start: rs.yml: HTTP 403' in f.blocked[9]
    assert 'dispatched' not in flow.load_premerge()['9']


def test_no_tip_tests_when_none_are_set(world):
    w, s, h, origin = world
    f, _ = make(world)
    f.g['dispatch_tests'] = lambda ref: pytest.fail('no tests are set')
    p9 = card(9, RD, h['z'])
    assert [p['number'] for p in f.pull_batch({}, {RD: [p9]}, s, room=1)] == [9]


def test_only_the_top_ready_pr_is_pre_merged_whatever_the_env_says():
    assert flow.PREMERGE_AHEAD == 1


# -- 3. fixed in place: a new head keeps the card in Ready -------------------------------------------

def test_a_push_to_a_ready_pr_fixed_in_place_stays_in_ready_and_asks_codex():
    with flow.fix_update() as recs:
        recs['7'] = dict(head='a' * 40, kind='conflict')
    ex = Fake(col=RD, full=dict(codex_pr(head='e' * 40), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert not any(c[:3] == ['gh', 'project', 'item-edit'] for c in ex.calls)
    assert ex.calls.count(['gh', 'pr', 'comment', '7', '-R', W.REPO, '--body', '@codex review']) == 1
    assert ex.cascades == [1] and any('the card stays in Ready' in d for d in ex.done)


def test_a_push_to_a_ready_pr_not_fixed_in_place_still_goes_to_auto_review():
    ex = Fake(col=RD, full=dict(codex_pr(head='e' * 40), isDraft=False))
    ex.run(W.Action('handoff', 7, args={'push': True}))
    assert ex.calls[0][-1] == board.OPTION[board.AUTO_REVIEW]


@pytest.mark.parametrize('action', [W.Action('codex', 7), W.Action('ci', 7, args={'sha': 'e' * 40})])
def test_codex_findings_or_red_ci_on_a_fix_relay_to_the_worker_and_keep_it_in_ready(monkeypatch, action):
    monkeypatch.setattr(A, 'check_runs', lambda sha, repo=None: RED)
    with flow.fix_update() as recs:
        recs['7'] = dict(head='a' * 40, kind='conflict')
    ex = Fake(col=RD, full=codex_pr(head='e' * 40, findings=True))
    ex.run(action)
    assert ex.relays and not any(c[:3] == ['gh', 'project', 'item-edit'] for c in ex.calls)
    assert any('fixed in place: the card stays in Ready' in d for d in ex.done)


# -- 4. tell Michal at once when the flow stalls --------------------------------------------------

def stalled(monkeypatch, cols, blocked=None, dry=False):
    monkeypatch.setattr(flow, 'STATE', flow.FIX_STATE.with_name('pull.json'))
    out = []
    c = A.Cascade(g=CascadeGate(), dry_run=dry, out=out.append, sleep=lambda s: None)
    c.f.blocked.update(blocked or {})
    return c, out


def test_a_blocked_top_pr_sends_one_plain_english_notice_naming_it(monkeypatch, tmp_path):
    posts = []
    stall.STALL_URL_FILE.write_text('https://example.invalid/stall\n')
    monkeypatch.setattr(W, 'post_wake', lambda payload, **k: posts.append((payload, k)) or True)
    cols = {HR: [pr(1, HR)], RD: [pr(580, RD), pr(581, RD)]}
    c, out = stalled(monkeypatch, cols, {580: 'it conflicts with main in web/x.ts'})
    text = c.stall(cols)
    assert text == ("The flow into Human Review is stalled: Human Review has 1 of 2. #580 (the top Ready PR) "
                    "blocks it: it conflicts with main in web/x.ts.")
    assert posts[0][0]['kind'] == 'stall' and posts[0][0]['pr'] == 580 and posts[0][0]['text'] == text
    assert posts[0][1]['url_file'] == stall.STALL_URL_FILE and posts[0][1]['background'] is False
    assert out[-1].startswith('STALL_NOTICE: ') and out[-1].endswith('(sent to the stall webhook)')
    c.stall(cols)  # the same stall: no second notice
    assert len(posts) == 1 and out[-1].endswith('(notice already sent)')
    c.stall({HR: [pr(1, HR), pr(2, HR)], RD: []})  # full again
    c.stall(cols)  # a new stall: told again
    assert len(posts) == 2


def test_an_empty_ready_says_so(monkeypatch):
    woke = []
    monkeypatch.setattr(stall, 'notify', lambda text, k, pr=None: woke.append((text, k, pr)) or 'ok')
    c, out = stalled(monkeypatch, {HR: [], RD: []})
    c.stall({HR: [], RD: []})
    assert woke[0][0] == 'The flow into Human Review is stalled: Human Review has 0 of 2. Ready is empty.'
    assert woke[0][2] is None


def test_no_notice_while_human_review_is_full_or_a_pull_fills_it(monkeypatch):
    monkeypatch.setattr(stall, 'notify', lambda *a, **k: pytest.fail('no stall'))
    c, out = stalled(monkeypatch, {})
    assert c.stall({HR: [pr(1, HR), pr(2, HR)], RD: [pr(3, RD)]}) is None
    flow.save_pulls([dict(pr=3, head='3' * 40, branch='b3', item='I3', work='w' * 40, started=0)])
    assert c.stall({HR: [pr(1, HR)], RD: [pr(3, RD)]}) is None


def test_with_no_stall_webhook_swarmbot_is_woken_never_only_a_log_line(monkeypatch):
    woke = []
    note = stall.notify('stalled: #5', stall.key(5, 'h', 'x'), pr=5,
                        wake=lambda text, key, pr=None, kind=None, now=None: woke.append((text, key, pr, kind)))
    assert woke == [('stalled: #5', 'stall:#5@h:x', 5, 'stall')] and 'swarmbot woken' in note


def test_a_failed_post_is_tried_again(monkeypatch):
    stall.STALL_URL_FILE.write_text('https://example.invalid/stall\n')
    tries = []
    post = lambda payload, **k: tries.append(1) or False  # noqa: E731
    assert 'failed' in stall.notify('t', 'k', post=post)
    assert stall.notify('t', 'k', post=lambda payload, **k: True) == 'sent to the stall webhook'


def test_the_stall_key_ignores_changing_details():
    assert stall.key(9, 'a' * 40, 'it is being updated onto main; its tests run on b9-v2 (x)') == \
        stall.key(9, 'a' * 40, 'it is being updated onto main; its tests run on b9-v2 (y)')
    assert stall.key(9, 'a' * 40, 'it conflicts with main in x') != stall.key(9, 'b' * 40, 'it conflicts with main in x')


def test_the_cascade_names_the_blocked_top_pr_in_its_stall_notice(monkeypatch, tmp_path):
    """The whole pass: #580 conflicts with main, so the fill waits and the notice names it."""
    from test_advance import cascade
    sent = []
    monkeypatch.setattr(stall, 'notify', lambda text, k, pr=None: sent.append(text) or 'ok')
    c = {HR: [pr(1, HR)], RD: [pr(580, RD), pr(581, RD)]}
    out, pulled = cascade(monkeypatch, tmp_path, c, CascadeGate(), main_dirty={580})  # sets up the fakes
    assert pulled == []
    out = []
    cas = A.Cascade(g=CascadeGate(), dry_run=True, out=out.append, sleep=lambda s: None)
    cas.pulled = []
    cas.stall(cas.fill())
    assert any(l == ('STALL_NOTICE (would send): The flow into Human Review is stalled: Human Review has 1 of 2. '
                     '#580 (the top Ready PR) blocks it: it conflicts with main in f580.ts.') for l in out)
