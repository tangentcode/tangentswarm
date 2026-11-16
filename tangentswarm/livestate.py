"""The live state of the swarm agents (tangentswarm #44): what runs in each tmux pane now.
Read from tmux, /proc and the Claude session files; never from reservations or held lists.

One row per pane of the agents tmux session:

  window  claude pid  state (active / idle / no claude)  screen age  tests and other processes
  memory and CPU of the pane's process tree  branch  PR and card

- A Claude is running when a 'claude' process is in the pane's process tree (/proc).
- Active or idle: the Claude session status (busy / idle), and the 1-minute pane capture
  (#40): a pane whose screen changed in the last CAPTURE_EVERY s is active.
- CPU is the share of one CPU between two samples (a PaneWatch keeps the last sample).
- The PR and card come from the board snapshot file (boardcache.PATH); it is never fetched here.

host() is the live host load: load average, CPUs, MemAvailable, the worker slice memory, and
the Claudes that count against MAX_CLAUDES (slots.live_claudes). capacity() is the decision
from that state, the same rule launches.admits uses for every start.

  python -m tangentswarm.livestate [--json]   one sample (JSON for the sanity check, #43)
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import boardcache, launches, slots, workerprocs

SESSION = slots.TMUX_SESSION
CAPTURE_EVERY = float(os.environ.get('SWARM_CAPTURE_EVERY', '60'))  # s between captures of one pane
TESTS = re.compile(r'\b(pytest|py\.test|jest|vitest|playwright|mocha|tox|nox)\b|\bnpm (run )?test\b'
                   r'|\b(cargo|go|bun|deno) test\b|\bmake (test|check)\b|\bnode\b.*--test\b')
TICK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
PAGE = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
PANE_FMT = '#{pane_id}\t#{window_index}\t#{window_name}\t#{pane_pid}\t#{pane_current_path}'


# -- sources ----------------------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''


def procs(proc=Path('/proc'), uid=None):
    """{pid: {ppid, comm, cmd, ticks, rss}} for every process of ours (rss in bytes, ticks:
    user + system CPU clock ticks so far)."""
    uid = os.getuid() if uid is None else uid
    out = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            stat = (d / 'stat').read_text()
        except OSError:
            continue
        try:
            f = stat.rsplit(')', 1)[1].split()
            if f[0] in ('Z', 'X'):
                continue
            out[int(d.name)] = dict(ppid=int(f[1]), comm=_read(d / 'comm').strip(),
                                    cmd=_read(d / 'cmdline').replace('\0', ' ').strip(),
                                    ticks=int(f[11]) + int(f[12]), rss=int(f[21]) * PAGE)
        except (IndexError, ValueError):
            continue
    return out


def tree(table, root):
    """root and every descendant of it in table, root first."""
    kids = {}
    for pid, p in table.items():
        kids.setdefault(p['ppid'], []).append(pid)
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid in table and pid not in out:
            out.append(pid)
            todo += sorted(kids.get(pid, []), reverse=True)
    return out


def is_claude(p):
    return p['comm'] == 'claude' or (p['cmd'].split(' ')[0].endswith('/claude'))


def kind(p):
    """'claude', 'test', 'other', or None (a shell, tmux or an MCP server: not work)."""
    if is_claude(p):
        return 'claude'
    if p['comm'] in workerprocs.SHELLS or 'mcp' in p['cmd']:
        return None
    return 'test' if TESTS.search(p['cmd']) else 'other'


def sessions(path=None):
    """{pid: Claude session record} from ~/.claude/sessions/<pid>.json."""
    out = {}
    for f in sorted((path or slots.SESSIONS).glob('*.json')):
        try:
            d = json.loads(f.read_text())
            out[int(d.get('pid') or f.stem)] = d
        except (OSError, ValueError, TypeError):
            continue
    return out


def tmux_panes(session=SESSION, run=subprocess.run):
    """[{pane, index, window, pid, cwd}] for the panes of the tmux session ([] without tmux)."""
    try:
        r = run(['tmux', 'list-panes', '-s', '-t', session, '-F', PANE_FMT], capture_output=True, text=True,
                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in (r.stdout or '').splitlines():
        f = line.split('\t')
        if len(f) == 5 and f[3].isdigit():
            out.append(dict(pane=f[0], index=int(f[1]) if f[1].isdigit() else f[1], window=f[2], pid=int(f[3]),
                            cwd=f[4]))
    return out


def board_cards(path=None):
    """{branch: (PR number, card column)} from the board snapshot file ({} when none)."""
    try:
        snap = json.loads((path or boardcache.PATH).read_text())
        return {c.get('headRefName'): (c['number'], boardcache.col(it)) for it, c in boardcache.open_prs(snap)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def capture(pane, run=subprocess.run):
    try:
        r = run(['tmux', 'capture-pane', '-p', '-t', pane], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def branch_of(cwd):
    head = _read(Path(cwd) / '.git')
    if head.startswith('gitdir:'):  # a worktree
        head = _read(Path(head.split(':', 1)[1].strip()) / 'HEAD')
    else:
        head = _read(Path(cwd) / '.git' / 'HEAD')
    head = head.strip()
    return head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else ''


# -- the 1-minute pane capture and CPU samples --------------------------------------------------

class PaneWatch:
    """What one sample needs from the samples before it: each pane's last screen hash and when
    it last changed (captured at most every CAPTURE_EVERY s), and each process's CPU ticks."""

    def __init__(self, every=CAPTURE_EVERY, capture=capture):
        self.every, self.capture = every, capture
        self.screens = {}  # pane: {hash, changed, captured}
        self.ticks, self.at = {}, None

    def screen(self, pane, now):
        """Seconds since the pane's screen last changed, or None before a change was seen."""
        s = self.screens.get(pane)
        if s is None or now - s['captured'] >= self.every:
            text = self.capture(pane)
            if text is not None:
                h = hashlib.sha1(text.encode()).hexdigest()
                if s is None:
                    s = self.screens[pane] = dict(hash=h, changed=None, captured=now)
                else:
                    if h != s['hash']:
                        s['hash'], s['changed'] = h, now
                    s['captured'] = now
        return None if s is None or s['changed'] is None else now - s['changed']

    def cpu(self, table, now):
        """{pid: percent of one CPU} since the last call ({} on the first)."""
        dt = None if self.at is None else now - self.at
        out = {}
        if dt and dt > 0:
            for pid, p in table.items():
                if pid in self.ticks:
                    out[pid] = max(0.0, (p['ticks'] - self.ticks[pid]) / TICK / dt * 100)
        self.ticks, self.at = {pid: p['ticks'] for pid, p in table.items()}, now
        return out


