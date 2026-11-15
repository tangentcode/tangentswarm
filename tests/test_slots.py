import json
from pathlib import Path

import pytest

from tangentswarm import board, slots


def issue_nodes(issues):
    """Snapshot issue nodes. issues: (number, column[, {labels, milestone, repo, state}])."""
    nodes = []
    for t in issues:
        n, col, kw = t[0], t[1], (t[2] if len(t) > 2 else {})
        nodes.append({'id': f'I{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]},
                      'content': {'__typename': 'Issue', 'number': n, 'title': f'issue {n}',
                                  'state': kw.get('state', 'OPEN'),
                                  'repository': {'nameWithOwner': kw.get('repo', slots.REPO)},
                                  'milestone': {'title': kw['milestone']} if kw.get('milestone') else None,
                                  'labels': {'nodes': [{'name': l} for l in kw.get('labels', ())]}}})
    return nodes


def board_items(issues):
    """The boardcache snapshot answer for these issues (no PRs)."""
    from boardfake import answer
    return answer(issue_nodes(issues))


def pr_node(n, closes, col=board.IN_PROGRESS):
    return {'id': f'P{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]},
            'content': {'__typename': 'PullRequest', 'number': n, 'state': 'OPEN',
                        'repository': {'nameWithOwner': slots.REPO},
                        'closingIssuesReferences': {'nodes': [{'number': i} for i in closes]}}}


# -- (c) Active issues without a PR, in Ready order -------------------------------------

def test_active_issues_without_a_pr_in_ready_order():
    snap = {'issues': issue_nodes([
        (30, board.ACTIVE), (21, board.ACTIVE), (25, board.ACTIVE, {'milestone': 'ptw 15'}),
        (27, board.ACTIVE),                                    # open PR #90 on the board closes it
        (29, board.ACTIVE, {'labels': ['parked']}),
        (31, board.BACKLOG), (32, board.ACTIVE, {'repo': 'tangentcode/other'}),
        (33, board.ACTIVE, {'state': 'CLOSED'}),
        (34, board.ACTIVE),                                    # a PR from prs closes it
        (28, board.ACTIVE), (40, board.ACTIVE)]),
        'prs': [pr_node(90, [27])]}
    got = slots.active_issues(snap, active_ft=40, prs={95: {'closes': [34]}})
    assert [i['number'] for i in got] == [40, 25, 21, 28, 30]  # fast-track, priority milestone, oldest


@pytest.mark.parametrize('new, issues, ready, want', [
    (0, 19, 7, (3, 3)),   # Ready full: 3 new-work slots
    (2, 19, 7, (1, 3)),
    (3, 19, 7, (0, 3)),
    (3, 19, 2, (1, 4)),   # Ready short: a 4th slot
    (0, 2, 7, (2, 3)),    # never more than the issues waiting
    (5, 19, 7, (0, 3)),
])
def test_start_plan(new, issues, ready, want):
    assert slots.start_plan(new, [{}] * issues, ready, 6) == want


# -- running Claudes ----------------------------------------------------------------

def session(tmp_path, pid, cwd, sid, tmux='agents:@1.%9', status='idle'):
    d = tmp_path / 'claude-sessions'
    d.mkdir(exist_ok=True)
    (d / f'{pid}.json').write_text(json.dumps(dict(pid=pid, sessionId=sid, cwd=str(cwd), tmux=tmux, status=status)))


def test_running_lists_only_live_claudes_in_platform_worktrees(tmp_path, monkeypatch):
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', tmp_path / 'ver')
    ver = tmp_path / 'ver'
    session(tmp_path, 11, ver / 'platform.a', 's-a', tmux='agents:@3.%30')
    session(tmp_path, 12, ver / 'platform.b', 's-b')          # dead
    session(tmp_path, 13, ver / 'platform', 's-main')         # the main checkout
    session(tmp_path, 14, ver / 'tangentswarm.x', 's-x')
    session(tmp_path, 15, ver / 'platform.c', 's-c', tmux='other:@1.%2')
    got = slots.running(alive=lambda pid: pid != 12, branch_of=lambda cwd: Path(cwd).name[9:])
    assert [(c['pid'], c['session'], c['pane'], c['branch']) for c in got] == [
        (11, 's-a', '%30', 'a'), (15, 's-c', '', 'c')]


