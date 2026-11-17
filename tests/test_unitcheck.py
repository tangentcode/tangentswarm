"""swarm-unit-check (methodology #84): every swarm unit imports tangentswarm from flow-live.
Fake systemctl output and a fake /proc only; nothing reads the real host."""
import json
import os

import pytest

from tangentswarm import unitcheck as U

H = '/home/u'
LIVE = f'{H}/ver/tangentswarm.flow-live'
OLD = f'{H}/srv/swarm-tmux'
VENV = f'{H}/.venvs/swarm-tmux/bin'
CHECKOUTS = {LIVE, OLD}


def has(d):
    return d in CHECKOUTS


def exec_start(argv, pid=0):
    return (f"{{ path={argv.split()[0]} ; argv[]={argv} ; ignore_errors=no ; start_time=[n/a] ; "
            f"stop_time=[n/a] ; pid={pid} ; code=(null) ; status=0/0 }}")


def unit(state='enabled', active='active', wd=LIVE, env=f'PYTHONPATH={LIVE}', argv=f'{VENV}/python -m tangentswarm.promoted',
         pid=0):
    return dict(UnitFileState=state, ActiveState=active, WorkingDirectory=wd, Environment=env,
                ExecStart=exec_start(argv, pid), MainPID=str(pid))


# the units as they are on the host after the #84 fix
GOOD = {
    'swarm-webhook.service': unit(wd=H, env=f'PYTHONPATH={LIVE} PATH=/usr/bin:/bin PYTHONUNBUFFERED=1',
                                  argv=f'{VENV}/python -m tangentswarm.webhook --port 8772'),
    'swarm-promoted.service': unit(),
    'swarm-mcp-apikey.service': unit(env=f'TANGENTSWARM_API_KEY_FILE={H}/k PYTHONPATH={LIVE}',
                                     argv=f'{VENV}/swarm-mcp --http --auth-mode apikey --port 8766'),
    'swarm-mcp-oauth.service': unit(env=f'PYTHONPATH={LIVE} "TANGENTSWARM_AUTH_REQUIRED_SCOPES=a:read a:shell"',
                                    argv=f'{VENV}/swarm-mcp --http --auth-mode builtin --port 8767'),
    'swarm-mcp-http.service': unit(state='disabled', active='inactive', wd='!' + H, env='',
                                   argv=f'{H}/.venvs/tangentswarm/bin/swarm-mcp --http --port 8765'),
}


class Systemctl:
    def __init__(self, units, memory=None):
        self.units = units
        self.memory = memory or {}  # units in memory with no unit file row: {name: props}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == 'list-unit-files':
            rows = [f"{u} {p['UnitFileState']} enabled" for u, p in self.units.items()]
            return '\n'.join(rows + ['swarm@.service disabled enabled']) + '\n'
        if args[0] == 'list-units':
            both = {**self.units, **self.memory}
            return ''.join(f"{u} loaded {p['ActiveState']} running x\n" for u, p in both.items()
                           if p['ActiveState'] != 'inactive' or u in self.memory)
        assert args[0] == 'show'
        p = dict({**self.units, **self.memory}[args[1]], Id=args[1])
        return ''.join(f"{k}={p[k]}\n" for k in U.PROPS if k in p)


def run_check(units, **kw):
    return {r['unit']: r for r in U.check(run=Systemctl(units), live=LIVE, has=has, **kw)}


def test_all_good_units_pass_and_the_disabled_unit_is_skipped():
    sc = Systemctl(GOOD)
    res = U.check(run=sc, live=LIVE, has=has)
    assert sorted(r['unit'] for r in res) == sorted(u for u in GOOD if u != 'swarm-mcp-http.service')
    assert all(r['ok'] for r in res), res
    shown = [a[1] for a in sc.calls if a[0] == 'show']
    assert 'swarm@.service' not in shown  # a template unit is never shown


def test_the_84_incident_unit_on_srv_swarm_tmux_fails_and_is_named(capsys, monkeypatch):
    units = dict(GOOD)
    units['swarm-mcp-apikey.service'] = unit(wd=OLD, env=f'TANGENTSWARM_API_KEY_FILE={H}/k',
                                             argv=f'{VENV}/swarm-mcp --http --auth-mode apikey --port 8766')
    res = run_check(units)
    bad = [u for u, r in res.items() if not r['ok']]
    assert bad == ['swarm-mcp-apikey.service']
    text = ' '.join(res['swarm-mcp-apikey.service']['problems'])
    assert "venv's installed tangentswarm loads" in text and f'WorkingDirectory {OLD}' in text

    monkeypatch.setattr(U, 'check', lambda live=None, units=None: list(res.values()))
    assert U.main([]) == 1
    out = capsys.readouterr()
    assert 'FAIL swarm-mcp-apikey.service' in out.out and 'swarm-mcp-apikey.service' in out.err


def test_python_m_imports_from_a_stale_working_directory_before_pythonpath():
    units = {'swarm-promoted.service': unit(wd=OLD)}  # -m: the cwd comes first on sys.path
    r = run_check(units)['swarm-promoted.service']
    assert not r['ok'] and any(f'imports tangentswarm from {OLD}' in p for p in r['problems'])


def test_pythonpath_order_and_isolated_mode():
    r = run_check({'swarm-webhook.service': unit(wd=H, env=f'PYTHONPATH={OLD}:{LIVE}')})['swarm-webhook.service']
    assert any(f'from {OLD}' in p for p in r['problems'])
    r = run_check({'swarm-webhook.service': unit(wd=H, argv=f'{VENV}/python -I -m tangentswarm.webhook')})
    assert not r['swarm-webhook.service']['ok']  # -I: PYTHONPATH is ignored


