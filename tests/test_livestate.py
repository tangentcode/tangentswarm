"""The live agent state (tangentswarm #44) and the Agents screen of swarm-events: real
processes, Claude status, screen changes, memory, CPU, PR and card; capacity from that state."""
import io
import json
import os
import types

from tangentswarm import agents_view as AV, events_view as V, launches, livestate as LS

MB = 1024 ** 2
NOW = 1_800_000_000.0


def proc(pid, ppid, comm, cmd, ticks=0, rss_pages=0):
    return dict(ppid=ppid, comm=comm, cmd=cmd, ticks=ticks, rss=rss_pages * LS.PAGE)


TABLE = {
    100: proc(100, 1, 'bash', '-bash', rss_pages=100),
    101: proc(101, 100, 'claude', '/home/u/.npm-global/bin/claude', ticks=500, rss_pages=1000),
    102: proc(102, 101, 'bash', '/bin/bash -c eval pytest', rss_pages=10),
    103: proc(103, 102, 'python3', '/usr/bin/python3 -m pytest -q tests', ticks=200, rss_pages=300),
    104: proc(104, 101, 'swarm-worker-mc', '/venv/bin/python3 /home/u/bin/swarm-worker-mcp', rss_pages=50),
    200: proc(200, 1, 'bash', '-bash', rss_pages=100),
    201: proc(201, 200, 'claude', 'claude', rss_pages=900),
    300: proc(300, 1, 'bash', '-bash', rss_pages=100),
    301: proc(301, 300, 'node', 'node server.js', rss_pages=400),
    999: proc(999, 1, 'claude', 'claude'),  # not in a pane
}
PANES = [dict(pane='%1', index=3, window='mn-1-busy', pid=100, cwd='/w/platform.busy'),
         dict(pane='%2', index=4, window='mn-2-idle', pid=200, cwd='/w/platform.idle'),
         dict(pane='%3', index=5, window='mn-3-shell', pid=300, cwd='/w/platform.shell')]
SESS = {101: {'status': 'busy', 'cwd': '/w/platform.busy'},
        201: {'status': 'idle', 'cwd': '/w/platform.idle', 'statusUpdatedAt': (NOW - 600) * 1000}}
CARDS = {'busy': (601, 'In Progress'), 'idle': (602, 'Pending')}
BRANCH = {'/w/platform.busy': 'busy', '/w/platform.idle': 'idle', '/w/platform.shell': 'shell'}


def rows(**kw):
    return LS.agent_rows(PANES, TABLE, SESS, CARDS, now=NOW, branch=BRANCH.get, **kw)


def test_tree_and_kinds():
    assert LS.tree(TABLE, 100) == [100, 101, 102, 103, 104]
    assert [LS.kind(TABLE[p]) for p in (100, 101, 102, 103, 104, 301)] == [
        None, 'claude', None, 'test', None, 'other']


def test_rows_show_claude_state_tests_memory_pr_and_card():
    busy, idle, shell = rows()
    assert (busy['claude'], busy['state'], busy['pr'], busy['card']) == (101, 'active', 601, 'In Progress')
    assert busy['tests'] == [(103, '/usr/bin/python3 -m pytest -q tests')] and busy['other'] == []
    assert busy['mem'] == (100 + 1000 + 10 + 300 + 50) * LS.PAGE
    assert (idle['claude'], idle['state'], idle['idle_s'], idle['card']) == (201, 'idle', 600, 'Pending')
    assert (shell['claude'], shell['state'], shell['pr']) == (None, 'no claude', None)
    assert shell['other'] == [(301, 'node server.js')]


def test_a_changed_screen_makes_an_idle_claude_active():
    """#40's 1-minute capture: a screen that changed in the last minute is a working Claude."""
    assert LS.state_of(201, {'status': 'idle'}, 30) == 'active'
    assert LS.state_of(201, {'status': 'idle'}, 600) == 'idle'
    assert LS.state_of(201, {'status': 'idle'}, None) == 'idle'
    assert LS.state_of(201, None, None) == 'idle'
    assert LS.state_of(None, {'status': 'busy'}, 1) == 'no claude'


