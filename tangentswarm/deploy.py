"""swarm-deploy: deploy tangentswarm main to the live checkout after a merge (tangentswarm #113).

Michal 2026-11-17: every merge into tangentswarm main goes live on its own, tests first,
with a rollback when the units do not come back right. Triggers:
  merge   swarm-webhook: a tangentcode/tangentswarm PR merged into main (action 'deploy')
  pass    swarm-promoted, every PASS_EVERY s: origin main is not live and no deploy of it
          was tried (a merge event lost during a restart, or never sent)

Both call start(sha): a transient user unit (systemd-run, unit deploy-tangentswarm-<sha12>)
runs `python -m tangentswarm.deploy run --sha SHA`. It is not inside any swarm unit, so the
restart of swarm-webhook or swarm-promoted cannot kill it. One deploy runs at a time (LOCK).

The run, on the live checkout ($SWARM_FLOW_LIVE, default ~/ver/tangentswarm.flow-live):
  1. git fetch origin main. The target is origin/main; it must contain the merged sha. When
     the live HEAD already contains the target: nothing to do. A live checkout with local
     changes is never touched (a failure, reported).
  2. The test suite runs on the target in a scratch worktree (git worktree add --detach),
     not in the live checkout, so the services keep the old code. A failure: no deploy,
     the services stay on the old sha, one report.
  3. Pass: the live checkout goes to the target (git checkout --detach), the four UNITS are
     restarted, and after SETTLE s each must be active and swarm-unit-check must pass.
  4. Either fails: the live checkout goes back to the previous sha, the UNITS are restarted
     again and checked again, and one report names the failing sha.
  5. Every run is one JSON line in LOG: time, sha, previous sha, test result, unit-check
     result, result.

Reports go to swarmbot (alerts.wake_swarmbot: deduped by key, one per sha). A sha that LOG
already has a final result for is not deployed again by start(), so a failing merge is
reported once and stays undeployed until the next merge.

GitHub events during the restart: swarm-webhook writes each delivery to its inbox before it
answers 200, and its worker handles the inbox at start, so a delivery that was received is
handled after the restart (an inbox file is removed only after it was handled). A delivery
GitHub could not send while the port was closed is not sent again by GitHub; the regular
board passes of swarm-promoted (BOARD_EVERY, and PASS_EVERY for the deploy) pick up its
effect from the board state.

    swarm-deploy run --sha SHA [--pr N]     the deploy, in this process
    swarm-deploy start --sha SHA [--pr N]   the deploy, in its own transient unit
    swarm-deploy log [-n 20]                the last deploys
"""
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = 'tangentcode/tangentswarm'
UNITS = ('swarm-mcp-apikey', 'swarm-mcp-oauth', 'swarm-promoted', 'swarm-webhook')
SETTLE = int(os.environ.get('SWARM_DEPLOY_SETTLE', '10'))       # s after a restart before the checks
TEST_TIMEOUT = int(os.environ.get('SWARM_DEPLOY_TEST_TIMEOUT', '1200'))
PASS_EVERY = int(os.environ.get('SWARM_DEPLOY_PASS_EVERY', '300'))  # s between swarm-promoted's checks
UNIT_PREFIX = 'deploy-tangentswarm-'  # not swarm*: swarm-unit-check does not check the deploy itself
FINAL = ('deployed', 'not-deployed', 'rolled-back', 'rollback-failed', 'already-live')


def live_path():
    return os.path.realpath(os.path.expanduser(
        os.environ.get('SWARM_FLOW_LIVE') or '~/ver/tangentswarm.flow-live'))


def state_dir():
    return Path(os.environ.get('SWARM_DEPLOY_STATE') or (Path.home() / '.local' / 'state' / 'swarm-flow'))


def log_path():
    return state_dir() / 'deploy.log'


def et_now(t=None):
    from .alerts import et_stamp
    return et_stamp(time.time() if t is None else t)


def read_log(path=None):
    """The deploy log rows (oldest first); a bad line is skipped."""
    out = []
    try:
        lines = Path(path or log_path()).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def tried(sha, path=None):
    """True when the log has a final result for sha (a deploy of it ran to its end)."""
    return any(r.get('sha') == sha and r.get('result') in FINAL for r in read_log(path))


def write_log(row, path=None):
    path = Path(path or log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps(row) + '\n')


def run_cmd(argv, cwd=None, env=None, timeout=300):
    """(returncode, output tail). Never raises: a timeout or a missing program is rc 124/127."""
    try:
        r = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, f"{type(e).__name__}: {e}"
    return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()[-2000:]


