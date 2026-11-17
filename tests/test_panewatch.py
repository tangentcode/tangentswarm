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
    ni = dict(pr, col=board.NEEDS_INPUT)
    assert PW.decide(pane(worker, 'asking', screen=ASK), ni) == [('exit', 'card in Needs Input')]
    assert PW.decide(pane(dict(worker, dirty=True), 'idle'), ni) == [('exit', 'card in Needs Input')]  # no nudge
    assert PW.decide(pane(worker, 'idle', window='meth-26-auto-coordinator-1'), pr) == []  # never a coordinator


class Rig:
    """A PaneWatch over fake panes, with every effect recorded."""

    def __init__(self, worker, screens, pr=None, shell_root=True, transcript='/t/s.jsonl'):
        self.worker, self.screens = worker, screens
        self.transcripts = []
        self.calls, self.told, self.handoffs, self.routes, self.wakes, self.incidents = [], [], [], [], [], []
        self.results = {}
        self.st = alerts.state()

        class R:
            returncode, stdout = 0, ''
        self.w = PW.PaneWatch(
            sh=lambda argv, timeout=30: self.calls.append(argv) or R(),
            panes=lambda: [dict(id='%1', widx='1', window='mn-7-feat', command='claude', path=worker['top'],
                                pane_pid='100')],
            cap=lambda pid: (self.screens[0], self.screens[1]), facts=lambda path: dict(self.worker),
            is_claude=lambda p: True, reported=lambda n: False, tell=lambda p, t: self.told.append((p, t)),
            handoff=lambda wt, t: self.transcripts.append(t) or self.handoffs.append(wt)
            or self.results.get(len(self.handoffs), {'done': True, 'state': 'handed', 'log': 'moved'}),
            transcript=lambda wt: transcript,
            route=lambda *a: self.routes.append(a) or 'routed',
            wake=lambda why, key, pr=None, detail=None: self.wakes.append((why, detail)),
            incident=lambda key, title, what, **kw: self.incidents.append((key, title, what, kw.get('fix'))),
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


def test_the_missed_handoff_carries_the_workers_transcript(worker):
    """Codex on PR #52: the Stop hook's question check (partial scope, a pending decision goes
    to Needs Input, never ready) needs the transcript, also on a retry."""
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS))
    r.results = {1: {'done': False, 'why': 'x'}}
    r.run(0)
    r.run(60)
    r.run(120)
    assert r.transcripts == ['/t/s.jsonl', '/t/s.jsonl']


def test_no_transcript_means_a_wake_never_a_handoff(worker):
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS), transcript=None)
    r.run(0)
    out = r.run(60)
    r.run(120)
    assert r.handoffs == [] and 'no transcript' in out[0]
    assert len(r.wakes) == 1 and 'no transcript was found' in r.wakes[0][0]
    r.w.transcript = lambda wt: '/t/late.jsonl'  # the transcript shows up: the handoff runs
    r.run(180)
    assert r.handoffs == [worker['top']] and r.transcripts == ['/t/late.jsonl']


def test_transcript_of_finds_the_newest_session(tmp_path):
    import os
    wt = '/home/x/ver/repo.feat'
    d = tmp_path / '-home-x-ver-repo-feat'
    d.mkdir()
    (d / 'old.jsonl').write_text('{}')
    (d / 'new.jsonl').write_text('{}')
    os.utime(d / 'old.jsonl', (1, 1))
    assert PW.transcript_of(wt, tmp_path) == str(d / 'new.jsonl')
    assert PW.transcript_of('/home/x/none', tmp_path) is None


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
    assert r.routes[0][2] == 'mn-7-feat'  # the window agent_report checks against the PR
    assert len(r.incidents) == 1 and 'asked in its pane' in r.incidents[0][1]


def test_a_claude_whose_card_is_in_needs_input_is_closed(worker):
    r = Rig(worker, (ASK, True), pr=dict(AR, col=board.NEEDS_INPUT))
    r.run(0)
    r.run(60)
    assert r.told == [('%1', '/exit')] and r.routes == []


def test_an_agents_own_recent_report_holds_the_watcher_off(worker):
    """tangentswarm #53: the pane capture is the fallback; a reporting agent is left alone."""
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS))
    r.w.reported = lambda n: n == 7
    r.run(0)
    assert r.run(60) == [] and r.handoffs == [] and r.told == []
    assert 'reported recently' in PW.read_report()['panes']['%1']['note']
    r.w.reported = lambda n: False
    r.run(120)
    assert r.handoffs == [worker['top']]


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


def test_a_dry_run_writes_no_report_and_no_once_key(worker):
    r = Rig(worker, (screen(['● x'], typed='relay'), False), pr=AR)
    r.w.dry = True
    r.run(0)
    r.run(60)
    assert PW.read_report() == {'at': None, 'panes': {}} and alerts.state().data['done'] == {}


