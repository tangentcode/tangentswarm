"""Agent status reports (tangentswarm #53): report_status over swarm-agent's MCP HTTP server,
the events DB rows, and the daemon's actions (done, needs-input, blocked)."""
import json

import pytest

from tangentswarm import agent_mcp_server as S
from tangentswarm import agent_report as R
from tangentswarm import board, eventdb, launcher, slots, spool, stophook
from test_agent_mcp_server import AGENT_KEY, AgentMcp, AgentServer, key_file

HEAD = 'a' * 40
WT = '/home/x/ver/platform.feat'


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for var in (S.ENV_KEY, S.ENV_KEY_FILE, S.ENV_PUBLIC_URL):
        monkeypatch.delenv(var, raising=False)
    S.IDENTITIES.clear()
    S.REPORTERS.clear()
    yield
    S.IDENTITIES.clear()
    S.REPORTERS.clear()
    S.API_KEY_MODE = False


def rows():
    return eventdb.store().rows("event='agent-report'")


def requests():
    return [json.loads(p.read_text()) for p in sorted(spool.SPOOL.glob('*.req.json'))]


# -- the report itself ---------------------------------------------------------------------

def test_check_normalizes_and_refuses():
    assert R.check('Needs Input', '#12', 'mn-12-feat', question='Which port?') == dict(
        state='needs-input', pr=12, window='mn-12-feat', message='', question='Which port?')
    assert R.check('blocker', 12, 'w', message='no creds')['state'] == 'blocked'
    assert R.check('done', 12, 'w')['state'] == 'done'
    for args, why in ((('finished', 12, 'w'), 'state must be'), (('done', 'x', 'w'), 'PR number'),
                      (('done', 0, 'w'), 'PR number'), (('done', 12, 'bad window; rm'), 'window'),
                      (('needs-input', 12, 'w'), 'question'), (('blocked', 12, 'w'), 'reason')):
        with pytest.raises(R.ReportError, match=why):
            R.check(*args)


# -- over MCP HTTP -------------------------------------------------------------------------

def test_reports_over_http_go_to_the_events_db_and_the_spool(tmp_path):
    with AgentServer({'api_key_file': str(key_file(tmp_path))}) as srv:
        m = AgentMcp(srv, AGENT_KEY, headers={'X-Swarm-Agent': 'mn-12-feat'})
        assert m.initialize()[0] == 200
        done = m.tool('report_status', state='done', pr=12, window='mn-12-feat')
        assert done['ok'] and done['state'] == 'done' and 'end your turn' in done['next']
        q = m.tool('report_status', state='needs-input', pr=12, window='mn-12-feat', question='Port 80 or 8080?')
        assert q['ok'] and 'Needs Input' in q['next']
        b = m.tool('report_status', state='blocked', pr=12, window='mn-12-feat', message='staging is down')
        assert b['ok']
        w = m.tool('report_status', state='working', pr=12, window='mn-12-feat', message='tests next')
        assert w['ok'] and 'queued' not in w
        with pytest.raises(RuntimeError, match='question'):
            m.tool('report_status', state='needs-input', pr=12, window='mn-12-feat')

    got = rows()
    assert [(r['pr'], json.loads(r['decisions']) or [r.get('action')]) for r in got] == [
        (12, ['done']), (12, ['needs-input']), (12, ['blocked']), (12, ['working'])]
    assert 'Port 80 or 8080?' in got[1]['notes'] and 'staging is down' in got[2]['notes']
    reqs = requests()
    assert [(r['op'], r['state'], r['pr'], r['window']) for r in reqs] == [
        ('report', 'done', 12, 'mn-12-feat'), ('report', 'needs-input', 12, 'mn-12-feat'),
        ('report', 'blocked', 12, 'mn-12-feat')]
    assert reqs[1]['question'] == 'Port 80 or 8080?'
    assert reqs[0]['agent'] == 'mn-12-feat' and reqs[0]['principal'] == 'apikey'
    assert reqs[0]['event'] == got[0]['id']


def test_a_session_reports_for_one_pr_and_window_only(tmp_path):
    with AgentServer({'api_key_file': str(key_file(tmp_path))}) as srv:
        m = AgentMcp(srv, AGENT_KEY)
        assert m.initialize()[0] == 200
        m.tool('report_status', state='working', pr=12, window='mn-12-feat', message='x')
        with pytest.raises(RuntimeError, match="registered as '12@mn-12-feat'"):
            m.tool('report_status', state='done', pr=13, window='mn-13-other')
        with pytest.raises(RuntimeError, match="registered as '12@mn-12-feat'"):
            m.tool('report_status', state='done', pr=12, window='mn-99-spoof')
        other = AgentMcp(srv, AGENT_KEY)
        assert other.initialize()[0] == 200
        assert other.tool('report_status', state='working', pr=13, window='mn-13-other', message='y')['ok']
    assert [r['pr'] for r in rows()] == [12, 13]


def test_a_failed_queue_still_logs_and_says_so():
    def boom(*a, **k):
        raise OSError('disk full')
    res = R.accept('done', 12, 'w', submit=boom)
    assert not res['ok'] and 'disk full' in res['error']
    assert [r['error'] for r in rows()] == [None, 'OSError: disk full']


