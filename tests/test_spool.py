"""The handoff spool (2026-11-15 01:40 ET blocker): inside swarm-workers.slice NoNewPrivileges
is set, so sudo, and with it every gh read through the minbot shim, fails. The worker side
must only write spool files; the swarm-promoted daemon runs the GitHub calls outside."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from tangentswarm import spool, worker_mcp as W

from test_stophook import G, run, wt  # noqa: F401  (the worktree fixture)

SRC = str(Path(__file__).resolve().parents[1])


def daemon(stop, executor=None):
    """A stand-in swarm-promoted: dispatch every 50 ms and serve in-process."""
    def serve(cmd, **k):
        spool.serve(cmd[-1])
    while not stop.is_set():
        spool.dispatch(spawn=serve, command=['serve'])
        time.sleep(0.05)


def test_the_worker_side_never_runs_sudo_or_gh(wt, tmp_path):  # noqa: F811
    """Run the real hook and the MCP client side in a child process whose sudo and gh fail
    the way they do in the slice; neither may be called."""
    fake = tmp_path / 'fakebin'
    fake.mkdir()
    calls = tmp_path / 'calls'
    for name in ('sudo', 'gh', 'minbot'):
        (fake / name).write_text(f'#!/bin/sh\necho {name} >> {calls}\n'
                                 'echo "sudo: The no new privileges flag is set" >&2\nexit 1\n')
        (fake / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:/usr/bin:/bin", PYTHONPATH=SRC, SWARM_HANDOFF_SPOOL=str(tmp_path / 'sp'),
               SWARM_WORKTREE_PARENT=str(wt.parent), SWARM_STOP_HOOK_LOG=str(tmp_path / 'log'))
    code = ("import io, json, sys; from tangentswarm import stophook; "
            f"sys.exit(stophook.hook(io.StringIO(json.dumps({{'cwd': {str(wt)!r}}}))))")
    r = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not calls.exists()  # no sudo, no gh, no minbot inside the slice
    assert len(list((tmp_path / 'sp').glob('*.req.json'))) == 1


def test_a_worker_tool_gets_the_real_error_text_back(wt, monkeypatch):  # noqa: F811
    monkeypatch.setenv('SWARM_WORKER_CWD', str(wt))

    def fail(req):
        raise RuntimeError('gh api graphql ...: sudo: The no new privileges flag is set')
    monkeypatch.setattr(spool, 'execute', fail)
    stop = threading.Event()
    t = threading.Thread(target=daemon, args=(stop,), daemon=True)
    t.start()
    try:
        r = W.via_daemon('status')
    finally:
        stop.set()
        t.join(2)
    assert r == {'ok': False, 'error': 'RuntimeError: gh api graphql ...: sudo: The no new privileges flag is set'}


def test_a_worker_tool_gets_the_daemon_answer(wt, monkeypatch):  # noqa: F811
    monkeypatch.setenv('SWARM_WORKER_CWD', str(wt))
    monkeypatch.setattr(spool, 'execute', lambda req: {'ok': True, 'op': req['op'], 'cwd': req['cwd']})
    stop = threading.Event()
    t = threading.Thread(target=daemon, args=(stop,), daemon=True)
    t.start()
    try:
        r = W.via_daemon('handoff')
    finally:
        stop.set()
        t.join(2)
    assert r == {'ok': True, 'op': 'handoff', 'cwd': str(wt)}
    assert list(spool.SPOOL.iterdir()) == []  # request, claim and answer files are all gone


def test_no_daemon_gives_a_clear_error(wt, monkeypatch):  # noqa: F811
    t = [0.0]
    r = spool.submit('status', wt, wait=5, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert r['ok'] is False and 'systemctl --user status swarm-promoted' in r['error']
    assert list(spool.SPOOL.glob('*.req.json')) == []  # the unclaimed request is taken back


def test_dispatch_claims_once_and_serves_a_stop_hook_without_an_answer(wt, monkeypatch):  # noqa: F811
    served = []
    monkeypatch.setattr(spool, 'execute', lambda req: served.append(req['op']) or {'ok': True, 'log': 'x'})
    spool.submit('stophook', wt, transcript='/t')
    got = spool.dispatch(spawn=lambda cmd, **k: spool.serve(cmd[-1]), command=['serve'])
    assert len(got) == 1 and served == ['stophook']
    assert spool.dispatch(spawn=lambda cmd, **k: served.append('again'), command=['serve']) == []
    assert list(spool.SPOOL.iterdir()) == []  # no answer file for the Stop hook


def test_requests_claimed_before_a_restart_are_served_again(wt):  # noqa: F811
    """01:52 ET: a daemon restart (KillMode=control-group) killed four serve processes and
    left their claimed files behind for good."""
    spool.submit('stophook', wt)
    spool.dispatch(spawn=lambda cmd, **k: None, command=['serve'])  # claimed, then the serve dies
    assert len(list(spool.SPOOL.glob('*.run.json'))) == 1
    assert len(spool.reclaim()) == 1 and len(list(spool.SPOOL.glob('*.req.json'))) == 1
    spool.dispatch(spawn=lambda cmd, **k: None, command=['serve'])
    t = time.time()
    assert spool.reclaim(older_than=spool.RUN_STALE, now=lambda: t) == []  # fresh claim: kept
    assert len(spool.reclaim(older_than=spool.RUN_STALE, now=lambda: t + spool.RUN_STALE + 1)) == 1
