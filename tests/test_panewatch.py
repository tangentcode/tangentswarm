"""The pane watcher (tangentswarm #40): capture compare, classes, and what it does to a pane."""
import pytest

from tangentswarm import alerts, board, panewatch as PW, slots

BAR = '─' * 40


def screen(body, typed='', busy=False):
    """A Claude screen: the message lines, the input box, the footer."""
    lines = list(body) + ([] if not busy else ['✻ Working… (12s · esc to interrupt)'])
    return '\n'.join(lines + [BAR, '❯ ' + typed, BAR, '  ? for shortcuts']) + '\n'


IDLE = screen(['● Done: pushed abc1234.'])
ASK = screen(['● Should the button be blue or red?'])
MENU = screen(['● Bash(rm -rf build)', 'Do you want to proceed?', '❯ 1. Yes', '  2. No'])
TRUST = screen(['Do you trust the files in this folder?', '❯ 1. Yes, proceed', '  2. No, exit'])


def test_compare_keeps_the_change_time_while_the_screen_is_the_same():
    a = PW.compare(None, {'hash': 'x'}, 100)
    assert a['changed_at'] == 100 and a['unchanged_s'] is None  # a first capture
    b = PW.compare(a, {'hash': 'x'}, 160)
    assert b['changed_at'] == 100 and b['unchanged_s'] == 60
    c = PW.compare(b, {'hash': 'y'}, 220)
    assert c['changed_at'] == 220 and c['unchanged_s'] == 0


@pytest.mark.parametrize('scr,blank,claude,unchanged,cls', [
    (IDLE, True, False, 60, 'shell'),
    (IDLE, True, True, None, 'new'),
    (IDLE, True, True, 0, 'working'),            # the screen moved: a timer runs
    (screen(['● x'], busy=True), True, True, 60, 'working'),
    (screen(['● x'], busy=True), True, True, 900, 'frozen'),
    (TRUST, True, True, 60, 'dialog'),
    (MENU, True, True, 60, 'menu'),
    (ASK, True, True, 60, 'asking'),
    (screen(['● Done.'], typed='relay text'), False, True, 60, 'input'),
    (IDLE, True, True, 60, 'idle'),
])
def test_classify(scr, blank, claude, unchanged, cls):
    assert PW.classify(scr, blank, claude, unchanged, frozen_s=600) == cls


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', tmp_path)
    top = str(tmp_path / 'platform.feat')
    return dict(top=top, branch='feat', head='h' * 40, dirty=False, ahead=0, scaffold=False)


def pane(wt, cls, window='mn-7-feat', screen=IDLE):
    return dict(id='%1', window=window, cls=cls, wt=wt, screen=screen, path=wt['top'])


def test_decide(worker):
    pr = dict(number=7, col=board.AUTO_REVIEW, draft=False, head='h' * 40)
    assert PW.decide(pane(worker, 'working'), pr) == []
    assert PW.decide(pane(worker, 'input'), pr) == [('submit', '')]
    assert PW.decide(pane(worker, 'idle'), pr) == [('exit', 'card in Auto Review')]
    assert PW.decide(pane(worker, 'idle'), None) == [('exit', 'no open PR for this branch')]
    ip = dict(pr, col=board.IN_PROGRESS)
    assert PW.decide(pane(worker, 'idle'), ip) == [('handoff', 'card In Progress')]
    assert PW.decide(pane(dict(worker, dirty=True), 'idle'), ip) == [('nudge', 'uncommitted changes')]
    assert PW.decide(pane(dict(worker, scaffold=True), 'idle'), ip) == []  # waits for its brief
    assert PW.decide(pane(worker, 'asking', screen=ASK), ip)[0][0] == 'ask'
    assert PW.decide(pane(worker, 'asking', screen=ASK), ip, 'asked Michal') == [('exit', 'asked Michal')]
    assert PW.decide(pane(worker, 'idle', window='meth-26-auto-coordinator-1'), pr) == []  # never a coordinator


class Rig:
    """A PaneWatch over fake panes, with every effect recorded."""

    def __init__(self, worker, screens, pr=None, shell_root=True):
        self.worker, self.screens = worker, screens
        self.calls, self.told, self.handoffs, self.routes, self.wakes, self.incidents = [], [], [], [], [], []
        self.st = alerts.state()

        class R:
            returncode, stdout = 0, ''
        self.w = PW.PaneWatch(
            sh=lambda argv, timeout=30: self.calls.append(argv) or R(),
            panes=lambda: [dict(id='%1', widx='1', window='mn-7-feat', command='claude', path=worker['top'],
                                pane_pid='100')],
            cap=lambda pid: (self.screens[0], self.screens[1]), facts=lambda path: dict(self.worker),
            is_claude=lambda p: True, tell=lambda p, t: self.told.append((p, t)),
            handoff=lambda wt: self.handoffs.append(wt), route=lambda *a: self.routes.append(a) or 'routed',
            wake=lambda why, key, pr=None, detail=None: self.wakes.append((why, detail)),
            incident=lambda key, title, what, **kw: self.incidents.append((key, title, what)),
            st=self.st, shell_root=lambda pid: shell_root)
        self.prs = {'feat': pr} if pr else {}

    def run(self, now):
        return self.w.once(self.prs, now)


