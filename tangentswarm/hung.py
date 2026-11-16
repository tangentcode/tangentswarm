"""Hung test and automation processes in worker worktrees (tangentswarm #40 item 4d).

Michal 2026-11-15 6:56 PM ET: a hung process needs no OK from him. The watcher kills it,
then wakes swarmbot with the process facts and the pane capture to diagnose why it hung.

A process is hung when its command is a test or automation run (TEST_RE), its working
directory is a worker worktree, it is older than HUNG_MIN, and its CPU time has not grown
for FLAT_MIN (or it is older than HARD_MIN). The kill is TERM, then KILL for the process
and its children. A shell, a Claude, tmux and the swarm's own tools are never killed.
"""
import os
import re
import signal
import time
from pathlib import Path

HUNG_MIN = int(os.environ.get('SWARM_HUNG_MIN', '1800'))     # s: younger runs are never hung
FLAT_MIN = int(os.environ.get('SWARM_HUNG_FLAT', '600'))     # s with no CPU growth
HARD_MIN = int(os.environ.get('SWARM_HUNG_HARD', '7200'))    # s: hung whatever the CPU does
TEST_RE = re.compile(r'pytest|test-slot|vitest|jest|playwright|mocha|cypress|\b(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test'
                     r'|\bgo test\b|\bcargo test\b|\btox\b|unittest|--headless', re.I)
NEVER = re.compile(r'(?:^|/)(?:claude|tmux|swarm-[\w-]+|hr-gate|active-gate|bash|sh|zsh|fish|dash)(?:\s|$)')


def _stat(proc, pid):
    """(state, ppid, cpu ticks, start ticks) from /proc/pid/stat."""
    s = (proc / str(pid) / 'stat').read_text()
    f = s.rsplit(')', 1)[1].split()
    return f[0], int(f[1]), int(f[11]) + int(f[12]), int(f[19])


def boot_time(proc=Path('/proc')):
    for line in (proc / 'stat').read_text().splitlines():
        if line.startswith('btime '):
            return int(line.split()[1])
    return 0


def sample(in_worker, proc=Path('/proc'), uid=None, hz=None, btime=None):
    """{pid: {cmd, cwd, cpu, started, ppid}} of this user's test and automation processes
    whose cwd is in a worker worktree (in_worker(cwd) -> worktree path or None)."""
    uid = os.getuid() if uid is None else uid
    hz = hz or os.sysconf('SC_CLK_TCK')
    btime = boot_time(proc) if btime is None else btime
    out = {}
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            cmd = (d / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
            if not cmd or not TEST_RE.search(cmd) or NEVER.search(cmd.split(' ', 1)[0]):
                continue
            wt = in_worker(os.readlink(d / 'cwd'))
            if not wt:
                continue
            state, ppid, cpu, start = _stat(proc, d.name)
        except (OSError, ValueError, IndexError):
            continue
        if state in ('Z', 'X'):
            continue
        out[int(d.name)] = dict(cmd=cmd[:300], cwd=wt, cpu=cpu, started=btime + start / hz, ppid=ppid)
    return out


def track(prev, cur, now):
    """Carry each pid's flat-CPU start: {pid: dict(cur fields, flat_since)}."""
    out = {}
    for pid, p in cur.items():
        old = prev.get(pid)
        same = old and old.get('started') == p['started']
        flat = old['flat_since'] if same and old.get('cpu') == p['cpu'] else now
        out[pid] = dict(p, flat_since=flat)
    return out


def hung(tracked, now, hung_min=HUNG_MIN, flat_min=FLAT_MIN, hard_min=HARD_MIN):
    """[(pid, why)] of the hung processes, oldest first. A child of another hung process
    is left to that parent's kill."""
    out = []
    for pid, p in tracked.items():
        age = now - p['started']
        if age < hung_min:
            continue
        flat = now - p['flat_since']
        if flat >= flat_min:
            out.append((pid, f"running {int(age // 60)} min, no CPU time for {int(flat // 60)} min"))
        elif age >= hard_min:
            out.append((pid, f"running {int(age // 60)} min (over {hard_min // 60} min)"))
    pids = {pid for pid, _ in out}
    out = [(pid, why) for pid, why in out if tracked[pid].get('ppid') not in pids]
    return sorted(out, key=lambda x: tracked[x[0]]['started'])


def children(pid, proc=Path('/proc')):
    """All descendants of pid (from /proc/*/stat ppid)."""
    kids = {}
    for d in proc.iterdir():
        if d.name.isdigit():
            try:
                kids.setdefault(_stat(proc, d.name)[1], []).append(int(d.name))
            except (OSError, ValueError, IndexError):
                continue
    out, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.append(k)
            todo.append(k)
    return out


def kill(pid, proc=Path('/proc'), signal_fn=os.kill, sleep=time.sleep, grace=5):
    """TERM pid and its children, then KILL what is left. Returns the pids signalled."""
    pids = [pid] + children(pid, proc)
    for p in pids:
        try:
            signal_fn(p, signal.SIGTERM)
        except OSError:
            pass
    sleep(grace)
    for p in pids:
        if (proc / str(p)).exists():
            try:
                signal_fn(p, signal.SIGKILL)
            except OSError:
                pass
    return pids