def test_kinds_split_rework_from_new_work():
    claudes = slots.attach_prs([dict(cwd='/w/a', session='s1', branch='a'), dict(cwd='/w/b', session='s2', branch='b'),
                                dict(cwd='/w/c', session='s3', branch='c')],
                               {7: {'branch': 'a'}, 8: {'branch': 'b'}, 9: {'branch': 'c', 'col': board.READY}})
    k = slots.kinds(claudes, {'rework': {'8': {'worktree': '/w/b', 'session': 'old'}}})
    assert [c['pr'] for c in k['new']] == [7] and [c['pr'] for c in k['rework']] == [8]
    assert [c['pr'] for c in k['done']] == [9]  # its PR is in Ready: no new-work slot


def test_latest_session_reads_the_claude_project_dir(tmp_path):
    d = tmp_path / 'claude-projects' / '-home-u-ver-platform-x-y'
    d.mkdir(parents=True)
    import os
    for i, sid in enumerate(('old', 'new')):
        (d / f'{sid}.jsonl').write_text('{}')
        os.utime(d / f'{sid}.jsonl', (1000 + i, 1000 + i))
    assert slots.latest_session('/home/u/ver/platform.x-y') == 'new'
    assert slots.latest_session('/home/u/ver/platform.none') is None


# -- (a) park the Claude of a PR in Ready ----------------------------------------------------

def claude(pid, wt, branch, sid, pane='%1'):
    return dict(pid=pid, cwd=wt, branch=branch, session=sid, pane=pane, status='idle')


PRS = {7: dict(branch='b7', head='7' * 40, state='OPEN'), 8: dict(branch='b8', head='8' * 40, state='OPEN'),
       9: dict(branch='b9', head='9' * 40, state='OPEN')}
COLS = {board.READY: [dict(number=7), dict(number=8)], board.IN_PROGRESS: [dict(number=9)]}


def test_park_records_the_session_then_parks_only_ready_prs():
    parked, out = [], []

    def park(c, say):
        assert slots.load()['parked'][str(c['pr'])]['session'] == c['session']  # recorded before the /exit
        c['window'] = f"mn-{c['pr']}-x"
        parked.append(c['pr'])
    cl = [claude(1, '/w/b7', 'b7', 's7', '%7'), claude(2, '/w/b9', 'b9', 's9', '%9'),
          claude(3, '/w/b8', 'b8', 's8', '')]  # #8 runs outside the agents session
    assert slots.park_ready(PRS, COLS, out=out.append, claudes=cl, park=park) == [7]
    assert parked == [7]
    rec = slots.load()['parked']['7']
    assert (rec['session'], rec['window'], rec['worktree'], rec['head']) == ('s7', 'mn-7-x', '/w/b7', '7' * 40)
    assert 'park: #8 Claude pid 3 is not in the agents tmux session: not parked' in out


def test_park_dry_run_changes_nothing():
    out = []
    slots.park_ready(PRS, COLS, dry=True, out=out.append, claudes=[claude(1, '/w/b7', 'b7', 'abcdef123')],
                     park=lambda c, say: pytest.fail('a dry run parked'))
    assert out == ['park: would park #7 (pane %1, session abcdef12): its PR is in Ready']
    assert not slots.STATE.exists()


def test_a_refused_park_keeps_the_claude_and_the_session_record():
    out = []
    slots.park_ready(PRS, COLS, out=out.append, claudes=[claude(1, '/w/b7', 'b7', 's7')],
                     park=lambda c, say: 'mn-7-x: prompt is not empty')
    assert out == ['park: #7 not parked: mn-7-x: prompt is not empty']
    assert slots.load()['parked']['7']['session'] == 's7'