class Deploy:
    """One deploy of one merged sha. Every side effect is an injected callable or a method,
    so the tests run with no git, systemd or network.
      run(argv, cwd=None, env=None, timeout=...) -> (rc, output)
      report(why, key, pr) -> a note line"""

    def __init__(self, sha, pr=None, live=None, run=run_cmd, report=None, sleep=time.sleep, now=time.time,
                 log=None, python=None, scratch=None):
        self.sha, self.pr = sha, pr
        self.live = live or live_path()
        self._run, self.sleep, self.now = run, sleep, now
        self._report = report
        self.log = log
        self.python = python or sys.executable
        self.scratch = Path(scratch) if scratch else state_dir() / 'deploy-test'
        self.notes = []

    def git(self, *args, timeout=300):
        return self._run(['git', '-C', self.live, *args], timeout=timeout)

    def rev(self, ref):
        rc, out = self.git('rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}')
        return out.strip().splitlines()[-1] if rc == 0 and out.strip() else None

    def contains(self, new, old):
        """True when commit new contains commit old (old is an ancestor of new, or the same)."""
        return new == old or self.git('merge-base', '--is-ancestor', old, new)[0] == 0

    def report(self, why):
        if self._report:
            return self._report(why, f"deploy:{self.sha}", self.pr)
        from . import alerts, webhook
        note = alerts.wake_swarmbot(why, f"deploy:{self.sha}", pr=self.pr, kind='deploy')
        webhook.drain_posts(60)
        return note

    # -- the steps --------------------------------------------------------------------------
    def tests(self, target):
        """(passed, output) of the full suite on target, in a scratch worktree."""
        tree = self.scratch / target[:12]
        self.drop_tree(tree)
        tree.parent.mkdir(parents=True, exist_ok=True)
        rc, out = self.git('worktree', 'add', '--detach', str(tree), target)
        if rc:
            return False, f"git worktree add failed: {out[-300:]}"
        try:
            env = dict(os.environ, PYTHONPATH=str(tree), PYTHONDONTWRITEBYTECODE='1')
            rc, out = self._run([self.python, '-m', 'pytest', '-q', '-x', '-p', 'no:cacheprovider'],
                                cwd=str(tree), env=env, timeout=TEST_TIMEOUT)
            return rc == 0, out
        finally:
            self.drop_tree(tree)

    def drop_tree(self, tree):
        self.git('worktree', 'remove', '--force', str(tree))
        if Path(tree).exists():
            shutil.rmtree(tree, ignore_errors=True)
        self.git('worktree', 'prune')

    def restart(self):
        """(ok, why): restart the UNITS; after SETTLE s each is active and swarm-unit-check passes."""
        rc, out = self._run(['systemctl', '--user', 'restart', *UNITS], timeout=120)
        if rc:
            return False, f"systemctl restart failed: {out[-300:]}"
        self.sleep(SETTLE)
        rc, out = self._run(['systemctl', '--user', 'is-active', *UNITS], timeout=60)
        states = out.split()
        down = [f"{u} {s}" for u, s in zip(UNITS, states) if s != 'active']
        if rc or down or len(states) != len(UNITS):
            return False, 'not active: ' + (', '.join(down) or out[-200:] or f"is-active exit {rc}")
        env = dict(os.environ, TANGENTSWARM_PYTHON=self.python, SWARM_FLOW_LIVE=self.live)
        rc, out = self._run([os.path.join(self.live, 'bin', 'swarm-unit-check')], env=env, timeout=120)
        if rc:
            return False, f"swarm-unit-check failed (exit {rc}): {out[-600:]}"
        return True, 'units active, swarm-unit-check ok'

    def run(self):
        """The deploy. Returns the log row."""
        row = {'at': et_now(self.now()), 'sha': self.sha, 'pr': self.pr, 'target': None, 'prev': None,
               'tests': None, 'unit_check': None, 'result': None, 'detail': ''}
        try:
            self.steps(row)
        except Exception as e:
            row['result'] = row['result'] or 'error'
            row['detail'] = f"{row['detail']}; {type(e).__name__}: {str(e)[:300]}".strip('; ')
            self.report(f"tangentswarm deploy of {self.sha[:8]} failed: {row['detail']}")
        row['done_at'] = et_now(self.now())
        write_log(row, self.log)
        return row

    def steps(self, row):
        rc, out = self.git('fetch', '--quiet', 'origin', 'main', timeout=120)
        if rc:
            raise RuntimeError(f"git fetch failed: {out[-300:]}")
        target, prev = self.rev('FETCH_HEAD'), self.rev('HEAD')
        row['target'], row['prev'] = target, prev
        if not target or not prev:
            raise RuntimeError(f"cannot read the shas (origin main {target}, live HEAD {prev})")
        if not self.contains(target, self.sha):
            raise RuntimeError(f"origin main {target[:8]} does not contain {self.sha[:8]}")
        if self.contains(prev, target):
            row['result'] = 'already-live'
            row['detail'] = f"the live HEAD {prev[:8]} already contains {target[:8]}"
            return
        rc, out = self.git('status', '--porcelain', '--untracked-files=no')
        if rc or out.strip():
            row['result'] = 'not-deployed'
            row['detail'] = f"the live checkout {self.live} has local changes: not touched"
            self.report(f"tangentswarm deploy of {target[:8]} not done: {row['detail']}")
            return
        ok, out = self.tests(target)
        row['tests'] = 'pass' if ok else 'fail'
        if not ok:
            row['result'] = 'not-deployed'
            row['detail'] = 'tests failed: ' + out[-800:]
            self.report(f"tangentswarm main {target[:8]} failed its tests: not deployed, the services stay on "
                        f"{prev[:8]}. {out[-600:]}")
            return
        rc, out = self.git('checkout', '--quiet', '--detach', target)
        if rc:
            raise RuntimeError(f"git checkout {target[:8]} failed: {out[-300:]}")
        ok, why = self.restart()
        row['unit_check'] = 'pass' if ok else 'fail'
        if ok:
            row['result'] = 'deployed'
            row['detail'] = why
            return
        self.rollback(row, target, prev, why)

    def rollback(self, row, target, prev, why):
        rc, out = self.git('checkout', '--quiet', '--detach', prev)
        if rc:
            row['result'] = 'rollback-failed'
            row['detail'] = f"{why}; git checkout {prev[:8]} failed: {out[-300:]}"
        else:
            ok, back = self.restart()
            row['rollback_check'] = 'pass' if ok else 'fail'
            row['result'] = 'rolled-back' if ok else 'rollback-failed'
            row['detail'] = f"{why}; after the rollback to {prev[:8]}: {back}"
        self.report(f"tangentswarm main {target[:8]} failed the unit check after its restart ({why}). "
                    f"Rolled back to {prev[:8]}: {row['result']}.")