AR = dict(number=7, col=board.AUTO_REVIEW, draft=False, head='h' * 40)


def test_a_first_capture_does_nothing_and_an_unchanged_idle_pane_is_closed_with_exit(worker):
    r = Rig(worker, (IDLE, True), pr=AR)
    assert r.run(0) == []
    out = r.run(60)
    assert r.told == [('%1', '/exit')] and 'the window stays' in out[0]
    rep = PW.read_report()
    assert rep['panes']['%1']['cls'] == 'idle' and 'pushed abc1234' in rep['panes']['%1']['screen']


def test_exit_is_never_sent_when_claude_is_the_panes_own_process(worker):
    r = Rig(worker, (IDLE, True), pr=AR, shell_root=False)
    r.run(0)
    out = r.run(60)
    assert r.told == [] and 'would close the window' in out[0]


def test_a_changing_screen_is_working_and_left_alone(worker):
    r = Rig(worker, (IDLE, True), pr=AR)
    r.run(0)
    r.screens = (screen(['● Done: pushed abc1234.', '● more']), True)
    assert r.run(60) == [] and r.told == []


def test_a_missed_stop_hook_handoff_runs_once_with_an_incident(worker):
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS, draft=True))
    r.run(0)
    r.run(60)
    r.run(120)
    assert r.handoffs == [worker['top']]
    assert len(r.incidents) == 1 and 'Stop hook missed' in r.incidents[0][1]


def test_unsent_input_is_submitted_once(worker):
    r = Rig(worker, (screen(['● x'], typed='relay'), False), pr=AR)
    r.run(0)
    r.run(60)
    r.run(120)
    assert r.calls.count(['tmux', 'send-keys', '-t', '%1', 'Enter']) == 1


def test_a_pane_that_asks_is_routed_like_needs_input_and_reported(worker):
    r = Rig(worker, (ASK, True), pr=dict(AR, col=board.IN_PROGRESS))
    r.run(0)
    r.run(60)
    r.run(120)
    assert len(r.routes) == 1 and r.routes[0][0] == 7 and 'blue or red' in r.routes[0][1]
    assert len(r.incidents) == 1 and 'asked in its pane' in r.incidents[0][1]


def test_a_pending_exit_closes_the_claude_and_clears(worker):
    r = Rig(worker, (ASK, True), pr=dict(AR, col=board.NEEDS_INPUT))
    from tangentswarm import needsinput
    needsinput.mark_exit(worker['top'], 7, 'asked Michal (needs_input)')
    r.run(0)
    r.run(60)
    assert r.told == [('%1', '/exit')] and r.routes == []
    assert needsinput.pending_exits() == {}


def test_a_menu_wakes_swarmbot_with_the_capture_once(worker):
    r = Rig(worker, (MENU, True), pr=AR)
    r.run(0)
    r.run(60)
    r.run(120)
    assert len(r.wakes) == 1 and 'Do you want to proceed' in r.wakes[0][1]['capture']
    assert len(r.incidents) == 1


def test_the_trust_dialog_gets_enter_then_swarmbot_if_it_stays(worker):
    r = Rig(worker, (TRUST, True), pr=AR)
    r.run(0)
    r.run(60)
    assert r.calls.count(['tmux', 'send-keys', '-t', '%1', 'Enter']) == 1 and r.wakes == []
    r.run(120)
    assert len(r.wakes) == 1


def test_a_dirty_idle_worker_is_nudged_once(worker):
    r = Rig(dict(worker, dirty=True, ahead=2), (IDLE, True), pr=AR)
    r.worker = dict(worker, dirty=True, ahead=2)
    r.run(0)
    r.run(60)
    r.run(120)
    assert len(r.told) == 1 and 'needs_input' in r.told[0][1] and '2 unpushed' in r.told[0][1]


def test_dry_run_only_says_what_it_would_do(worker):
    r = Rig(worker, (IDLE, True), pr=AR)
    r.w.dry = True
    r.run(0)
    assert r.run(60) == ['would exit #7 %1 mn-7-feat: card in Auto Review'] and r.told == []


def test_a_failing_action_on_one_pane_does_not_stop_the_pass(worker):
    r = Rig(worker, (IDLE, True), pr=AR)

    def tell(p, t):
        raise RuntimeError('never reached an empty prompt')
    r.w.tell = tell
    r.run(0)
    out = r.run(60)
    assert out == ['%1 mn-7-feat: exit failed: RuntimeError: never reached an empty prompt']
    assert PW.read_report()['panes']['%1']['cls'] == 'idle'  # the report is still written