def test_pane_watch_captures_once_a_minute_and_tracks_changes():
    screens = iter(['a', 'a', 'b'])
    calls = []

    def cap(pane):
        calls.append(pane)
        return next(screens)
    w = LS.PaneWatch(every=60, capture=cap)
    assert w.screen('%1', 0) is None          # the first capture: no change seen yet
    assert w.screen('%1', 30) is None and calls == ['%1']  # not captured again inside the minute
    assert w.screen('%1', 60) is None         # same screen
    assert w.screen('%1', 120) == 0           # changed at 120
    assert w.screen('%1', 150) == 30 and len(calls) == 3


def test_pane_watch_cpu_is_a_share_of_one_cpu_between_samples():
    w = LS.PaneWatch(capture=lambda p: None)
    assert w.cpu({1: dict(ticks=0)}, 0) == {}
    got = w.cpu({1: dict(ticks=LS.TICK), 2: dict(ticks=5)}, 2.0)
    assert got == {1: 50.0}


def test_rows_sum_cpu_over_the_pane_tree():
    busy = rows(cpu={101: 20.0, 103: 75.5, 999: 50.0})[0]
    assert busy['cpu'] == 95.5


def h(**kw):
    m = dict(current=1000 * MB, high=4608 * MB, max=5120 * MB, avail=4 * 1024 ** 3)
    d = dict(load=[0.5, 0.5, 0.5], cpus=4, mem=m, mem_error='', claudes=3, max_claudes=10)
    d.update(kw)
    return d


def test_capacity_from_live_claudes_memory_and_cpu():
    assert LS.capacity(h()) == (7, '')
    assert LS.capacity(h(claudes=10)) == (0, '10 Claudes run (cap 10)')
    free, why = LS.capacity(h(load=[1, 7.0, 1]))
    assert free == 0 and why.startswith('CPU load 7.00 over 6.0')
    free, why = LS.capacity(h(mem=dict(h()['mem'], current=4500 * MB)))
    assert free == 0 and why.startswith('worker memory holds starts')
    free, why = LS.capacity(h(mem=None, load=[1, 9.0, 1]))
    assert free == 0 and 'CPU load' in why
    assert LS.capacity(h(mem=None)) == (7, '')  # memory unreadable: the count and CPU still decide
    assert LS.capacity(h(claudes=None))[0] == 0


def test_host_reports_an_unreadable_memory():
    def bad():
        raise RuntimeError('no systemd')
    got = LS.host(mem=bad, counted=lambda: 2, loadavg=lambda: (1, 2, 3), cpus=lambda: 4)
    assert got['mem'] is None and 'no systemd' in got['mem_error'] and got['claudes'] == 2


def test_procs_reads_proc(tmp_path):
    d = tmp_path / '42'
    d.mkdir()
    (d / 'stat').write_text('42 (py (x)) S 7 ' + ' '.join(['0'] * 9) + ' 30 12 ' + ' '.join(['0'] * 8) + ' 25 0')
    (d / 'comm').write_text('python3\n')
    (d / 'cmdline').write_bytes(b'python3\0-m\0pytest\0')
    z = tmp_path / '43'
    z.mkdir()
    (z / 'stat').write_text('43 (dead) Z 1 ' + ' '.join(['0'] * 30))
    (tmp_path / 'self').mkdir()
    got = LS.procs(tmp_path, uid=os.getuid())
    assert got == {42: dict(ppid=7, comm='python3', cmd='python3 -m pytest', ticks=42, rss=25 * LS.PAGE)}