# -- the rows -----------------------------------------------------------------------------------

def state_of(claude, sess, screen_age, every=CAPTURE_EVERY):
    """'active', 'idle' or 'no claude': not idle in its session record, or its screen changed
    in the last capture interval."""
    if claude is None:
        return 'no claude'
    if (sess or {}).get('status') not in (None, '', 'idle'):  # busy, or running a shell command
        return 'active'
    if screen_age is not None and screen_age <= every * 1.5:
        return 'active'
    return 'idle'


def agent_rows(panes, table, sess, cards, cpu=None, screen=None, now=None, branch=None):
    """[row] for each pane: {pane, index, window, cwd, branch, claude, status, state, idle_s,
    screen_s, tests, other, mem, cpu, pr, card}. tests and other: [(pid, command)]."""
    now = time.time() if now is None else now
    cpu, branch = cpu or {}, branch or branch_of
    rows = []
    for p in panes:
        pids = tree(table, p['pid'])
        work = {pid: kind(table[pid]) for pid in pids}
        claude = next((pid for pid in pids if work[pid] == 'claude'), None)
        s = sess.get(claude) if claude else None
        age = screen(p['pane'], now) if screen else None
        idle_s = None
        if s and s.get('status') == 'idle' and s.get('statusUpdatedAt'):
            idle_s = max(0, int(now - float(s['statusUpdatedAt']) / 1000))
        cwd = (s or {}).get('cwd') or p['cwd']
        br = branch(cwd) if cwd else ''
        pr, card = cards.get(br, (None, None)) if br else (None, None)
        rows.append(dict(
            pane=p['pane'], index=p['index'], window=p['window'], cwd=cwd, branch=br, claude=claude,
            status=(s or {}).get('status') or ('' if claude is None else '?'),
            state=state_of(claude, s, age), idle_s=idle_s, screen_s=None if age is None else int(age),
            tests=[(pid, table[pid]['cmd'][:120]) for pid in pids if work[pid] == 'test'],
            other=[(pid, table[pid]['cmd'][:120]) for pid in pids if work[pid] == 'other'],
            mem=sum(table[pid]['rss'] for pid in pids), cpu=round(sum(cpu.get(pid, 0.0) for pid in pids), 1),
            pr=pr, card=card))
    return rows