def test_a_rework_claude_is_parked_only_after_it_pushes_a_new_head():
    slots.save({'parked': {}, 'rework': {'7': dict(worktree='/w/b7', session='s7', head='7' * 40),
                                         '5': dict(worktree='/w/b5', head='5' * 40)}})
    out, cl = [], [claude(1, '/w/b7', 'b7', 's7')]
    assert slots.park_ready(PRS, COLS, out=out.append, claudes=cl, park=lambda c, say: None) == []
    assert out == ['park: #7 is in rework and has pushed no new head since 77777777: it keeps running']
    assert set(slots.load()['rework']) == {'7'}  # #5 is no longer open: forgotten
    prs = {**PRS, 7: dict(PRS[7], head='a' * 40)}
    assert slots.park_ready(prs, COLS, out=out.append, claudes=cl, park=lambda c, say: None) == [7]
    st = slots.load()
    assert st['rework'] == {} and st['parked']['7']['head'] == 'a' * 40


# -- (b) revive ------------------------------------------------------------------------

class Refused(Exception):
    pass


class FakeSP:
    """The swarm_parking surface that revive uses."""
    Refused = Refused
    SESSION, SLICE, MB = 'agents', 'swarm-workers.slice', 1024 ** 2
    RESUME_PARK_NEED_MB, RESUME_MIN_MB = 450, 400

    def __init__(self, wt, live=None, room=2000):
        self.wt, self.live, self.room, self.removed = wt, live, room, []
        import contextlib
        self.flocked = lambda name, blocking=True: contextlib.nullcontext(True)

    def resolve(self, host, key):
        return 'mn-7-b7', self.wt, 'b7', {'window': 'mn-7-b7'}

    def resume_lock(self, w):
        return 'resume-' + w

    def live_pane(self, host, w):
        return self.live

    def idle_shell_problem(self, host, p, wt):
        return None

    def scoped_command(self, window, resume=True):
        assert resume is False
        return ['XDG_RUNTIME_DIR=/run/user/1', 'systemd-run', '--user', '--scope', '--unit=u1', '--', 'claude'], 'u1'

    def launch_line(self, argv):
        return ' '.join(argv)

    def headroom(self, host):
        return self.room * self.MB

    def shell_for(self, host, window, wt, existing=None):
        return (existing or {}).get('pane', '%50'), bool(existing)

    def wait_ready(self, host, pane, unit):
        return True, ''

    def remove_window(self, w):
        self.removed.append(w)


class FakeHost:
    def __init__(self):
        self.typed, self.told = [], []

    def type_line(self, pane, line):
        self.typed.append((pane, line))

    def tell(self, pane, text):
        self.told.append((pane, text))

    def run_park(self, mb):
        pass


@pytest.fixture
def wt(tmp_path, monkeypatch):
    import subprocess
    w = tmp_path / 'platform.b7'
    subprocess.run(['git', 'init', '-q', '-b', 'b7', str(w)], check=True)
    subprocess.run(['git', '-C', str(w), '-c', 'user.name=t', '-c', 'user.email=t@e', 'commit', '-q',
                    '--allow-empty', '-m', 'x'], check=True)
    return str(w)


def test_revive_resumes_the_recorded_session_and_sends_the_feedback(wt):
    slots.save({'parked': {'7': dict(session='sid-7', worktree=wt)}, 'rework': {}})
    sp, host, out = FakeSP(wt, live={'pane': '%50', 'agent': None}), FakeHost(), []
    rc = slots.revive('#7', 'Fix the\nlabel   text.', out=out.append, sp=sp, host=host, claudes=[])
    assert rc == 0
    assert host.typed == [('%50', 'XDG_RUNTIME_DIR=/run/user/1 systemd-run --user --scope --unit=u1 -- claude '
                                  '--resume sid-7')]
    assert host.told == [('%50', 'Fix the label text.')]  # one line
    st = slots.load()
    assert st['parked'] == {} and st['rework']['7']['session'] == 'sid-7' and st['rework']['7']['worktree'] == wt
    assert sp.removed == ['mn-7-b7']