def lock():
    path = state_dir() / 'deploy.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, 'a')
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def run(sha, pr=None, **kw):
    """One deploy under the deploy lock. Returns the log row."""
    with lock():
        return Deploy(sha, pr, **kw).run()


def start(sha, pr=None, run=run_cmd, log=None):
    """Start the deploy of sha in its own transient user unit. Returns a note line."""
    if not sha or len(sha) != 40:
        return f"no deploy: a full sha is needed (got {sha!r})"
    if tried(sha, log):
        return f"no deploy: {sha[:8]} was already tried (see {log or log_path()})"
    live = live_path()
    argv = ['systemd-run', '--user', '--quiet', '--collect', f'--unit={UNIT_PREFIX}{sha[:12]}',
            f'--working-directory={live}', f'--setenv=PYTHONPATH={live}',
            f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
            sys.executable, '-m', 'tangentswarm.deploy', 'run', '--sha', sha]
    if pr:
        argv += ['--pr', str(pr)]
    rc, out = run(argv, timeout=60)
    if rc:
        if 'already' in out:
            return f"a deploy of {sha[:8]} is already running"
        return f"the deploy of {sha[:8]} did not start: {out[-200:]}"
    return f"deploy of {sha[:8]} started (unit {UNIT_PREFIX}{sha[:12]}; log {log or log_path()})"


def pass_check(run=run_cmd, begin=None, log=None):
    """swarm-promoted's regular pass: when origin main is not in the live HEAD and no deploy
    of it was tried, start one. git ls-remote only: no API quota. Returns a note or None."""
    live = live_path()
    if not os.path.isdir(live):
        return None
    rc, out = run(['git', '-C', live, 'ls-remote', 'origin', 'refs/heads/main'], timeout=60)
    main = out.split()[0] if rc == 0 and out.strip() else ''
    if len(main) != 40:
        return None
    rc, head = run(['git', '-C', live, 'rev-parse', 'HEAD'], timeout=30)
    if rc == 0 and head.strip() == main:
        return None
    if tried(main, log):
        return None
    return (begin or start)(main)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-deploy', description=__doc__.split('\n', 1)[0])
    ap.add_argument('cmd', choices=['run', 'start', 'log'])
    ap.add_argument('--sha')
    ap.add_argument('--pr', type=int)
    ap.add_argument('-n', type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == 'log':
        for r in read_log()[-a.n:]:
            print(f"{r.get('at')} {(r.get('sha') or '')[:8]} tests={r.get('tests') or '-'} "
                  f"unit_check={r.get('unit_check') or '-'} {r.get('result')}: {(r.get('detail') or '')[:160]}")
        return 0
    if not a.sha:
        ap.error('--sha is needed')
    if a.cmd == 'start':
        print(start(a.sha, a.pr))
        return 0
    from . import alerts, eventdb, webhook  # noqa: F401  loaded now: the run changes the code on disk
    eventdb.set_source('deploy')
    row = run(a.sha, a.pr)
    print(json.dumps(row))
    return 0 if row.get('result') in ('deployed', 'already-live') else 1


if __name__ == '__main__':
    sys.exit(main())
