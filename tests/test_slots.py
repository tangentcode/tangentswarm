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
                                  'milestone': ({'title': kw['milestone'], 'dueOn': kw.get('due')}
                                                if kw.get('milestone') else None),
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


# -- (c) the launch picker: Backlog issues without a PR ---------------------------------

def test_launch_key_is_fast_track_then_nearest_due_date_then_number():
    """Michal 2026-11-15 12:31 AM ET: a deterministic sort, no LLM choice, no tokens."""
    issues = [
        dict(number=412),                                                    # no milestone
        dict(number=399),
        dict(number=510, milestone='ptw 15', due='2026-11-20T00:00:00Z'),
        dict(number=490, milestone='MVP', due='2026-12-15T00:00:00Z'),
        dict(number=505, milestone='ptw 15', due='2026-11-20T00:00:00Z'),
        dict(number=600, milestone='someday'),                               # milestone, no due date
        dict(number=700, labels=['Fast-Track']),                            # the label wins over all
        dict(number=650, ft=True),                                           # the active fast-track
    ]
    assert [i['number'] for i in sorted(issues, key=slots.launch_key)] == [650, 700, 505, 510, 490, 600, 399, 412]
    assert slots.launch_key(dict(number=1)) == slots.launch_key(dict(number=1))  # pure: same input, same key
    assert slots.key_text(issues[2]) == '#510=(ft:no,due:2026-11-20 ptw 15,n:510)'
    assert slots.key_text(issues[0]) == '#412=(ft:no,due:none,n:412)'
    assert slots.key_text(issues[5]) == '#600=(ft:no,due:none someday,n:600)'
    assert slots.key_text(issues[6]) == '#700=(ft:yes,due:none,n:700)'


def test_the_picker_takes_only_backlog_issues():
    """Michal 2026-11-15 12:16 AM ET: START_CLAUDE named Parked #344 (active_without_pr=1)
    while Backlog held the approved issues, so the tick launched nothing."""
    snap = {'issues': issue_nodes([
        (344, board.ACTIVE),                                   # Active: not the launch queue
        (499, board.BACKLOG), (412, board.BACKLOG),
        (505, board.BACKLOG, {'milestone': 'ptw 15', 'due': '2026-11-20T00:00:00Z'}),
        (490, board.BACKLOG, {'milestone': 'MVP', 'due': '2026-12-15T00:00:00Z'}),
        (377, board.BACKLOG),                                  # held
        (350, board.BACKLOG),                                  # parked by the handoff list
        (420, board.BACKLOG, {'labels': ['parked']}),
        (421, board.BACKLOG, {'labels': ['Triage']}),
        (430, board.TRIAGE), (431, board.PARKED), (432, board.INBOX), (433, board.DISCUSS),
        (434, board.BACKLOG),                                  # an open PR closes it
        (435, board.BACKLOG, {'repo': 'tangentcode/other'}), (436, board.BACKLOG, {'state': 'CLOSED'}),
        (507, board.BACKLOG, {'labels': ['fast-track']})]),
        'prs': [pr_node(90, [434])]}
    got = slots.backlog_issues(snap, active_ft=None, prs={}, now=0)
    assert [i['number'] for i in got] == [507, 505, 490, 412, 499]
    ft = [i['number'] for i in slots.backlog_issues(snap, active_ft=499, prs={}, now=0)]
    assert ft[:2] == [499, 507]  # both fast-track: the lower number first


def test_a_named_issue_is_not_named_again_until_the_grace_ends(tmp_path):
    snap = {'issues': issue_nodes([(412, board.BACKLOG), (413, board.BACKLOG)]), 'prs': []}
    path = tmp_path / 'slots.json'
    slots.mark_named([412], path=path, now=1000)
    named = slots.load(path)['named']
    assert [i['number'] for i in slots.backlog_issues(snap, named=named, now=1000 + 60)] == [413]
    assert [i['number'] for i in slots.backlog_issues(snap, named=named, now=1000 + slots.LAUNCH_GRACE)] == [412, 413]
    slots.mark_named([413], path=path, now=1000 + slots.LAUNCH_GRACE)  # drops the old entry
    assert set(slots.load(path)['named']) == {'413'}


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