# -- the daemon's actions ------------------------------------------------------------------

class Fake:
    """The daemon's outside world: one PR #12 on branch feat in window mn-12-feat."""

    def __init__(self, monkeypatch, tmp_path, col=board.IN_PROGRESS, issues=(7,), statuses=('busy', 'idle'),
                 pushed=('feat', HEAD), park_why=()):
        self.moves, self.sh, self.told, self.parked, self.exited, self.handoffs = [], [], [], [], [], []
        self.statuses, self.park_why = list(statuses), list(park_why)
        self.slots_path = tmp_path / 'slots.json'
        monkeypatch.setattr(stophook, 'find_pr', lambda g, b, issues_=False, **k: (12, 'ITEM12', col, False,
                                                                                    list(issues)))
        monkeypatch.setattr(stophook, 'move_card', lambda g, item, c, sh: self.moves.append((item, c)))
        self.reporter = R.Reporter(
            g={}, sh=self.sh.append, resolve=lambda pr: ('mn-12-feat', WT, 'feat'),
            running=self.running, handoff=lambda wt: self.handoffs.append(wt) or '#12 handed off',
            park=lambda c, out: self.parked.append(c['pane']) or (self.park_why.pop(0) if self.park_why else None),
            exit_claude=lambda c, out: self.exited.append(c['pane']), tell=lambda n, t: self.told.append(t),
            pushed_head=lambda wt: pushed, sleep=lambda s: None, slots_path=self.slots_path)

    def running(self):
        st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return [dict(pid=1, session='sess-1234', cwd=WT, pane='%5', status=st, branch='feat')]

    def run(self, state, **kw):
        return R.execute(dict(op='report', state=state, pr=12, window='mn-12-feat', event=1, **kw),
                         reporter=self.reporter)


def test_done_hands_off_then_exits_the_idle_claude_keeping_the_window(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, statuses=('busy', 'busy', 'idle'), park_why=["busy (shows 'esc to interrupt')"])
    res = f.run('done')
    assert res['ok'] and f.handoffs == [WT]
    assert f.parked == ['%5', '%5']  # busy at first: tried again until /exit went through
    assert any('closed the Claude with /exit; window mn-12-feat stays' in l for l in res['lines'])
    rec = slots.load(f.slots_path)['parked']['12']
    assert rec['session'] == 'sess-1234' and rec['window'] == 'mn-12-feat' and rec['reason'] == 'done'
    handled = rows()[-1]
    assert 'done handled' in (handled.get('action') or '') + handled['decisions']
    assert '#12 handed off' in handled['notes']


def test_done_with_unpushed_work_is_refused_and_the_claude_keeps_running(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, pushed=('feat', 'uncommitted changes'))
    res = f.run('done')
    assert res['ok'] and f.handoffs == [] and f.parked == []
    assert f.told and 'uncommitted changes' in f.told[0] and "report_status(state='done')" in f.told[0]


def test_needs_input_posts_the_question_moves_the_card_and_closes_the_claude(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, pushed=('feat', 'uncommitted changes'))
    res = f.run('needs-input', question='Port 80 or 8080?')
    assert res['ok']
    assert f.moves == [('ITEM12', board.NEEDS_INPUT)]
    (argv,) = f.sh
    assert argv[:4] == ['gh', 'issue', 'comment', '7']
    body = argv[argv.index('--body') + 1]
    assert stophook.QUESTION_MARK in body and '> Port 80 or 8080?' in body
    assert f.exited == ['%5'] and f.parked == []  # not strict: a dirty worktree may be closed
    assert slots.load(f.slots_path)['parked']['12']['reason'] == 'needs-input'
    f.sh.clear()
    f.run('needs-input', question='Port 80 or 8080?')  # the same question again: not posted twice
    assert f.sh == []


def test_a_new_question_on_a_card_already_in_needs_input_is_posted_without_a_move(monkeypatch, tmp_path):
    # Codex on PR #54: after a blocked report the card is in Needs Input; a later question must still reach Michal
    f = Fake(monkeypatch, tmp_path, col=board.NEEDS_INPUT)
    res = f.run('needs-input', question='Which?')
    assert f.moves == [] and len(f.sh) == 1 and '> Which?' in f.sh[0][-1] and f.exited == ['%5']
    assert any('already in Needs Input; posted the question' in l for l in res['lines'])
    f.sh.clear()
    f.run('needs-input', question='Which?')
    assert f.sh == []  # the same question once only
    f.run('blocked', message='new blocker')
    assert f.moves == [] and len(f.sh) == 1 and R.BLOCKED_MARK in f.sh[0][-1]