def host(mem=launches.memory, counted=slots.live_claudes, loadavg=os.getloadavg, cpus=os.cpu_count):
    """{load, cpus, mem (launches.memory, or None), mem_error, claudes, max_claudes}."""
    try:
        m, err = mem(), ''
    except Exception as e:
        m, err = None, f"{type(e).__name__}: {e}"
    try:
        n = counted()
    except Exception:
        n = None
    return dict(load=list(loadavg()), cpus=cpus() or 1, mem=m, mem_error=err, claudes=n,
                max_claudes=slots.MAX_CLAUDES)


def capacity(h):
    """(free Claude starts, why none) from the host state: 0 while the memory or the CPU load
    holds starts (launches.admits), or the counted Claudes reach MAX_CLAUDES."""
    m = h.get('mem')
    if m is not None:
        m = dict(m, load=h['load'], cpus=h['cpus'])
        if not launches.admits(m):
            if not launches.cpu_admits(m):
                return 0, f"CPU load {m['load'][1]:.2f} over {launches.load_max(m['cpus']):.1f} ({m['cpus']} CPUs)"
            return 0, f"worker memory holds starts ({launches.describe(m)})"
    elif not launches.cpu_admits(dict(load=h['load'], cpus=h['cpus'])):
        return 0, f"CPU load {h['load'][1]:.2f} over {launches.load_max(h['cpus']):.1f} ({h['cpus']} CPUs)"
    n = h.get('claudes')
    if n is None:
        return 0, 'cannot count the running Claudes'
    if n >= h['max_claudes']:
        return 0, f"{n} Claudes run (cap {h['max_claudes']})"
    return h['max_claudes'] - n, ''


def collect(watch=None, now=None, panes=None, table=None, sess=None, cards=None, hst=None):
    """One sample of the live state: {at, host, capacity: {free, why}, agents: [row]}. JSON-safe,
    for the Agents screen and the sanity check (#43)."""
    now = time.time() if now is None else now
    table = procs() if table is None else table
    cpu = watch.cpu(table, now) if watch else {}
    h = host() if hst is None else hst
    free, why = capacity(h)
    rows = agent_rows(tmux_panes() if panes is None else panes, table, sessions() if sess is None else sess,
                      board_cards() if cards is None else cards, cpu, watch.screen if watch else None, now)
    return dict(at=now, host=h, capacity=dict(free=free, why=why), agents=rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-livestate', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--json', action='store_true', help='print the sample as JSON')
    a = ap.parse_args(argv)
    watch = PaneWatch()
    collect(watch)  # the first CPU sample
    time.sleep(1)
    st = collect(watch)
    if a.json:
        print(json.dumps(st, indent=1, default=str))
    else:
        from . import agents_view
        print('\n'.join(agents_view.render(st, color=False)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
