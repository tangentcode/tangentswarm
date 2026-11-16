"""The pane watcher (tangentswarm #40, Michal 2026-11-15 6:38 PM ET)."""
import subprocess

from tangentswarm import panewatch as PW

PANE = dict(id='%5', window='mn-540-search', command='claude', path='/w/platform.search')
IDLE = 'Done: pushed 1a2b3c4d.\n\n> \n'
BUSY = '* Working... (12s, esc to interrupt)\n> \n'


def test_a_changing_screen_is_working_and_a_quiet_one_is_classified():
    r1 = PW.observe(None, PANE, BUSY, True, True, 1000)
    assert r1['cls'] == 'working' and r1['pr'] == 540
    r2 = PW.observe(r1, PANE, BUSY.replace('12s', '13s'), True, True, 1060)  # the timer moved
    assert r2['cls'] == 'working' and r2['changed_at'] == 1060
    r3 = PW.observe(r2, PANE, BUSY.replace('12s', '13s'), True, True, 1060 + PW.QUIET)
    assert r3['cls'] == 'quiet'  # busy line, unchanged a minute: not yet frozen
    r4 = PW.observe(r3, PANE, BUSY.replace('12s', '13s'), True, True, 1060 + PW.FROZEN)
    assert r4['cls'] == 'frozen'
    i1 = PW.observe(None, PANE, IDLE, True, True, 2000)
    assert PW.observe(i1, PANE, IDLE, True, True, 2000 + PW.QUIET)['cls'] == 'idle'
    assert PW.observe(None, PANE, IDLE, True, False, 2000)['cls'] == 'shell'


def test_actions_once_per_screen():
    rec = dict(PW.observe(None, PANE, IDLE, False, True, 0), changed_at=0)
    rec = PW.observe(rec, PANE, IDLE, False, True, PW.QUIET)  # unsent text at the prompt
    assert rec['cls'] == 'input' and PW.actions(rec) == [('enter', None)]
    assert PW.actions(dict(rec, acted=rec['hash'])) == []
    idle = PW.observe(PW.observe(None, PANE, IDLE, True, True, 0), PANE, IDLE, True, True, PW.QUIET)
    assert PW.actions(idle) == [] and PW.actions(idle, 'In Progress') == [('stophook', '/w/platform.search')]
    assert PW.actions(idle, 'Auto Review') == []


class Fake:
    def __init__(self, screens, cols=None):
        self.screens, self.calls, self.submits, self.wakes, self.lines = screens, [], [], [], []
        self.cols = cols or {}

    def sh(self, argv, timeout=None, check=False):
        self.calls.append(argv)
        out = '\n'.join('\t'.join([p['id'], p['window'], p['command'], p['path'], '0']) for p in self.screens) \
            if argv[:2] == ['tmux', 'list-panes'] else ''
        return subprocess.CompletedProcess(argv, 0, out, '')


def test_a_scan_acts_on_a_missed_stop_hook_and_an_asking_pane(tmp_path):
    asking = dict(PANE, id='%6', window='mn-541-x', path='/w/platform.x')
    screens = {'%5': (IDLE, True), '%6': ('Should I use the old colors or the new ones?\n> \n', True)}
    f = Fake([PANE, asking])
    w = PW.PaneWatch(sh=f.sh, capture=lambda pid: screens[pid], is_claude=lambda pid, cmd: True,
                     cols=lambda: {540: 'In Progress', 541: 'In Progress'},
                     submit=lambda op, cwd, transcript=None: f.submits.append((op, cwd, transcript)),
                     transcript=lambda wt: wt + '/t.jsonl',
                     wake=lambda pr, why, key: f.wakes.append((pr, why)), path=tmp_path / 'panes.json', out=f.lines.append)
    w.scan(now=1000)
    assert f.submits == [] and f.wakes == []  # changed this minute: working
    st = w.scan(now=1000 + PW.QUIET)
    assert f.submits == [('stophook', '/w/platform.search', '/w/platform.search/t.jsonl')]  # with its transcript
    assert f.wakes and f.wakes[0][0] == 541 and 'asks in its pane' in f.wakes[0][1]
    assert st['%5']['cls'] == 'idle' and st['%6']['cls'] == 'asking'
    w.scan(now=1000 + 2 * PW.QUIET)  # the same screens: no second action
    assert len(f.submits) == 1 and len(f.wakes) == 1


def test_no_transcript_means_no_handoff_and_a_board_outage_spares_the_other_panes(tmp_path):
    """Codex on PR #52: the Stop hook's safety check needs the transcript; one board failure
    must not stop the scan."""
    asking = dict(PANE, id='%6', window='mn-541-x', path='/w/platform.x')
    screens = {'%5': (IDLE, True), '%6': ('Should I use the old colors or the new ones?\n> \n', True)}
    f = Fake([PANE, asking])

    def boom():
        raise RuntimeError('GraphQL down')
    w = PW.PaneWatch(sh=f.sh, capture=lambda pid: screens[pid], is_claude=lambda pid, cmd: True, cols=boom,
                     submit=lambda *a, **k: f.submits.append(a), wake=lambda pr, why, key: f.wakes.append((pr, why)),
                     path=tmp_path / 'panes.json', out=f.lines.append, transcript=lambda wt: None)
    w.scan(now=1000)
    st = w.scan(now=1000 + PW.QUIET)
    assert st['%6']['cls'] == 'asking' and any(pr == 541 for pr, _ in f.wakes)  # the other pane still acted
    assert any('board read failed' in l for l in f.lines) and f.submits == []
    w2 = PW.PaneWatch(sh=f.sh, capture=lambda pid: screens[pid], is_claude=lambda pid, cmd: True,
                      cols=lambda: {540: 'In Progress'}, submit=lambda *a, **k: f.submits.append(a),
                      wake=lambda pr, why, key: f.wakes.append((pr, why)), path=tmp_path / 'p2.json',
                      out=f.lines.append, transcript=lambda wt: None)
    w2.scan(now=1000)
    w2.scan(now=1000 + PW.QUIET)
    assert f.submits == [] and any('no transcript' in why for _, why in f.wakes)
