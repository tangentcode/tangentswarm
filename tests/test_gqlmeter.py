import json
import subprocess

import pytest

from tangentswarm import advance as A, gqlmeter as M, promoted as P

REAL_SCHEDULE = A.schedule  # conftest stubs A.schedule for every other test


@pytest.fixture(autouse=True)
def fresh_tally(monkeypatch):
    monkeypatch.setattr(M, 'TALLY', {'calls': 0, 'points': 0, 'remaining': None, 'reset': None, 'by': {}})


def test_inject_adds_rate_limit_to_queries_only():
    q = 'query($c:String){organization(login:"x"){id}}'
    assert M.inject(q) == 'query($c:String){rateLimit{cost remaining resetAt} organization(login:"x"){id}}'
    assert M.inject('mutation{a}') == 'mutation{a}'


def test_call_counts_points_and_takes_rate_limit_out():
    def sh(*args, check=True):
        assert 'rateLimit{cost remaining resetAt}' in args[4]
        return subprocess.CompletedProcess(args, 0, json.dumps({'data': {
            'rateLimit': {'cost': 6, 'remaining': 4000, 'resetAt': '2026-11-15T03:00:00Z'}, 'x': 1}}), '')
    assert M.call(sh, 'query{x}', {'c': None}) == {'x': 1}
    assert M.call(sh, 'query{x}', {}) == {'x': 1}
    assert (M.TALLY['calls'], M.TALLY['points'], M.TALLY['remaining']) == (2, 12, 4000)
    lines = M.LOG.read_text().splitlines()
    assert len(lines) == 2 and lines[0].split('\t')[3] == '6'
    s = M.summary(lines)
    assert sum(v[1] for by in s.values() for v in by.values()) == 12


def test_a_rate_limit_error_raises_with_the_reset_time(monkeypatch):
    monkeypatch.setattr(M, 'reset_time', lambda: 1794709000.0)

    def sh(*args, check=True):
        return subprocess.CompletedProcess(args, 1, '', 'gh: API rate limit already exceeded for installation ID 1.')
    with pytest.raises(M.RateLimited) as e:
        M.call(sh, 'query{x}', {})
    assert e.value.reset == 1794709000.0 and isinstance(e.value, RuntimeError)


def test_reset_time_reads_gh_api_rate_limit():
    def run(argv, **kw):
        assert argv == ['gh', 'api', 'rate_limit']
        return subprocess.CompletedProcess(argv, 0, json.dumps({'resources': {'graphql': {'remaining': 0, 'reset': 123}}}))
    assert M.reset_time(run=run) == 123.0


# -- the cascade stops, keeps the rerun flag, schedules ONE rerun ---------------------------------

def test_the_cascade_stops_on_a_rate_limit_and_schedules_one_rerun(tmp_path, monkeypatch):
    flag = tmp_path / 'rerun'
    sched = []
    monkeypatch.setattr(A, 'schedule', lambda reset: sched.append(reset) or 'one rerun scheduled in 660s')

    def body():
        raise M.RateLimited('GraphQL rate limit', reset=1794709000.0)
    out = []
    rc = A.run_locked(body, flag=flag, lock=tmp_path / 'lock', out=out.append, env={})
    assert rc == A.EXIT_RATELIMIT == 75 and flag.exists() and sched == [1794709000.0]
    assert out[-1].startswith('stopped: GraphQL rate limit; it resets at ') and 'rerun flag kept' in out[-1]
    assert not (tmp_path / 'lock').exists() or A.gatelock  # the with released it: a second run gets the lock
    assert A.run_locked(lambda: None, flag=flag, lock=tmp_path / 'lock', out=out.append, env={}) == 0


def test_a_minbot_rate_limit_error_text_also_stops_the_cascade(tmp_path, monkeypatch):
    monkeypatch.setattr(A, 'schedule', lambda reset: 'scheduled')
    monkeypatch.setattr(M, 'reset_time', lambda: 99.0)

    def body():
        raise RuntimeError('minbot project status failed: API rate limit exceeded for installation ID 1')
    assert A.run_locked(body, flag=tmp_path / 'f', lock=tmp_path / 'l', out=lambda s: None, env={}) == 75


def test_schedule_starts_one_timer_and_keeps_a_waiting_one():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc = 1 if argv[:2] == ['systemctl', '--user'] else 0
        return subprocess.CompletedProcess(argv, rc, '', '')
    assert REAL_SCHEDULE(1000.0, run=run, now=lambda: 400.0) == \
        f"one rerun scheduled in 660s ({A.RERUN_UNIT}.timer)"
    assert calls[1][:5] == ['systemd-run', '--user', '--collect', f'--unit={A.RERUN_UNIT}', '--on-active=660s']
    assert calls[1][-2:] == ['--cascade', '--if-flagged']
    active = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '', '')  # noqa: E731
    assert REAL_SCHEDULE(1000.0, run=active, now=lambda: 400.0).startswith('a rerun is already scheduled')


def test_the_daemon_sleeps_until_the_reset_after_a_rate_limit():
    assert P.limit_pause(M.RateLimited('x', reset=1000.0), now=lambda: 400.0) == 630
    assert P.limit_pause(RuntimeError('some other error')) is None
