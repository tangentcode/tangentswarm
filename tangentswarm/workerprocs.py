"""Processes a worker Claude left running in its worktree (MethBot 2026-11-15 4:31 AM ET).

#532's worker left a dev server (tcode-server.py, pid 2790171) running for 1h45m after its
work was done; park refused ("test-slot process belongs to this worker") and only a person
could stop it. Now the Stop hook does not hand off while such a process runs, and park
relays the leftovers to the worker, so the worker stops them itself.

A leftover is a process of ours whose working directory is the worktree (or below it),
except a shell, the Claude itself, its MCP servers, and tmux.
"""
import os
from pathlib import Path

SHELLS = {'bash', 'sh', 'zsh', 'dash', 'fish', 'tmux', 'tmux: server', 'claude'}


def _read(path, default=''):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return default


def leftovers(worktree, proc=Path('/proc'), uid=None):
    """[(pid, command line)] of the leftover processes in worktree, lowest pid first."""
    wt = str(worktree).rstrip('/')
    uid = os.getuid() if uid is None else uid
    out = []
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            cwd = os.readlink(d / 'cwd')
        except OSError:
            continue
        if not (cwd == wt or cwd.startswith(wt + '/')):
            continue
        comm = _read(d / 'comm').strip()
        cmd = _read(d / 'cmdline').replace('\0', ' ').strip()
        if comm in SHELLS or '/claude' in cmd.split(' ')[0] or 'mcp' in cmd:
            continue
        out.append((int(d.name), cmd[:160] or comm))
    return sorted(out)


def stop_text(n, procs):
    """The one-line relay that asks the worker to stop its leftover processes."""
    listed = '; '.join(f"pid {pid} ({cmd[:80]})" for pid, cmd in procs[:5])
    return (f"Coordinator: #{n}: before the handoff, stop every dev server and background process you "
            f"started in this worktree. Still running: {listed}. Stop them (kill the pid you started), "
            "check that none is left, then end your turn again.")


def claudes_in(worktree, proc=Path('/proc'), uid=None):
    """[(pid, command line)] of the live Claude processes whose cwd is the worktree or below
    it (tangentswarm #40 item 8: one Claude per worktree, in tmux or not)."""
    from . import registry
    wt = str(worktree).rstrip('/')
    uid = os.getuid() if uid is None else uid
    out = []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            cwd = os.readlink(d / 'cwd')
            stat = _read(d / 'stat')
            if stat and stat.rsplit(')', 1)[-1].split()[:1] in (['Z'], ['X']):
                continue
        except OSError:
            continue
        if not (cwd == wt or cwd.startswith(wt + '/')):
            continue
        comm = _read(d / 'comm').strip()
        cmd = _read(d / 'cmdline').replace('\0', ' ').strip()
        if registry.classify_process(comm, cmd) == 'claude':
            out.append((int(d.name), cmd[:160] or comm))
    return sorted(out)


def refuse_second_claude(worktree, find=None):
    """The refusal text when a Claude already runs in the worktree, else None."""
    found = (find or claudes_in)(worktree)
    if not found:
        return None
    return (f"a Claude already runs in {worktree} (pid {', '.join(str(p) for p, _ in found)}): "
            "one Claude per worktree; not starting another")