def test_a_rework_claude_back_in_ready_is_parked_even_without_a_new_head():
    """MethBot 2026-11-15 3:35 AM ET: a rework Claude kept its slot because it pushed no new
    head, and rework=3/3 held #516 with NEEDS_WORKER."""
    slots.save({'parked': {}, 'rework': {'7': dict(worktree='/w/b7', session='s7', head='7' * 40,
                                                   feedback='Coordinator: #7 conflicts with work'),
                                         '5': dict(worktree='/w/b5', head='5' * 40)}})
    out, cl = [], [claude(1, '/w/b7', 'b7', 's7')]
    assert slots.park_ready(PRS, COLS, out=out.append, claudes=cl, park=lambda c, say: None) == [7]
    st = slots.load()
    assert st['rework'] == {} and st['parked']['7']['head'] == '7' * 40  # #5 is no longer open: forgotten


def test_a_screenshot_job_keeps_its_claude_until_the_body_has_an_image():
    """#519: revived for screenshots while its card stayed in Ready; it is parked once the
    body has an image (the reason is gone), not before."""
    text = 'Coordinator: #7 needs screenshots before Human Review. Take them from staging.'
    slots.save({'parked': {}, 'rework': {'7': dict(worktree='/w/b7', session='s7', head='7' * 40,
                                                   feedback=text, reason=slots.revive_reason(text))}})
    out, cl = [], [claude(1, '/w/b7', 'b7', 's7')]
    assert slots.park_ready(PRS, COLS, out=out.append, claudes=cl, park=lambda c, say: None) == []
    assert out == ['park: #7 is in rework and its PR body has no screenshot yet: it keeps running']
    prs = {**PRS, 7: dict(PRS[7], body='![shot](https://github.com/user-attachments/assets/0123456789abcdef0123)')}
    assert slots.park_ready(prs, COLS, out=out.append, claudes=cl, park=lambda c, say: None) == [7]


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


def test_idle_workers_in_staging_and_human_review_are_parked_too():
    """MethBot 2026-11-15 4:13 AM ET: #531 and #532 were in Staging with idle Claudes since
    about 3:00 and held two of the cap slots, because park looked only at Ready."""
    cols = {board.STAGING: [dict(number=7)], board.HUMAN_REVIEW: [dict(number=8)], board.IN_PROGRESS: [dict(number=9)]}
    cl = [dict(claude(1, '/w/b7', 'b7', 's7', '%7'), status='idle'), dict(claude(2, '/w/b8', 'b8', 's8', '%8'), status='busy'),
          dict(claude(3, '/w/b9', 'b9', 's9', '%9'), status='idle')]
    out = []
    assert slots.park_ready(PRS, cols, out=out.append, claudes=cl, park=lambda c, say: None) == [7]
    assert 'park: #7 parked; session s7 kept for swarm-revive 7' in out  # #8 is busy, #9 is still in work


def test_the_cap_counts_workers_and_busy_claudes_but_not_idle_others(tmp_path, monkeypatch):
    d = tmp_path / 'sessions'
    d.mkdir()
    rows = [(1, '/home/u/ver/platform.a', 'idle'), (2, '/home/u/ver/platform.b', 'busy'),
            (3, '/home/u/ver/implish.rtl-eval', 'idle'), (4, '/home/u/ver/tangentswarm.x', 'busy'),
            (5, '/home/u/ver/other', 'shell')]
    for pid, cwd, st in rows:
        (d / f'{pid}.json').write_text(json.dumps({'pid': pid, 'cwd': cwd, 'status': st}))
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', Path('/home/u/ver'))
    assert slots.live_claudes(sessions=d, alive=lambda pid: True) == 4  # not the idle implish Claude


def test_a_refused_park_names_the_leftover_process_to_the_worker_once():
    told, out = [], []
    left = [(2790171, '.venv/bin/python tcode-server.py --port 5391')]
    for _ in range(2):
        slots.relay_leftovers(532, '/w/b532', out=out.append, find=lambda cwd: left,
                              tell=lambda n, text: told.append((n, text)))
    assert len(told) == 1 and 'pid 2790171' in told[0][1]
    assert out == ['park: #532: asked its worker to stop 1 leftover process(es): 2790171']
    left.append((2790200, 'node vite'))
    slots.relay_leftovers(532, '/w/b532', out=out.append, find=lambda cwd: left,
                          tell=lambda n, text: told.append((n, text)))
    assert len(told) == 2  # a new set of pids is told again


