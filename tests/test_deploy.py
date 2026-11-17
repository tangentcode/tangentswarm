"""tangentswarm #113: deploy tangentswarm main after every merge, tests first, rollback when
the unit check fails. A fake runner stands in for git, pytest and systemctl: no network."""
import json

from tangentswarm import deploy as D
from tangentswarm import promoted as P
from tangentswarm import webhook as W

OLD, NEW, MERGED = 'a' * 40, 'b' * 40, 'c' * 40
LIVE = '/live'


class Fake:
    """The commands of one deploy. tests/units/check: the result of pytest, systemctl
    is-active and swarm-unit-check, as a list (one per call; the last one repeats)."""

    def __init__(self, head=OLD, main=NEW, contains=None, dirty='', tests=(True,), active=('active',),
                 check=(True,)):
        self.head, self.main, self.dirty = head, main, dirty
        self.contains = contains or {(NEW, MERGED), (NEW, OLD)}  # (new, old): new contains old
        self.res = {'tests': list(tests), 'active': list(active), 'check': list(check)}
        self.calls, self.envs = [], []

    def pop(self, k):
        v = self.res[k]
        return v.pop(0) if len(v) > 1 else v[0]

    def __call__(self, argv, cwd=None, env=None, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.envs.append(env)
        if argv[0] == 'git':
            a = argv[3:]
            if a[0] == 'rev-parse':
                return 0, {'FETCH_HEAD^{commit}': self.main, 'HEAD^{commit}': self.head}[a[-1]]
            if a[0] == 'merge-base':
                return (0 if (a[-1], a[-2]) in self.contains else 1), ''
            if a[0] == 'status':
                return 0, self.dirty
            if a[0] == 'checkout':
                self.head = a[-1]
            return 0, ''
        if argv[1:3] == ['-m', 'pytest']:
            ok = self.pop('tests')
            return (0, '5 passed') if ok else (1, 'FAILED tests/test_x.py::test_y')
        if argv[:3] == ['systemctl', '--user', 'restart']:
            return 0, ''
        if argv[:3] == ['systemctl', '--user', 'is-active']:
            st = self.pop('active')
            return (0 if st == 'active' else 3), '\n'.join([st] * len(D.UNITS))
        if argv[0].endswith('swarm-unit-check'):
            return (0, 'ok') if self.pop('check') else (1, 'FAIL swarm-webhook')
        raise AssertionError(f"unexpected command {argv}")

    def did(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def git(self, verb):
        return [c[3:] for c in self.calls if c[0] == 'git' and c[3] == verb]


def deploy(fake, sha=MERGED, tmp_path=None):
    reports = []
    d = D.Deploy(sha, pr=120, live=LIVE, run=fake, sleep=lambda s: None, python='/venv/python',
                 report=lambda why, key, pr: reports.append((why, key, pr)) or 'reported',
                 scratch=tmp_path / 'scratch' if tmp_path else None)
    return d.run(), reports


def test_a_passing_merge_is_tested_then_deployed_and_the_four_units_restart(tmp_path):
    f = Fake()
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'deployed' and row['tests'] == 'pass' and row['unit_check'] == 'pass'
    assert row['prev'] == OLD and row['target'] == NEW and reports == []
    # the tests ran in a scratch worktree of the target, before the live checkout moved
    (add,) = [c for c in f.git('worktree') if c[1] == 'add']
    assert add[:3] == ['worktree', 'add', '--detach'] and add[-1] == NEW
    i_test = next(i for i, c in enumerate(f.calls) if c[1:3] == ['-m', 'pytest'])
    i_co = next(i for i, c in enumerate(f.calls) if c[0] == 'git' and c[3] == 'checkout')
    assert i_test < i_co and f.git('checkout') == [['checkout', '--quiet', '--detach', NEW]]
    assert f.did('systemctl', '--user', 'restart') == [['systemctl', '--user', 'restart', *D.UNITS]]
    assert D.UNITS == ('swarm-mcp-apikey', 'swarm-mcp-oauth', 'swarm-promoted', 'swarm-webhook')
    assert f.did(f'{LIVE}/bin/swarm-unit-check')  # the live checkout's own check
    # one log line: time, sha, test result, unit-check result
    (logged,) = D.read_log()
    assert logged['sha'] == MERGED and logged['at'] and logged['tests'] == 'pass' and logged['unit_check'] == 'pass'


def test_a_failing_merge_stays_undeployed_and_is_reported_once(tmp_path):
    f = Fake(tests=(False,))
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'not-deployed' and row['tests'] == 'fail' and row['unit_check'] is None
    assert f.git('checkout') == [] and f.did('systemctl') == []  # the services stay on the old sha
    assert f.head == OLD
    assert len(reports) == 1 and 'failed its tests' in reports[0][0] and reports[0][1] == f'deploy:{MERGED}'
    # the scratch tree is removed
    assert any(c[:2] == ['worktree', 'remove'] for c in f.git('worktree'))
    # a later pass or a redelivery of the same merge does not deploy or report it again
    assert D.tried(MERGED)
    assert D.REAL_START(MERGED, 120, run=lambda *a, **k: (_ for _ in ()).throw(AssertionError('started'))) \
        .startswith('no deploy')


def test_a_failing_unit_check_rolls_back_restarts_again_and_reports_the_sha(tmp_path):
    f = Fake(check=(False, True))
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'rolled-back' and row['unit_check'] == 'fail' and row['rollback_check'] == 'pass'
    assert f.git('checkout') == [['checkout', '--quiet', '--detach', NEW], ['checkout', '--quiet', '--detach', OLD]]
    assert f.head == OLD and len(f.did('systemctl', '--user', 'restart')) == 2
    assert len(reports) == 1 and NEW[:8] in reports[0][0] and 'Rolled back to ' + OLD[:8] in reports[0][0]


def test_a_unit_that_is_not_active_after_the_restart_also_rolls_back(tmp_path):
    f = Fake(active=('failed', 'active'))
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'rolled-back' and 'not active' in row['detail'] and f.head == OLD
    assert len(reports) == 1


def test_a_failed_rollback_is_reported_as_such(tmp_path):
    f = Fake(check=(False,))
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'rollback-failed' and row['rollback_check'] == 'fail' and len(reports) == 1


def test_a_live_head_that_has_main_already_is_not_deployed_again(tmp_path):
    f = Fake(head=NEW)
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'already-live' and f.did('systemctl') == [] and reports == []
    assert not any(c[1:3] == ['-m', 'pytest'] for c in f.calls)


def test_a_live_checkout_with_local_changes_is_never_touched(tmp_path):
    f = Fake(dirty=' M tangentswarm/x.py')
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'not-deployed' and f.git('checkout') == [] and f.did('systemctl') == []
    assert len(reports) == 1 and 'local changes' in reports[0][0]


def test_main_that_lacks_the_merged_sha_is_an_error_and_nothing_moves(tmp_path):
    f = Fake(contains={(NEW, OLD)})
    row, reports = deploy(f, tmp_path=tmp_path)
    assert row['result'] == 'error' and 'does not contain' in row['detail']
    assert f.git('checkout') == [] and f.did('systemctl') == [] and len(reports) == 1
    assert not D.tried(MERGED)  # an error is tried again by the next pass


def test_start_runs_the_deploy_in_its_own_unit_outside_the_swarm_units(tmp_path):
    seen = []
    note = D.REAL_START(MERGED, 120, run=lambda argv, timeout=None: seen.append(argv) or (0, ''))
    (argv,) = seen
    assert argv[:2] == ['systemd-run', '--user'] and f'--unit={D.UNIT_PREFIX}{MERGED[:12]}' in argv
    assert not D.UNIT_PREFIX.startswith('swarm')  # swarm-unit-check checks swarm*.service only
    assert argv[-6:] == ['tangentswarm.deploy', 'run', '--sha', MERGED, '--pr', '120'] and 'started' in note
    busy = D.REAL_START(MERGED, run=lambda argv, timeout=None: (1, 'Unit x.service was already loaded'))
    assert 'already running' in busy


def test_the_webhook_starts_a_deploy_for_a_tangentswarm_merge_only():
    merged = {'number': 120, 'base': {'ref': 'main'}, 'merged': True, 'merge_commit_sha': MERGED,
              'head': {'sha': NEW, 'ref': 'x', 'repo': {'full_name': 'tangentcode/tangentswarm'}}}
    acts = W.classify('pull_request', {'action': 'closed', 'repository': {'full_name': 'tangentcode/tangentswarm'},
                                       'pull_request': merged})
    dep = [a for a in acts if a.kind == 'deploy']
    assert len(dep) == 1 and dep[0].args['sha'] == MERGED and dep[0].args['repo'] == 'tangentcode/tangentswarm'
    for repo in ('tangentcode/methodology', 'tangentcode/platform'):
        acts = W.classify('pull_request', {'action': 'closed', 'repository': {'full_name': repo},
                                           'pull_request': merged})
        assert 'deploy' not in [a.kind for a in acts]
    unmerged = dict(merged, merged=False)
    acts = W.classify('pull_request', {'action': 'closed', 'repository': {'full_name': 'tangentcode/tangentswarm'},
                                       'pull_request': unmerged})
    assert 'deploy' not in [a.kind for a in acts]
    started = []
    ex = W.Executor(deploy_start=lambda sha, pr: started.append((sha, pr)) or 'started')
    ex.run(dep[0])
    assert started == [(MERGED, 120)] and ex.errors == []


def test_the_board_pass_starts_a_deploy_of_main_when_the_live_checkout_lacks_it(tmp_path, monkeypatch):
    live = tmp_path / 'live'
    live.mkdir()
    monkeypatch.setenv('SWARM_FLOW_LIVE', str(live))
    heads = {'main': NEW, 'HEAD': OLD}

    def run(argv, timeout=None):
        return 0, (f"{heads['main']}\trefs/heads/main" if 'ls-remote' in argv else heads['HEAD'])
    begun = []
    assert D.pass_check(run=run, begin=lambda sha: begun.append(sha) or 'begun') == 'begun' and begun == [NEW]
    D.write_log({'sha': NEW, 'result': 'not-deployed'})
    assert D.pass_check(run=run, begin=begun.append) is None and begun == [NEW]  # tried: not again
    heads['HEAD'] = heads['main'] = MERGED
    assert D.pass_check(run=run, begin=begun.append) is None  # live: nothing to do
    # swarm-promoted runs it at most once per PASS_EVERY s
    p = P.Promoter(g={})
    calls = []
    assert p.deploy_pass(now=1000, check=lambda: calls.append(1) or 'x') == 'x'
    assert p.deploy_pass(now=1000 + D.PASS_EVERY - 1, check=lambda: calls.append(1)) is None and calls == [1]


def test_the_log_command_prints_one_line_per_deploy(capsys):
    D.write_log({'at': 't', 'sha': NEW, 'tests': 'pass', 'unit_check': 'fail', 'result': 'rolled-back',
                 'detail': 'x'})
    assert D.main(['log']) == 0
    out = capsys.readouterr().out
    assert NEW[:8] in out and 'tests=pass' in out and 'unit_check=fail' in out and 'rolled-back' in out
    assert json.loads(D.log_path().read_text())['sha'] == NEW
