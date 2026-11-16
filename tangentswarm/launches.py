"""swarm-launch: start a worker Claude, and read worker memory live.

swarm-start-scoped exits 3 ("held") when the worker slice is short of memory. There is no
held-launch list any more (Michal 2026-11-15 6:44 PM ET, tangentswarm #40: the Backlog
column is the queue): the launcher checks live capacity before it makes anything, and a
start that memory still holds stays the PR's unstarted record, which the cascade retries
when capacity is free.

Memory is read live on every check: the slice's MemoryCurrent, MemoryHigh and
MemoryMax from systemd, and MemAvailable from /proc/meminfo. Nothing assumes a fixed
slice size. The admission rule is the same as swarm-start-scoped's.

    swarm-launch BRANCH [WINDOW] [--pr N] [--brief FILE]   start a worker
    swarm-launch --memory                                  print the worker memory
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
SLICE = os.environ.get('SWARM_SLICE', 'swarm-workers.slice')
MB = 1024 ** 2
FLOOR = 700 * MB              # headroom under MemoryHigh (swarm-start-scoped's rule)
AVAIL_MIN = int(1.5 * 1024 ** 3)  # host MemAvailable must exceed this (same rule)
HELD = 3
LOAD_PER_CPU = float(os.environ.get('SWARM_LOAD_PER_CPU', '1.5'))  # 5-min load per CPU that holds starts


# ---------------------------------------------------------------------------
# memory, read live

def _systemctl_show(slice_):
    r = subprocess.run(['systemctl', '--user', 'show', slice_, '-p', 'MemoryCurrent', '-p', 'MemoryHigh',
                        '-p', 'MemoryMax'], capture_output=True, text=True, timeout=30)
    return r.stdout


def _meminfo():
    return Path('/proc/meminfo').read_text()


def parse_limit(v):
    """systemd prints 'infinity' or '[not set]' for no limit."""
    return int(v) if v and v.isdigit() else None


def memory(show=_systemctl_show, meminfo=_meminfo, loadavg=os.getloadavg, cpus=os.cpu_count):
    """{current, high, max, avail} in bytes (high/max are None when unlimited), and the host
    {load, cpus} (tangentswarm #44: starts are admitted on CPU load too)."""
    vals = dict(line.split('=', 1) for line in show(SLICE).splitlines() if '=' in line)
    info = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in meminfo().splitlines()
            if line.startswith('MemAvailable:')}
    cur = parse_limit(vals.get('MemoryCurrent'))
    if cur is None:
        raise RuntimeError(f"cannot read {SLICE} MemoryCurrent: {vals.get('MemoryCurrent')!r}")
    return dict(current=cur, high=parse_limit(vals.get('MemoryHigh')), max=parse_limit(vals.get('MemoryMax')),
                avail=info.get('MemAvailable', 0), load=list(loadavg()), cpus=cpus() or 1)


def headroom(m):
    limit = m['high'] or m['max']
    return None if limit is None else limit - m['current']


def load_max(cpus):
    return LOAD_PER_CPU * (cpus or 1)


def cpu_admits(m):
    """The 5-minute load average is under LOAD_PER_CPU per CPU (True when m has no load)."""
    load = m.get('load')
    return not load or load[1] < load_max(m.get('cpus'))


def admits(m):
    room = headroom(m)
    return (room is None or room >= FLOOR) and m['avail'] > AVAIL_MIN and cpu_admits(m)


def describe(m):
    def mb(v):
        return 'none' if v is None else f"{v // MB} MB"
    return (f"{SLICE} uses {mb(m['current'])} of MemoryHigh {mb(m['high'])} (MemoryMax {mb(m['max'])}), "
            f"headroom {mb(headroom(m))}; host MemAvailable {mb(m['avail'])}")


# ---------------------------------------------------------------------------
# starting a worker

OLD_HELD = None  # the retired held-launch list (#40); None: the path the old code used (old_held())


def old_held():
    """Codex on PR #46: the old code wrote the list to $SWARM_HELD when it was set."""
    return OLD_HELD or Path(os.environ.get('SWARM_HELD', str(HOME / 'var' / 'swarm-held.json')))