@pytest.mark.parametrize('scr,blank,col,attr', [
    (ASK, True, board.IN_PROGRESS, 'route'),
    (IDLE, True, board.IN_PROGRESS, 'handoff'),
    (screen(['● x'], typed='relay'), False, board.AUTO_REVIEW, 'sh'),
    (MENU, True, board.AUTO_REVIEW, 'wake'),
])
def test_a_failed_action_is_retried_next_pass_not_marked_done(worker, scr, blank, col, attr):
    """Codex on PR #51: the once-key is saved only after the action succeeds."""
    r = Rig(worker, (scr, blank), pr=dict(AR, col=col, draft=False))
    calls = []
    real = getattr(r.w, attr)

    def flaky(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError('GitHub 502')
        return real(*a, **k)
    setattr(r.w, attr, flaky)
    r.run(0)
    assert 'RuntimeError: GitHub 502' in r.run(60)[0]
    assert r.run(120) and len(calls) == 2  # retried, and done
    r.run(180)
    assert len(calls) == 2  # then never again


def test_a_failed_tmux_send_keys_is_an_error_not_a_success(worker):
    r = Rig(worker, (screen(['● x'], typed='relay'), False), pr=AR)

    class Bad:
        returncode, stdout, stderr = 1, '', "can't find pane %1"
    r.w.sh = lambda argv, timeout=30: Bad()
    r.run(0)
    assert "can't find pane" in r.run(60)[0]


def test_typed_input_under_an_old_question_is_sent_not_routed(worker):
    """Codex on PR #51: the answer typed under a question must be submitted."""
    assert PW.classify(screen(['● Should the button be blue or red?'], typed='Blue.'), False, True, 60) == 'input'
    r = Rig(worker, (screen(['● Should the button be blue or red?'], typed='Blue.'), False), pr=AR)
    r.run(0)
    r.run(60)
    assert r.calls == [['tmux', 'send-keys', '-t', '%1', 'Enter']] and r.routes == []


def test_a_handoff_that_does_not_finish_is_retried_then_goes_to_swarmbot(worker, monkeypatch):
    """Codex on PR #51: a tried handoff is not a done handoff. tangentswarm #120: it is retried
    each pass, and after HANDOFF_WAIT the board's bot gets it once, with an incident."""
    monkeypatch.setattr(PW, 'HANDOFF_WAIT', 100)
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS))
    r.results = {1: {'done': False, 'why': 'GitHub still shows 1234567'}}

    def boom(wt, t):
        r.handoffs.append(wt)
        raise RuntimeError('gh: HTTP 502')
    r.run(0)
    assert 'did not finish (GitHub still shows 1234567): tries again' in r.run(60)[0]
    r.w.handoff = boom
    assert 'RuntimeError: gh: HTTP 502' in r.run(120)[0] and r.wakes == []
    out = r.run(180)
    assert 'did not finish in 1 min' in out[0] and 'woke swarmbot' in out[0]
    assert len(r.handoffs) == 3 and len(r.wakes) == 1 and 'handoff did not finish' in r.wakes[0][0]
    assert len(r.incidents) == 1 and 'could not hand off' in r.incidents[0][1]
    assert r.run(240) == [] and len(r.handoffs) == 3  # once per head


def test_a_dirty_idle_worker_is_nudged_and_told_to_swarmbot_once(worker):
    """tangentswarm #120: stray files or unpushed commits: not moved; reported once to the bot."""
    r = Rig(dict(worker, dirty=True, ahead=2), (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS))
    r.run(0)
    out = r.run(60)
    r.run(120)
    assert r.handoffs == [] and len(r.told) == 1 and 'swarmbot told' in out[0]
    assert len(r.wakes) == 1 and 'uncommitted changes, 2 unpushed commit(s)' in r.wakes[0][0]
    assert len(r.incidents) == 1


def test_the_watcher_runs_the_handoff_itself_not_through_the_spool(worker, monkeypatch):
    """tangentswarm #120: "queued the handoff" and nothing drained it. The default hook calls
    worker_mcp.do_handoff in this process; no spool request is written."""
    from tangentswarm import spool, worker_mcp
    monkeypatch.setattr(spool, 'submit', lambda *a, **k: pytest.fail('no spool request'))
    calls = []
    monkeypatch.setattr(worker_mcp, 'do_handoff', lambda wt, transcript=None: calls.append((wt, transcript))
                        or {'done': True, 'state': 'handed', 'log': '#7 head hhhhhhh pushed. Moved.'})
    r = Rig(worker, (IDLE, True), pr=dict(AR, col=board.IN_PROGRESS, draft=True))
    r.w.handoff = PW.PaneWatch._handoff
    r.run(0)
    out = r.run(60)
    assert calls == [(worker['top'], '/t/s.jsonl')]
    assert 'ran the handoff (the Stop hook missed it): #7 head hhhhhhh pushed. Moved.' in out[0]
    assert r.incidents[0][3] == 'The pane watcher ran the handoff itself: #7 head hhhhhhh pushed. Moved.'


def test_spool_peek_reads_an_answer_once(tmp_path):
    from tangentswarm import spool
    assert spool.peek('x', spool=tmp_path) is None
    (tmp_path / 'x.res.json').write_text('{"ok": true}')
    assert spool.peek('x', spool=tmp_path) == {'ok': True} and spool.peek('x', spool=tmp_path) is None