def test_idle_non_pr_workers_are_parked_but_never_a_coordinator(tmp_path, monkeypatch):
    """Michal 2026-11-15 4:40 AM ET: idle agents get /exit; the window stays (imp-22-rtl-eval)."""
    d = tmp_path / 'sessions'
    d.mkdir()
    now = 100_000.0
    rows = [(1, '/home/u/ver/implish.rtl-eval', 'idle', '%151', now - 3 * 3600),   # imp-22: parked
            (2, '/home/u/ver/tangentswarm.auto', 'idle', '%9', now - 3 * 3600),    # meth-26: coordinator
            (3, '/home/u/ver/implish.other', 'idle', '%7', now - 60),             # idle 1 min: kept
            (4, '/home/u/ver/platform.x', 'idle', '%5', now - 3 * 3600),          # PR worker: by its card
            (5, '/home/u/ver/implish.busy', 'busy', '%4', now - 3 * 3600)]
    for pid, cwd, st, pane, at in rows:
        (d / f'{pid}.json').write_text(json.dumps({'pid': pid, 'cwd': cwd, 'status': st, 'sessionId': f's{pid}',
                                                   'tmux': f'agents:@1.{pane}', 'statusUpdatedAt': at * 1000}))
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', Path('/home/u/ver'))
    windows = {'%151': 'imp-22-rtl-eval', '%9': 'meth-26-auto-coordinator-1', '%7': 'imp-23-other',
               '%5': 'mn-5-x', '%4': 'imp-24-busy'}
    found = slots.idle_others(sessions=d, alive=lambda pid: True, now=now, windows=windows)
    assert [c['window'] for c in found] == ['imp-22-rtl-eval'] and found[0]['idle_s'] == 3 * 3600
    out, exited = [], []
    assert slots.park_idle_others(out=out.append, found=found, park=lambda c, say: exited.append(c['pane'])) == \
        ['imp-22-rtl-eval']
    assert exited == ['%151'] and slots.load()['parked']['imp-22-rtl-eval']['session'] == 's1'
    assert out == ['park: imp-22-rtl-eval parked after 180 min idle; session s1 kept; its window stays']
    lines = []
    slots.report(out=lines.append)  # a window-keyed entry does not break the report
    assert any(l.startswith('parked imp-22-rtl-eval') for l in lines)


def _writer(path, base, n):
    from tangentswarm import slots as S
    for i in range(n):
        S.unstarted_add(base + i, path=path, branch='b')


def test_concurrent_writers_never_lose_a_record(tmp_path):
    """Codex on PR #26: the spool's launchers and the cascade wrote the state at the same
    time with no lock; one could drop another's unstarted or parked record."""
    import multiprocessing
    from tangentswarm import slots as S
    path = tmp_path / 'slots.json'
    ctx = multiprocessing.get_context('fork')
    procs = [ctx.Process(target=_writer, args=(path, k * 100, 25)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    assert len(S.load(path)['unstarted']) == 100
    assert not list(tmp_path.glob('*.tmp'))


def test_revive_holds_a_new_claude_at_the_global_cap_but_still_tells_a_running_one(wt, monkeypatch):
    """Codex on PR #26: a resume started a Claude with MAX_CLAUDES already running."""
    monkeypatch.setattr(slots, 'live_claudes', lambda *a, **k: slots.MAX_CLAUDES)
    out, host = [], FakeHost()
    assert slots.revive(7, 'go', out=out.append, sp=FakeSP(wt), host=host, claudes=[]) == 3
    assert not host.typed and 'cap' in out[-1]
    host = FakeHost()
    rc = slots.revive(7, 'more', out=lambda s: None, sp=FakeSP(wt, live={'pane': '%5', 'agent': 'claude'}),
                      host=host, claudes=[claude(1, wt, 'b7', 's7')])
    assert rc == 0 and host.told == [('%5', 'more')]


def test_a_revive_is_one_events_row(wt):
    """tangentswarm #32: resuming an agent and feedback to a running one are logged."""
    from tangentswarm import eventdb
    host = FakeHost()
    slots.revive(7, 'more', out=lambda s: None, sp=FakeSP(wt, live={'pane': '%5', 'agent': 'claude'}),
                 host=host, claudes=[claude(1, wt, 'b7', 's7')])
    (r,) = eventdb.store().rows("source='slots'")
    assert r['event'] == 'feedback' and r['action'] == 'done' and r['pr'] == 7


def test_claude_load_counts_live_claudes_and_spool_starts_only(monkeypatch):
    """tangentswarm #44: no reservations, no held list; the spool requests are live work."""
    from tangentswarm import spool
    monkeypatch.setattr(slots, 'live_claudes', lambda *a, **k: 3)
    slots.save({'launching': {'5': __import__('time').time()}, 'unstarted': {'601': {'branch': 'b'}}})
    assert slots.claude_load() == 3
    spool.submit('launch', '', issue=40)
    spool.submit('start', '', pr=601)
    spool.submit('handoff', '/w')
    assert slots.claude_load() == 5
    assert slots.capacity() == (slots.MAX_CLAUDES - 5, '')
    assert slots.capacity(mine=1) == (slots.MAX_CLAUDES - 4, '')