def test_sessions_board_cards_and_branch(tmp_path):
    s = tmp_path / 's'
    s.mkdir()
    (s / '5.json').write_text(json.dumps({'pid': 5, 'status': 'idle'}))
    (s / 'bad.json').write_text('{')
    assert LS.sessions(s) == {5: {'pid': 5, 'status': 'idle'}}
    snap = {'prs': [{'fieldValueByName': {'name': 'Ready'}, 'content': {
        '__typename': 'PullRequest', 'number': 7, 'state': 'OPEN', 'headRefName': 'feat',
        'repository': {'nameWithOwner': 'tangentcode/platform'}}}], 'issues': []}
    (tmp_path / 'snap.json').write_text(json.dumps(snap))
    assert LS.board_cards(tmp_path / 'snap.json') == {'feat': (7, 'Ready')}
    assert LS.board_cards(tmp_path / 'none.json') == {}
    wt, gd = tmp_path / 'wt', tmp_path / 'gd'
    wt.mkdir()
    gd.mkdir()
    (wt / '.git').write_text(f'gitdir: {gd}\n')
    (gd / 'HEAD').write_text('ref: refs/heads/agents-screen\n')
    assert LS.branch_of(wt) == 'agents-screen'
    assert LS.branch_of(tmp_path / 'nothing') == ''


def test_tmux_panes_parses_list_panes():
    out = '%1\t0\tevents\t11\t/home/u\nbad line\n%2\t3\tmn-1-x\t12\t/w/p\n'
    run = lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0)  # noqa: E731
    assert LS.tmux_panes(run=run) == [dict(pane='%1', index=0, window='events', pid=11, cwd='/home/u'),
                                      dict(pane='%2', index=3, window='mn-1-x', pid=12, cwd='/w/p')]


def sample():
    return LS.collect(now=NOW, panes=PANES, table=TABLE, sess=SESS, cards=CARDS, hst=h())


def test_collect_is_json_for_the_sanity_check(monkeypatch):
    monkeypatch.setattr(LS, 'branch_of', BRANCH.get)
    st = sample()
    back = json.loads(json.dumps(st))
    assert back['capacity'] == {'free': 7, 'why': ''}
    assert [r['window'] for r in back['agents']] == ['mn-1-busy', 'mn-2-idle', 'mn-3-shell']


def test_agents_screen_lines(monkeypatch):
    monkeypatch.setattr(LS, 'branch_of', BRANCH.get)
    lines = AV.render(sample(), color=False)
    assert lines[0].startswith('load 0.50 0.50 0.50 on 4 CPUs (holds at 6.0)') and 'Claudes 3/10' in lines[0]
    assert lines[1] == 'capacity: 7 start(s) free'
    body = '\n'.join(lines)
    assert 'mn-1-busy' in body and 'tests: pytest -q' in body and '#601' in body and 'In Progress' in body
    idle = next(l for l in lines if 'mn-2-idle' in l)
    assert 'idle 10m' in idle and 'Staging' in idle
    shell = next(l for l in lines if 'mn-3-shell' in l)
    assert 'no claude' in shell and '1 other: server.js' in shell
    assert lines[-1].startswith('3 panes; 2 with a Claude (1 active); 1 running tests')
    held = AV.render(dict(sample(), capacity=dict(free=0, why='10 Claudes run (cap 10)')), color=False)
    assert held[1] == 'capacity: none: 10 Claudes run (cap 10)'


def test_short_names():
    assert AV.short('timeout 600 /usr/bin/python3 -u -m pytest -q') == 'pytest -q'
    assert AV.short('bash /tmp/x/cw.sh 47') == 'cw.sh 47'
    assert AV.short('/home/u/bin/swarm-advance --dry-run') == 'swarm-advance --dry-run'
    assert AV.ago(59) == '59s' and AV.ago(600) == '10m' and AV.ago(7200) == '2h' and AV.ago(None) == ''


def test_swarm_events_agents_once_prints_the_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(LS, 'branch_of', BRANCH.get)
    out = io.StringIO()
    assert V.run(str(tmp_path / 'none.sqlite'), once=True, out=out, screen='agents', sample=sample) == 0
    assert out.getvalue().startswith('load 0.50') and 'mn-1-busy' in out.getvalue()