def pr_of_branch(branch):
    """The open launch PR whose head is branch, or None (gh failed or no such PR)."""
    from .launcher import REPO
    try:
        r = subprocess.run(['gh', 'pr', 'list', '-R', REPO, '--head', branch, '--state', 'open',
                            '--json', 'number', '--jq', '.[0].number'],
                           capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        return int(out) if r.returncode == 0 and out.isdigit() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def migrate_held(path=None, add=None, find_pr=None, warn=lambda s: None):
    """Once, at daemon start: each row of the retired held-launch list becomes its PR's
    unstarted record (the cascade's start retry starts it) (Codex on PR #46: a launch held
    before the upgrade had its retry only in that file). A row with no PR gets the open PR
    of its branch. A row that cannot be moved (no PR found, a bad row) stays in the file and
    is reported (warn) at every daemon start; the file goes only when nothing is left. An
    unreadable file is kept and reported. Returns the PR numbers moved."""
    path = path or old_held()
    try:
        rows = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        warn(f"retired held-launch list {path} is unreadable ({e}); kept, nothing migrated")
        return []
    if not isinstance(rows, list):
        warn(f"retired held-launch list {path} is not a list; kept, nothing migrated")
        return []
    if add is None:
        from . import slots
        add = slots.unstarted_add
    find_pr = find_pr or pr_of_branch
    moved, kept = [], []
    for r in rows:
        pr = None
        if isinstance(r, dict) and r.get('branch'):
            pr = r.get('pr') or find_pr(r['branch'])
        if not pr:
            kept.append(r)
            continue
        add(int(pr), branch=r['branch'], brief=r.get('brief') or '', issue=r.get('issue'))
        moved.append(int(pr))
    try:
        if kept:
            path.write_text(json.dumps(kept, indent=1))
            warn(f"retired held-launch list {path}: {len(kept)} row(s) with no open PR kept for a "
                 "person: " + ', '.join(str(r.get('branch') if isinstance(r, dict) else r) for r in kept))
        else:
            path.unlink()
    except OSError as e:
        warn(f"retired held-launch list {path}: could not update it ({e})")
    return moved


def clean_env(env=None):
    """The environment for swarm-start-scoped: without PYTHONPATH. The swarm-promoted daemon
    runs with PYTHONPATH=<its tangentswarm checkout>; inherited, it made swarm-start-scoped
    load that checkout's tangentswarm.registry instead of the deployed swarm-tmux one, and
    every launch crashed after the window was made (2026-11-15 03:44-03:51 ET, PRs #537-#542:
    "module 'tangentswarm.registry' has no attribute 'first_prompt_problem'")."""
    env = dict(os.environ if env is None else env)
    env.pop('PYTHONPATH', None)
    return env


def start_scoped(branch, window=None, refuse=None):
    """Run swarm-start-scoped. Returns (rc, json dict or {'refused': text}). A worktree that
    already has a live Claude is refused before anything runs (tangentswarm #40 item 8)."""
    from . import workerprocs
    why = (refuse or workerprocs.refuse_second_claude)(HOME / 'ver' / ('platform.' + branch.replace('/', '-')))
    if why:
        return 1, {'refused': why}
    r = subprocess.run([str(BIN / 'swarm-start-scoped'), branch, *([window] if window else [])],
                       capture_output=True, text=True, timeout=300, env=clean_env())
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {'refused': (r.stderr or r.stdout).strip()[-300:] or f'rc={r.returncode}'}
    return r.returncode, out


def brief_line(path):
    from .gatechecks import SHOTS_RULE
    return (f"Your brief is in {path}. Read the whole file first, then follow it. "
            "It names your branch, the issue, the tests and how to report. " + SHOTS_RULE)


def send_brief(pane, unit, path):
    """Wait for the new Claude's empty prompt in its scope, then send one line that points to the brief."""
    sys.path.insert(0, str(BIN))
    import swarm_parking as sp
    host = sp.Host()
    ok, why = sp.wait_ready(host, pane, unit)
    if not ok:
        raise RuntimeError(why)
    host.tell(pane, brief_line(path))


def launch(branch, window=None, pr=None, brief=None, start=start_scoped, send=send_brief):
    """Start a worker. Returns (what, detail): what is 'launched', 'held' or 'refused'."""
    rc, out = start(branch, window)
    if rc == HELD and out.get('held'):
        return 'held', out  # no list: the caller keeps the PR's unstarted record
    if rc != 0:
        return 'refused', out
    if brief:
        try:
            send(out['pane'], out['unit'], brief)
        except Exception as e:  # Claude runs; only the first message failed
            return 'launched', dict(out, brief_error=str(e))
    return 'launched', out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-launch', description=__doc__.split('\n', 1)[0])
    ap.add_argument('branch', nargs='?')
    ap.add_argument('window', nargs='?')
    ap.add_argument('--pr', type=int)
    ap.add_argument('--brief', help='brief file; the new Claude is told to read it')
    ap.add_argument('--memory', action='store_true', help='print the worker memory and exit')
    a = ap.parse_args(argv)
    try:
        if a.memory:
            print(describe(memory()) + ('; admits a launch' if admits(memory()) else '; holds launches'))
            return 0
        if not a.branch:
            ap.error('BRANCH is required')
        if a.brief and not Path(a.brief).is_file():
            ap.error(f'{a.brief} is not a file')
        what, out = launch(a.branch, a.window, a.pr, a.brief and str(Path(a.brief).resolve()))
        print(json.dumps(dict(what=what, **out)))
        return {'launched': 0, 'held': HELD}.get(what, 1)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print(f"swarm-launch error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