def test_a_failed_post_is_posted_on_retry(monkeypatch, tmp_path):
    # Codex on PR #54: the move went through, the comment failed; the retry must post it
    f = Fake(monkeypatch, tmp_path)
    calls = []

    def sh(argv):
        calls.append(argv)
        if len(calls) == 1:
            raise RuntimeError('gh: 502')
    f.reporter.sh = sh
    with pytest.raises(RuntimeError, match='502'):  # spool.serve turns it into a logged failure
        f.run('needs-input', question='Which?')
    assert f.moves == [('ITEM12', board.NEEDS_INPUT)] and 'failed' in rows()[-1]['notes'] + str(rows()[-1].get('action'))
    col = {'v': board.NEEDS_INPUT}
    monkeypatch.setattr(stophook, 'find_pr', lambda g, b, **k: (12, 'ITEM12', col['v'], False, [7]))
    f.run('needs-input', question='Which?')
    assert len(calls) == 2 and '> Which?' in calls[1][-1]


def test_a_card_in_another_column_is_left_alone(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, col=board.READY)
    for state, kw in (('needs-input', {'question': 'Which?'}), ('blocked', {'message': 'x'})):
        res = f.run(state, **kw)
        assert any('in Ready: not moved, not posted' in l for l in res['lines'])
    assert f.moves == [] and f.sh == [] and f.exited == []


def test_the_rework_slot_is_kept_until_the_claude_is_closed(monkeypatch, tmp_path):
    # Codex on PR #54: a refused close must not free the rework slot of a still-running Claude
    f = Fake(monkeypatch, tmp_path, park_why=['worktree has leftover processes'])
    with slots.update(f.slots_path) as st:
        st['rework']['12'] = {'worktree': WT}
    res = f.run('done')
    assert any('was not closed' in l for l in res['lines'])
    assert '12' in slots.load(f.slots_path)['rework']
    f.run('needs-input', question='Which?')  # exit_idle succeeds
    assert '12' not in slots.load(f.slots_path)['rework']


def test_blocked_posts_and_moves_but_keeps_the_claude(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, issues=())
    f.run('blocked', message='staging is down')
    assert f.moves == [('ITEM12', board.NEEDS_INPUT)]
    assert f.sh[0][:4] == ['gh', 'issue', 'comment', '12'] and R.BLOCKED_MARK in f.sh[0][-1]
    assert f.exited == [] and f.parked == []
    f.sh.clear()
    f.run('blocked', message='staging is down')  # the same blocker again: not posted twice
    assert f.sh == []


def test_identity_is_the_prs_own_window(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path)
    res = R.execute(dict(op='report', state='done', pr=12, window='mn-99-other'), reporter=f.reporter)
    assert not res['ok'] and 'belongs to window mn-12-feat' in res['error']
    assert f.handoffs == [] and f.parked == []
    assert rows()[-1]['error'] and 'refused' in (rows()[-1].get('action') or rows()[-1]['decisions'])
    monkeypatch.setattr(stophook, 'find_pr', lambda g, b, **k: (13, 'ITEM13', board.IN_PROGRESS, False, []))
    res = f.run('done')
    assert not res['ok'] and 'is PR #13, not #12' in res['error']


def test_a_claude_that_stays_busy_is_not_closed(monkeypatch, tmp_path):
    f = Fake(monkeypatch, tmp_path, statuses=('busy',))
    t = [0.0]
    f.reporter.clock = lambda: t[0]
    f.reporter.sleep = lambda s: t.__setitem__(0, t[0] + s)
    f.reporter.close_wait = 10
    res = f.run('needs-input', question='?')
    assert f.exited == [] and any('stayed busy' in l for l in res['lines'])


def test_the_spool_serves_report(monkeypatch):
    got = []
    monkeypatch.setattr(R, 'execute', lambda req: got.append(req) or {'ok': True})
    assert spool.execute({'op': 'report', 'state': 'done', 'pr': 12, 'window': 'w'}) == {'ok': True}
    assert got[0]['pr'] == 12 and 'report' in spool.OPS


def test_last_report_is_for_the_watchdog():
    assert R.last_report(12) is None
    R.accept('working', 12, 'w', message='tests')
    R.log('working handled', 12, 'w')
    assert R.last_report(12)['pr'] == 12
    assert R.last_report(13) is None
    assert R.last_report(12, within=10, now=10 ** 11) is None


def test_pending_input_until_handled_or_superseded():
    noop = lambda *a, **k: {'queued': 'x'}
    assert R.pending_input(12) is None
    R.accept('needs-input', 12, 'w', question='Which?', submit=noop)
    R.accept('working', 12, 'w', message='waiting')
    assert R.pending_input(12)['pr'] == 12 and R.pending_input(13) is None
    R.log('needs-input refused', 12, 'w', error='x')
    assert R.pending_input(12) is None
    R.accept('blocked', 12, 'w', message='down', submit=noop)
    assert R.pending_input(12)
    R.accept('done', 12, 'w', submit=noop)
    assert R.pending_input(12) is None
    R.accept('blocked', 12, 'w', message='down', submit=noop)
    assert R.pending_input(12, now=10 ** 11) is None  # a report never handled stops holding after the TTL


# -- the worker brief ----------------------------------------------------------------------

def test_the_brief_tells_the_worker_to_report():
    text = launcher.brief_text(7, 'Fix the port', 'issue-7-port', 12)
    assert "report_status(state='done', pr=12" in text
    assert "report_status(state='needs-input', pr=12" in text
    assert 'swarm-agent MCP server' in text and "tmux display-message -p '#W'" in text