def test_revive_falls_back_to_the_newest_session_of_the_worktree(wt):
    d = slots.PROJECTS / slots.re.sub(r'[^A-Za-z0-9]', '-', wt)
    d.mkdir(parents=True)
    (d / 'sid-latest.jsonl').write_text('{}')
    host = FakeHost()
    assert slots.revive(7, 'go', out=lambda s: None, sp=FakeSP(wt), host=host, claudes=[]) == 0
    assert host.typed[0][1].endswith('--resume sid-latest')


def test_revive_is_held_when_the_rework_slots_are_full(wt, monkeypatch):
    monkeypatch.setattr(slots, 'REWORK_SLOTS', 2)
    slots.save({'parked': {}, 'rework': {'1': dict(worktree='/w/1'), '2': dict(worktree='/w/2')}})
    out, host = [], FakeHost()
    cl = [claude(1, '/w/1', 'b1', 's1'), claude(2, '/w/2', 'b2', 's2')]
    assert slots.revive(7, 'go', out=out.append, sp=FakeSP(wt), host=host, claudes=cl) == 3
    assert out == ['held: the 2 rework slots are in use (#1, #2); try again when one is parked'] and not host.typed


def test_revive_of_a_running_claude_only_sends_the_feedback(wt):
    host = FakeHost()
    rc = slots.revive(7, 'more', out=lambda s: None, sp=FakeSP(wt, live={'pane': '%5', 'agent': 'claude'}),
                      host=host, claudes=[claude(1, wt, 'b7', 's7')])
    assert rc == 0 and host.typed == [] and host.told == [('%5', 'more')]
    assert slots.load()['rework']['7']['pane'] == '%5'


def test_revive_dry_run_and_empty_feedback(wt):
    out, host = [], FakeHost()
    assert slots.revive(7, '  ', out=out.append, sp=FakeSP(wt), host=host) == 1
    assert out == ['refused: the feedback is empty']
    assert slots.revive(7, 'go', dry=True, out=out.append, sp=FakeSP(wt), host=host, claudes=[]) == 0
    assert out[-2].startswith('would resume #7 in mn-7-b7 (no session id found: claude --continue)')
    assert not host.typed and not host.told and not slots.STATE.exists()


def test_revive_is_held_without_memory(wt):
    out = []
    assert slots.revive(7, 'go', out=out.append, sp=FakeSP(wt, room=100), host=FakeHost(), claudes=[]) == 3
    assert out[-1].startswith('held: mn-7-b7 needs 400 MB')


def test_make_host_takes_the_tmux_modules_from_the_deployed_checkout(tmp_path, monkeypatch):
    pkg = tmp_path / 'srv' / 'tangentswarm'
    pkg.mkdir(parents=True)
    (pkg / '__init__.py').write_text('')
    (pkg / 'tmux.py').write_text('WHERE = "srv"\ndef run_checked(*a): pass\n')
    (pkg / 'registry.py').write_text('from . import tmux\ndef shell_problem(*a): return tmux.WHERE\n')
    (pkg / 'agents.py').write_text('from . import registry, tmux\n')
    monkeypatch.setattr(slots, 'TMUX_SRC', tmp_path / 'srv')
    monkeypatch.setattr(slots, 'TMUX_PKG', 'swarm_tmux_test')

    class SP:
        RUNTIME_DIR = '/run/user/1'

        class Host:
            def __init__(self):
                raise AssertionError('the default Host would import the caller checkout')
    h = slots.make_host(SP)
    assert h.registry.shell_problem() == 'srv' and h.env['XDG_RUNTIME_DIR'] == '/run/user/1'
    monkeypatch.setattr(slots, 'TMUX_SRC', tmp_path / 'none')
    with pytest.raises(AssertionError):
        slots.make_host(SP)