def test_swarm_events_keys_switch_screens_and_quit(monkeypatch, tmp_path):
    """#34 Step 2: in a terminal, a shows the Agents screen, e the events, q quits."""
    monkeypatch.setattr(LS, 'branch_of', BRANCH.get)
    from tangentswarm import eventdb
    st = eventdb.EventStore(tmp_path / 'e.sqlite')
    st.add_received('d1', 'ping', {'zen': 'x'}, {}, b'{}', True, decisions=['codex'], now=NOW - 5)
    presses = iter(['a', '', 'e', 'q'])
    out = io.StringIO()
    rc = V.run(str(tmp_path / 'e.sqlite'), since='all', out=out, tty=True, color=False, clock=lambda: NOW,
               keys=lambda t: next(presses), sample=sample, redraw=1000)
    frames = out.getvalue().split(V.CLEAR)[1:]
    assert rc == 0 and len(frames) == 3
    assert 'ping' in frames[0] and frames[1].startswith('load 0.50') and 'ping' in frames[2]


def test_admits_uses_the_cpu_load():
    m = dict(current=1000 * MB, high=4608 * MB, max=5120 * MB, avail=4 * 1024 ** 3)
    assert launches.admits(dict(m, load=[0, 1, 0], cpus=4))
    assert not launches.admits(dict(m, load=[0, 6.5, 0], cpus=4))


def many(n):
    st = sample()
    base = st['agents'][2]
    return dict(st, agents=[dict(base, index=i, window=f'w-{i}') for i in range(n)])


def test_agents_screen_scrolls_and_keeps_the_header_and_summary():
    """Codex on PR #48: with more panes than fit, every row must be reachable."""
    st = many(43)
    lines = AV.render(st, color=False, height=20)
    assert len(lines) == 20 and lines[1].startswith('capacity:')
    assert ' w-0 ' in lines[4] and 'rows 1-14 of 43' in lines[-1] and 'j/k' in lines[-1]
    last = AV.render(st, color=False, height=20, top=999)
    assert ' w-42' in last[-3] and 'rows 30-43 of 43' in last[-1]
    assert AV.clamp(999, 43, 20) == 29 and AV.clamp(-3, 43, 20) == 0 and AV.clamp(5, 3, 20) == 0
    short = AV.render(many(3), color=False, height=20)
    assert 'rows' not in short[-1] and len(short) == 4 + 3 + 2


def test_swarm_events_keys_scroll_the_agents_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(V.shutil, 'get_terminal_size', lambda *a: os.terminal_size((200, 21)))
    presses = iter(['a', ' ', 'k', '\x1b[6~\x1b[6~', 'b', 'q'])
    out = io.StringIO()
    st = many(43)
    V.run(str(tmp_path / 'e.sqlite'), out=out, tty=True, color=False, clock=lambda: NOW,
          keys=lambda t: next(presses), sample=lambda: st, redraw=1000)
    frames = [f.split('\n') for f in out.getvalue().split(V.CLEAR)[1:] if f.startswith('load')]
    tops = [next(l for l in f if 'rows' in l).split('rows ')[1].split('-')[0] for f in frames]
    assert tops == ['1', '15', '14', '30', '16']


def test_keys_work_before_the_event_db_exists(tmp_path):
    """Codex on PR #48: a terminal never blocks on a missing DB; a and q still work."""
    presses = iter(['', 'a', 'q'])
    slept = []
    out = io.StringIO()
    rc = V.run(str(tmp_path / 'none.sqlite'), out=out, tty=True, color=False, clock=lambda: NOW,
               keys=lambda t: next(presses), sample=lambda: many(2), redraw=1000, sleep=slept.append)
    frames = out.getvalue().split(V.CLEAR)[1:]
    assert rc == 0 and slept == [] and len(frames) == 2 and frames[1].startswith('load 0.50')


def test_key_list_reads_escape_sequences_as_one_key():
    assert V.key_list('a\x1b[6~j\x1b[Aq') == ['a', '\x1b[6~', 'j', '\x1b[A', 'q']
