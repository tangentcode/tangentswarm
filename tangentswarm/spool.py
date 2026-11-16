"""The handoff spool: worker requests run outside swarm-workers.slice.

Worker Claudes run inside swarm-workers.slice with NoNewPrivileges. Every GitHub read goes
through the gh shim to `minbot api`, which needs sudo, so in the slice the Stop hook and
swarm-worker-mcp failed on every gh call ("sudo: The no new privileges flag is set";
stop-hook.log 2026-11-15 01:40-01:42 ET, PRs #447 #518 #525 #527 #528). No credential is
minted, and no other token is used.

Instead the worker side only writes a request file into SPOOL (no gh, no sudo). The
swarm-promoted daemon (a user service, outside the slice) checks SPOOL every second and
runs each request as `swarm-stop-hook serve FILE`. A request that wants an answer (the
worker MCP tools) gets a result file, which the worker side waits for.

  <id>.req.json   {op, cwd, transcript, at}: written by the worker side (atomic rename)
  <id>.run.json   claimed by the daemon (rename), being served
  <id>.res.json   the answer {ok, ...} or {ok: false, error: "<real error text>"}

ops: stophook (the Stop hook; no answer), handoff, status, request_review (worker MCP).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SPOOL = Path(os.environ.get('SWARM_HANDOFF_SPOOL', str(Path.home() / '.cache' / 'swarm-handoff-spool')))
OPS = ('stophook', 'handoff', 'status', 'request_review', 'launch', 'start')
WAIT = int(os.environ.get('SWARM_SPOOL_WAIT', '120'))  # s a worker MCP tool waits for its answer
MAX_AGE = 3600  # s: an unclaimed request or an unread answer older than this is dropped
NO_DAEMON = ("no answer from the swarm-promoted daemon in {wait}s; it runs the GitHub calls outside "
             "swarm-workers.slice. Check it with: systemctl --user status swarm-promoted")


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def submit(op, cwd, transcript=None, wait=0, spool=None, sleep=time.sleep, clock=time.monotonic, **extra):
    """Queue one request. wait > 0: wait up to that many seconds for the answer and return
    it; else return {'queued': id}."""
    if op not in OPS:
        raise ValueError(f'unknown spool op {op!r}')
    spool = spool or SPOOL
    rid = f"{time.time_ns()}-{os.getpid()}"
    _write(spool / f'{rid}.req.json', dict(extra, op=op, cwd=str(cwd), transcript=transcript, at=time.time()))
    if not wait:
        return {'queued': rid}
    res, end = spool / f'{rid}.res.json', clock() + wait
    while clock() < end:
        if res.exists():
            try:
                out = json.loads(res.read_text())
            except ValueError:
                sleep(0.2)  # being written
                continue
            res.unlink(missing_ok=True)
            return out
        sleep(0.5)
    for stale in (spool / f'{rid}.req.json',):  # never claimed: take it back
        stale.unlink(missing_ok=True)
    return {'ok': False, 'error': NO_DAEMON.format(wait=wait)}


RUN_STALE = 900  # s: a claimed request older than this lost its serve process: queue it again


def reclaim(spool=None, older_than=None, now=time.time):
    """Put claimed requests back into the queue: all of them at daemon start (a restart kills
    the whole service cgroup, serve processes included), else those older than older_than s.
    A handoff is safe to repeat (once per head). Returns the request ids."""
    spool = spool or SPOOL
    back = []
    for run in spool.glob('*.run.json') if spool.is_dir() else ():
        try:
            if older_than is not None and now() - run.stat().st_mtime < older_than:
                continue
            os.replace(run, run.with_name(run.name.replace('.run.json', '.req.json')))
            back.append(run.name.split('.')[0])
        except OSError:
            continue
    return back


def in_flight(ops=('launch', 'start'), spool=None):
    """[request dict] of the requests with these ops that wait or run now (live work, not
    bookkeeping: tangentswarm #44). Each request is one Claude about to start."""
    spool = spool or SPOOL
    out = []
    for f in list(spool.glob('*.req.json')) + list(spool.glob('*.run.json')) if spool.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get('op') in ops:
            out.append(d)
    return out


def dispatch(spool=None, spawn=subprocess.Popen, command=None, log=None, now=time.time):
    """Daemon side, every second: claim each request and serve it in its own process.
    Returns the claimed request ids."""
    spool = spool or SPOOL
    if not spool.is_dir():
        return []
    reclaim(spool, older_than=RUN_STALE, now=now)
    started = []
    for req in sorted(spool.glob('*.req.json')):
        try:
            age = now() - req.stat().st_mtime
        except OSError:
            continue
        if age > MAX_AGE:
            req.unlink(missing_ok=True)
            continue
        run = req.with_name(req.name.replace('.req.json', '.run.json'))
        try:
            os.replace(req, run)  # claim it (one daemon wins)
        except OSError:
            continue
        os.utime(run)  # the claim time (RUN_STALE counts from here)
        cmd = list(command or [_hook_command(), 'serve']) + [str(run)]
        logf = open(log, 'a') if log else subprocess.DEVNULL
        try:
            spawn(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
        finally:
            if log:
                logf.close()
        started.append(req.name.split('.')[0])
    for res in spool.glob('*.res.json'):  # answers nobody read
        try:
            if now() - res.stat().st_mtime > MAX_AGE:
                res.unlink(missing_ok=True)
        except OSError:
            pass
    return started


def _hook_command():
    from .promoted import BIN
    return str(BIN / 'swarm-stop-hook')


def execute(req):
    """Run one request (daemon side, outside the slice). Returns the answer dict."""
    from . import stophook, worker_mcp
    op, cwd = req.get('op'), Path(req.get('cwd') or '')
    if op == 'stophook':
        return {'ok': True, 'log': stophook.run(cwd, transcript=req.get('transcript'))}
    if op == 'handoff':
        return dict(worker_mcp.do_handoff(cwd), ok=True)
    if op == 'status':
        return dict(worker_mcp.status(cwd), ok=True)
    if op == 'request_review':
        return dict(worker_mcp.do_request_review(cwd), ok=True)
    if op == 'launch':
        from . import launcher
        return launcher.execute(req)
    if op == 'start':
        from . import launcher
        return launcher.execute_start(req)
    return {'ok': False, 'error': f'unknown op {op!r}'}


def serve(run_path):
    """`swarm-stop-hook serve FILE`: execute one claimed request and write its answer. Any
    error becomes {ok: false, error: '<type>: <text>'} (the real text, never a bare
    "Error executing tool")."""
    run_path = Path(run_path)
    try:
        req = json.loads(run_path.read_text())
    except (OSError, ValueError) as e:
        print(f'spool: unreadable request {run_path.name}: {e}', file=sys.stderr)
        run_path.unlink(missing_ok=True)
        return 2
    try:
        out = execute(req)
    except Exception as e:
        out = {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:600]}'}
    if req.get('op') in ('stophook', 'launch', 'start'):
        if not out.get('ok'):
            from .stophook import say
            what = (f"launch #{req.get('issue')}" if req.get('op') == 'launch' else
                    f"start #{req.get('pr')}" if req.get('op') == 'start' else Path(req.get('cwd') or '').name)
            say(f"{what}: {req.get('op')} failed: {out['error']}")
    else:
        _write(run_path.with_name(run_path.name.replace('.run.json', '.res.json')), out)
    run_path.unlink(missing_ok=True)
    return 0