def test_execstart_script_inside_another_checkout_fails():
    units = {'swarm-events.service': unit(argv=f'{OLD}/bin/swarm-events --follow')}
    r = run_check(units)['swarm-events.service']
    assert any(f'ExecStart runs {OLD}/bin/swarm-events' in p for p in r['problems'])


def test_a_running_template_instance_is_checked_and_a_dead_one_is_not():
    """Codex on PR #80: only swarm@.service has a unit file; its instance is found in memory."""
    mem = {'swarm@w1.service': unit(state='disabled', wd=OLD, env='', argv=f'{VENV}/python -m tangentswarm.tick'),
           'swarm@w2.service': unit(state='disabled', active='inactive', wd=OLD, env=''),
           'swarm-run-x.service': unit(state='transient', wd=OLD, env='')}
    sc = Systemctl(GOOD, memory=mem)
    res = {r['unit']: r for r in U.check(run=sc, live=LIVE, has=has)}
    assert not res['swarm@w1.service']['ok'] and 'swarm@w2.service' not in res
    assert not res['swarm-run-x.service']['ok']
    assert all(r['ok'] for u, r in res.items() if u in GOOD)


def test_a_disabled_unit_that_runs_is_checked():
    units = {'swarm-mcp-http.service': unit(state='disabled', active='active', wd=OLD, env='',
                                            argv=f'{VENV}/swarm-mcp --http')}
    assert not run_check(units)['swarm-mcp-http.service']['ok']


def fake_proc(tmp_path, pid, argv, cwd, env):
    d = tmp_path / str(pid)
    d.mkdir()
    (d / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
    os.symlink(cwd, d / 'cwd')
    (d / 'environ').write_bytes(b'\0'.join(f"{k}={v}".encode() for k, v in env.items()) + b'\0')
    return tmp_path


def test_a_process_not_restarted_after_the_drop_in_fails(tmp_path):
    """The unit file is right, but the process still runs with the old cwd and no PYTHONPATH."""
    units = {'swarm-mcp-oauth.service': unit(argv=f'{VENV}/swarm-mcp --http', pid=4242)}
    proc = fake_proc(tmp_path, 4242, [f'{VENV}/python', f'{VENV}/swarm-mcp', '--http'], OLD, {'HOME': H})
    r = run_check(units, proc=proc)['swarm-mcp-oauth.service']
    assert not r['ok'] and all(p.startswith('process 4242:') for p in r['problems'])

    proc2 = tmp_path / 'b'
    proc2.mkdir()
    fake_proc(proc2, 4242, [f'{VENV}/python', f'{VENV}/swarm-mcp', '--http'], LIVE, {'PYTHONPATH': LIVE})
    assert run_check(units, proc=proc2)['swarm-mcp-oauth.service']['ok']


def test_a_missing_process_is_a_problem(tmp_path):
    units = {'swarm-promoted.service': unit(pid=999)}
    r = run_check(units, proc=tmp_path)['swarm-promoted.service']
    assert not r['ok'] and 'cannot read /proc/999' in r['problems'][0]


def test_flow_live_without_the_package_fails():
    res = U.check(run=Systemctl({}), live=LIVE, has=lambda d: False)
    assert res and not res[0]['ok'] and 'does not hold a tangentswarm package' in res[0]['problems'][0]


@pytest.mark.parametrize('argv, cwd, env, want', [
    (['/v/bin/swarm-mcp', '--http'], '/w', {}, ('/v/bin', True)),
    (['/v/bin/python', '-m', 'x'], '/w', {}, ('/w', True)),
    (['/v/bin/python3', '-u', '-m', 'x'], '/w', {}, ('/w', True)),
    (['/v/bin/python', '-c', 'pass'], '/w', {}, ('/w', True)),
    (['/v/bin/python', '-W', 'ignore', '/s/run.py'], '/w', {}, ('/s', True)),
    (['/v/bin/python', '-E', '-m', 'x'], '/w', {}, ('/w', False)),
    (['/v/bin/python', '-Im', 'x'], '/w', {}, (None, False)),
    (['/v/bin/python', '-m', 'x'], '/w', {'PYTHONSAFEPATH': '1'}, (None, True)),
])
def test_path0(argv, cwd, env, want):
    assert U.path0(argv, cwd, env) == want


def test_systemctl_show_parsing():
    env = U.environment('A=1 "B=two words" PYTHONPATH=/x:/y')
    assert env == {'A': '1', 'B': 'two words', 'PYTHONPATH': '/x:/y'}
    assert U.exec_argvs(exec_start('/v/python -m a --p 1')) == [['/v/python', '-m', 'a', '--p', '1']]


def test_main_json_and_read_error(capsys, monkeypatch):
    monkeypatch.setattr(U, 'check', lambda live=None, units=None: [dict(unit='a', state='enabled', active='active',
                                                                        pid=0, ok=True, problems=[])])
    assert U.main(['--json']) == 0 and json.loads(capsys.readouterr().out)[0]['unit'] == 'a'

    def broken(live=None, units=None):
        raise RuntimeError('no bus')
    monkeypatch.setattr(U, 'check', broken)
    assert U.main([]) == 2 and 'cannot read the units' in capsys.readouterr().err


