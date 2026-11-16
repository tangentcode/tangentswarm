"""Claude slots: new-work and rework (Michal 2026-11-14 8:43 PM ET).

Of the usual 5 to 8 running Claudes:
  new work  3 to 4 slots. First implementations of Backlog issues, each on its own branch.
            Never stopped. Finished PRs pile up in Ready, which is the buffer.
  rework    2 to 3 slots. A parked Claude is revived with `claude --resume SESSION` plus
            feedback (swarm-revive).

  (a) park     A PR in Ready: its Claude exits (/exit, through swarm_parking.park_one, which
               needs an idle Claude at an empty prompt and a clean, pushed worktree). The
               tmux window, the bash shell, the branch and the worktree stay. The Claude
               session id is recorded first, so that swarm-revive can resume it.
  (b) revive   swarm-revive PR FEEDBACK: resume that session in the PR's window in a rework
               slot, then send the feedback as one line.
  (c) signal   START_CLAUDE while fewer than NEW_SLOTS new-work Claudes run and Backlog issues
               without a PR exist (up to NEW_SLOTS_SHORT while Ready is short). The picker
               (backlog_issues) takes only the Backlog column, never Parked, Triage or the
               NEVER_START list (#377 is held). Order: launch_key, a pure sort (fast-track
               label, then the nearest milestone due date, then the lowest number).

A running Claude is found from ~/.claude/sessions/<pid>.json (pid, sessionId, cwd, tmux
pane); only live Claudes in platform worktrees (~/ver/platform.*) count. A rework Claude is
one swarm-revive started; every other one is new work.

State: ~/.local/state/swarm-flow/slots.json
  parked  {PR: {session, window, pane, worktree, branch, head, parked_at}}
  rework  {PR: {session, window, pane, worktree, branch, head, started, feedback}}
  relayed {PR: head} the head a rework relay was sent for (the cascade sends one per head)

    swarm-slots              the running Claudes by slot, and the parked sessions
    swarm-slots --park [--dry-run]   park the Claude of each PR in Ready now
    swarm-revive PR TEXT...  resume PR's Claude in a rework slot and send TEXT (- reads stdin)
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import board

HOME = Path.home()
REPO = 'tangentcode/platform'
STATE = Path(os.environ.get('SWARM_SLOTS_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'slots.json')))
SESSIONS = Path(os.environ.get('SWARM_CLAUDE_SESSIONS', str(HOME / '.claude' / 'sessions')))
PROJECTS = Path(os.environ.get('SWARM_CLAUDE_PROJECTS', str(HOME / '.claude' / 'projects')))
WORKTREE_PARENT = Path(os.environ.get('SWARM_WORKTREE_PARENT', str(HOME / 'ver')))
NEW_SLOTS = int(os.environ.get('SWARM_NEW_SLOTS', '3'))              # new-work Claudes to keep running
NEW_SLOTS_SHORT = int(os.environ.get('SWARM_NEW_SLOTS_SHORT', '4'))  # ... while Ready is short
REWORK_SLOTS = int(os.environ.get('SWARM_REWORK_SLOTS', '3'))
# The launch picker never names these (the swarmbot handoff: parked items and held #377).
# Parked cards are also skipped by column (Parked) and by the label "parked".
NEVER_START = {int(x) for x in os.environ.get(
    'SWARM_NEVER_START', '21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,'
    '110,116,117,120,125,202,203,204,344,350,377').split(',') if x.strip().isdigit()}
LAUNCH_GRACE = int(os.environ.get('SWARM_LAUNCH_GRACE', '2700'))  # s: a named issue is not named again
LAUNCHING_TTL = int(os.environ.get('SWARM_LAUNCHING_TTL', '1800'))  # s: a queued launch, until its PR exists
MAX_CLAUDES = int(os.environ.get('SWARM_MAX_CLAUDES', '10'))  # every live Claude on the host
# tangentswarm #65 (Michal 2026-11-16 3:17 PM ET): a conflict fix for fill never waits for
# a rework slot; the only limits are the live Claudes and the host MemAvailable.
CONFLICT_MAX_CLAUDES = int(os.environ.get('SWARM_CONFLICT_MAX_CLAUDES', '9'))
CONFLICT_AVAIL_MIN = int(float(os.environ.get('SWARM_CONFLICT_AVAIL_MIN_GB', '1.5')) * 1024 ** 3)
TMUX_SESSION = 'agents'
FEEDBACK_MAX = 4000


# -- state ------------------------------------------------------------------------

def load(path=None):
    try:
        d = json.loads((path or STATE).read_text())
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    return {'parked': dict(d.get('parked') or {}), 'rework': dict(d.get('rework') or {}),
            'relayed': dict(d.get('relayed') or {}), 'named': dict(d.get('named') or {}),
            'launching': dict(d.get('launching') or {}), 'unstarted': dict(d.get('unstarted') or {}),
            'told': dict(d.get('told') or {})}


def save(d, path=None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')  # one per writer
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, path)


@contextlib.contextmanager
def update(path=None):
    """The state for one read-modify-write, under an interprocess lock (Codex on PR #26: the
    spool's launchers, the cascade and park wrote the file at the same time, and one could
    drop another's unstarted or parked record). It is saved on exit when it changed. Never
    nest two update() blocks: flock on a second fd of the same file would wait for itself."""
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + '.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            d = load(path)
            before = json.dumps(d, sort_keys=True)
            yield d
            if json.dumps(d, sort_keys=True) != before:
                save(d, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def et_now():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET')
    except Exception:
        return datetime.now().strftime('%Y-%m-%d %H:%M')


# -- running Claudes ----------------------------------------------------------------

def is_platform_worktree(cwd):
    """A worker worktree ~/ver/platform.<slug> (the main checkout ~/ver/platform is not one)."""
    p = Path(cwd or '')
    return p.parent == WORKTREE_PARENT and p.name.startswith('platform.')


def _alive(pid):
    """True while pid is a live claude process (a reused pid runs something else)."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        if stat.rsplit(')', 1)[1].split()[0] in ('Z', 'X'):
            return False
        return b'claude' in Path(f'/proc/{pid}/cmdline').read_bytes()
    except (OSError, IndexError):
        return False


def _git(cwd, *args):
    try:
        r = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


def running(sessions=None, alive=_alive, branch_of=None):
    """[{pid, session, cwd, pane, status, branch}] for live Claudes in platform worktrees."""
    branch_of = branch_of or (lambda cwd: _git(cwd, 'symbolic-ref', '--short', '-q', 'HEAD'))
    out = []
    for f in sorted((sessions or SESSIONS).glob('*.json')):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get('pid') or f.stem)
        except (OSError, ValueError, TypeError):
            continue
        if not is_platform_worktree(d.get('cwd')) or not alive(pid):
            continue
        tm = d.get('tmux') or ''  # "agents:@<window>.%<pane>"
        pane = tm.rsplit('.', 1)[-1] if tm.startswith(TMUX_SESSION + ':') else ''
        out.append(dict(pid=pid, session=d.get('sessionId') or '', cwd=d['cwd'], pane=pane,
                        status=d.get('status') or '', branch=branch_of(d['cwd'])))
    return out


def attach_prs(claudes, prs):
    """Set c['pr'] and c['col'] from the open PR whose head branch the worktree is on (None:
    no PR yet)."""
    by_branch = {p.get('branch'): n for n, p in prs.items() if p.get('state', 'OPEN') == 'OPEN'}
    for c in claudes:
        c['pr'] = by_branch.get(c.get('branch'))
        c['col'] = (prs.get(c['pr']) or {}).get('col')
    return claudes


DONE_COLS = (board.READY, board.STAGING, board.HUMAN_REVIEW)


def kinds(claudes, state):
    """{'new', 'rework', 'done'}: rework when swarm-revive started it; done when its PR is in
    Ready or later and it waits to be parked (it holds no new-work slot); else new work."""
    rw = state.get('rework') or {}
    out = {'new': [], 'rework': [], 'done': []}
    for c in claudes:
        r = rw.get(str(c.get('pr'))) or {}
        mine = r and (r.get('worktree') == c.get('cwd') or (r.get('session') and r.get('session') == c.get('session')))
        out['rework' if mine else 'done' if c.get('col') in DONE_COLS else 'new'].append(c)
    return out


def latest_session(worktree, projects=None):
    """The newest Claude session id for a worktree, from ~/.claude/projects (a fallback when
    nothing was recorded at park time)."""
    d = (projects or PROJECTS) / re.sub(r'[^A-Za-z0-9]', '-', str(worktree))
    try:
        files = sorted(d.glob('*.jsonl'), key=lambda f: f.stat().st_mtime)
    except OSError:
        return None
    return files[-1].stem if files else None


# -- (c) Backlog issues without a PR: the launch picker ----------------------------------------------------

def launch_key(issue):
    """The launch sort key of one issue (Michal 2026-11-15 12:31 AM ET: a deterministic sort,
    no LLM choice). A pure function of {number, labels, milestone, due}:
      (1) fast-track first (the label "fast-track", or the active fast-track: issue['ft']);
      (2) then the milestone due date, nearest first; a milestone without a due date after
          every dated one; no milestone last;
      (3) then the lowest issue number."""
    return board.pick_key(issue)  # the one shared pick order (tangentswarm #35)


def key_text(issue):
    """The sort key as START_CLAUDE prints it, e.g. #507=(ft:no,due:2026-11-20 ptw 15,n:507)."""
    ft, _, _, n = launch_key(issue)
    due = (issue.get('due') or '')[:10]  # from the issue: a fast-track key holds no due date
    when = f"{due} {issue.get('milestone')}" if due else f"none {issue.get('milestone')}" if issue.get('milestone') else 'none'
    return f"#{n}=(ft:{'yes' if ft == 0 else 'no'},due:{when},n:{n})"


def taken_issues(snap, prs=None):
    """The issues an open PR closes (board snapshot and board PRs)."""
    from . import boardcache
    taken = {i['number'] for _, c in boardcache.open_prs(snap) for i in boardcache.closes(c)}
    return taken | {i for p in (prs or {}).values() if p.get('state', 'OPEN') == 'OPEN' for i in p.get('closes') or ()}


def backlog_issues(snap, active_ft=None, prs=None, named=None, now=None, launching=None):
    """The launch picker (Michal 2026-11-15 12:16 AM ET: START_CLAUDE named Parked #344 while
    Backlog held the approved issues). Open platform issues in the BACKLOG column (by option
    id; so never Triage, Inbox, Discuss, Active or Parked) that no open PR closes, without the
    label "parked" or "triage", not in NEVER_START, and not named by START_CLAUDE in the last
    LAUNCH_GRACE s (named: {number: time}; its Claude has no PR yet). Sorted by launch_key.
    snap: the board snapshot (boardcache); prs: board PRs ({n: {closes}})."""
    from . import boardcache
    now = time.time() if now is None else now
    taken = taken_issues(snap, prs)
    recent = {int(k) for k, t in (named or {}).items() if now - float(t) < LAUNCH_GRACE}
    recent |= {int(k) for k, t in (launching or {}).items() if now - float(t) < LAUNCHING_TTL}
    out = []
    for it, c in boardcache.open_issues(snap):
        n = c['number']
        if boardcache.col(it) != board.BACKLOG or n in taken or n in NEVER_START or n in recent:
            continue
        labels = [l['name'] for l in (c.get('labels') or {}).get('nodes') or []]
        if {'parked', 'triage'} & {l.lower() for l in labels}:
            continue
        m = c.get('milestone') or {}
        out.append(dict(number=n, title=c.get('title') or '', labels=labels, milestone=m.get('title'),
                        due=m.get('dueOn'), ft=board.is_active_fast_track(n, (), active_ft)))
    return sorted(out, key=launch_key)


def launch_mark(numbers, path=None, now=None):
    """Issues whose launch is queued: the picker skips them until the launch ends."""
    now = time.time() if now is None else now
    with update(path) as d:
        d['launching'] = {k: t for k, t in d['launching'].items() if now - float(t) < LAUNCHING_TTL}
        d['launching'].update({str(n): now for n in numbers})


def unstarted_add(pr, path=None, now=None, **rec):
    """A launch made the PR and its window: until a claude runs there, the PR is unstarted.
    Each signal pass retries it, and it takes a new-work slot meanwhile."""
    with update(path) as d:
        d['unstarted'][str(pr)] = dict(rec, at=time.time() if now is None else now, queued=0)


def brief_pending(pr, pane, unit, brief, branch='', issue=None, path=None, now=None):
    """Its Claude runs, but the brief line was not sent (Codex on PR #26: the launch counted
    as done and the Claude had no task). The unstarted record keeps the pane, so the start
    retry (retry_starts -> start_existing) sends only the brief line."""
    with update(path) as d:
        rec = d['unstarted'].setdefault(str(pr), {})
        rec.update(branch=branch or rec.get('branch', ''), brief=str(brief), setup='done', brief_pane=pane,
                   unit=unit, at=time.time() if now is None else now, queued=0)
        if issue is not None:
            rec['issue'] = issue


def unstarted_done(pr, path=None):
    with update(path) as d:
        d['unstarted'].pop(str(pr), None)


def unstarted_retry(pr, path=None):
    """The retry failed: it may be queued again on a later pass."""
    with update(path) as d:
        if str(pr) in d['unstarted']:
            d['unstarted'][str(pr)]['queued'] = 0


def launch_done(n, path=None):
    """The launch of issue n ended (its draft PR exists, or it failed and may be tried again)."""
    with update(path) as d:
        d['launching'].pop(str(n), None)


def live_claudes(sessions=None, alive=None):
    """How many Claudes count against the MAX_CLAUDES cap. MethBot 2026-11-15 4:13 AM ET asked
    whether non-swarm Claudes count: every platform worker counts (busy or idle), and any
    other Claude counts while it is not idle (busy, or running a shell command). An idle
    non-worker Claude (such as implish's imp-22, idle since 11 PM) uses no CPU, and
    swarm-start-scoped's memory admission guards the RAM."""
    alive = alive or _alive
    n = 0
    for f in (sessions or SESSIONS).glob('*.json'):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get('pid') or f.stem)
        except (OSError, ValueError, TypeError):
            continue
        if alive(pid) and (is_platform_worktree(d.get('cwd')) or (d.get('status') or 'idle') != 'idle'):
            n += 1
    return n


def claude_load(spool=None):
    """What a new Claude start is admitted against (MAX_CLAUDES), from live state only
    (tangentswarm #44, Michal 2026-11-15 7:17 PM ET: never from reservations or held
    lists): the Claudes that run now, plus the launch and start requests waiting or
    running in the spool (each is a Claude about to start)."""
    from . import spool as _spool
    return live_claudes() + len(_spool.in_flight(spool=spool))


def mem_available(meminfo=None):
    """The host MemAvailable in bytes, read live from /proc/meminfo, or None when unreadable."""
    try:
        text = meminfo() if meminfo else Path('/proc/meminfo').read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    return None


def conflict_capacity(load=None, avail=None):
    """(ok, why): may a conflict fix start a Claude? Only CONFLICT_MAX_CLAUDES live Claudes
    (claude_load: running plus starts in the spool) and CONFLICT_AVAIL_MIN host MemAvailable
    hold it (tangentswarm #65); no rework-slot limit."""
    busy = claude_load() if load is None else load
    if busy >= CONFLICT_MAX_CLAUDES:
        return False, f"{busy} Claudes run or start (cap {CONFLICT_MAX_CLAUDES})"
    avail = mem_available() if avail is None else avail
    if avail is not None and avail < CONFLICT_AVAIL_MIN:
        return False, f"MemAvailable {avail // 1024 ** 2} MB (needs {CONFLICT_AVAIL_MIN // 1024 ** 2} MB)"
    return True, ''


def worker_memory():
    """The worker slice memory, read live (launches.memory), or None when unknown."""
    from . import launches
    return launches.memory()


def capacity(spool=None, mem=None, mine=0):
    """(free Claude starts, why none): MAX_CLAUDES minus claude_load(), and 0 while the
    worker memory does not admit a start (read live). mine: requests of the caller's own
    that claude_load() already counts (a launch checking its own capacity)."""
    from . import launches
    try:
        m = (mem or worker_memory)()
        if m is not None and not launches.admits(m):
            return 0, f"worker memory holds starts ({launches.describe(m)})"
    except Exception:
        pass  # memory unreadable: the Claude count still caps
    load = claude_load(spool) - mine
    if load >= MAX_CLAUDES:
        return 0, f"{load} Claudes run or start (cap {MAX_CLAUDES})"
    return MAX_CLAUDES - load, ''


def prune_named(taken, path=None, now=None):
    """Drop stale 'named' entries: an issue named by a pass that never launched it (before
    the launcher) has no open PR, and its entry hid it from the picker for 45 minutes
    (2026-11-15: #399-#431, #502, #503). Named means "its draft PR exists", so an entry
    whose issue no open PR closes (and older than 10 minutes: a new PR can lag in the board
    snapshot) is dropped. Returns the dropped issue numbers."""
    now = time.time() if now is None else now
    with update(path) as d:
        drop = [k for k, t in d['named'].items()
                if int(k) not in taken and now - float(t) > 600]  # a new PR may lag in the snapshot
        for k in drop:
            d['named'].pop(k)
    return sorted(int(k) for k in drop)


def mark_named(numbers, path=None, now=None):
    """Keep the issues START_CLAUDE named, so the next pass does not name them again while
    their Claude has no PR yet. Entries older than LAUNCH_GRACE are dropped."""
    now = time.time() if now is None else now
    with update(path) as d:
        d['named'] = {k: t for k, t in d['named'].items() if now - float(t) < LAUNCH_GRACE}
        d['named'].update({str(n): now for n in numbers})


def start_plan(new_running, issues, ready, ready_target):
    """(n, target): how many new-work Claudes to start. The target is NEW_SLOTS, or
    NEW_SLOTS_SHORT while Ready is below ready_target; never more than the issues waiting."""
    target = NEW_SLOTS_SHORT if ready < ready_target else NEW_SLOTS
    return max(0, min(target - new_running, len(issues))), target


# -- (a) park the Claude of a PR in Ready ------------------------------------------------

def parking():
    """The live ~/bin/swarm_parking module: tmux, /exit and launch mechanics, with its own
    checks (idle Claude at an empty prompt, clean and pushed worktree, no test slot)."""
    from .promoted import BIN
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import swarm_parking
    return swarm_parking


# swarm_parking.Host drives tmux with the deployed tangentswarm checkout (agents, registry,
# tmux: the bash-shell launch rules). A caller that puts another checkout first on sys.path
# (the swarm-advance and swarm-revive wrappers) would give it older modules, so the Host gets
# them from TMUX_SRC under their own package name.
TMUX_SRC = Path(os.environ.get('SWARM_TMUX_SRC', str(HOME / 'srv' / 'swarm-tmux')))
TMUX_PKG = 'swarm_tmux_live'


def _tmux_modules(src=None):
    import importlib
    import importlib.util
    if TMUX_PKG not in sys.modules:
        init = (src or TMUX_SRC) / 'tangentswarm' / '__init__.py'
        spec = importlib.util.spec_from_file_location(TMUX_PKG, init, submodule_search_locations=[str(init.parent)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[TMUX_PKG] = mod
        spec.loader.exec_module(mod)
    return [importlib.import_module(f'{TMUX_PKG}.{m}') for m in ('agents', 'registry', 'tmux')]


def make_host(sp):
    """swarm_parking.Host with the deployed tmux modules (see TMUX_SRC); sp.Host() when that
    checkout is missing."""
    if not (TMUX_SRC / 'tangentswarm' / 'registry.py').exists():
        return sp.Host()
    agents, registry, tmux = _tmux_modules()
    h = sp.Host.__new__(sp.Host)
    h.agents, h.tmux, h.registry = agents, tmux, registry
    h.tmux_run = tmux.run_checked
    h.env = dict(os.environ, XDG_RUNTIME_DIR=sp.RUNTIME_DIR)
    return h


def _park(c, out):
    """/exit the Claude in pane c['pane'] through swarm_parking.park_one. Sets c['window'].
    Returns None when parked, else why not."""
    sp = parking()
    host = make_host(sp)
    with sp.flocked('park.lock', timeout=120) as got:
        if not got:
            return 'another swarm-park holds the park lock'
        p = next((p for p in host.panes() if p['pane'] == c['pane']), None)
        if not p:
            return f"pane {c['pane']} is not in the {sp.SESSION} tmux session"
        c['window'] = p['window']
        try:
            sp.park_one(host, p, log=lambda s: out('park: ' + s))
        except sp.Refused as e:
            return str(e)
    return None


IDLE_PARK = int(os.environ.get('SWARM_IDLE_PARK', '1800'))  # s idle before a non-PR worker is parked
WORKER_WINDOW = re.compile(r'^[a-z]+-\d+-')                    # code-nnn-branch
NO_PARK_PREFIXES = tuple(os.environ.get('SWARM_NO_PARK', 'meth-').split(','))  # coordinator sessions


def idle_others(sessions=None, alive=None, now=None, windows=None):
    """Michal 2026-11-15 4:40 AM ET: idle agents are closed with /exit, keeping the window. The
    Claudes that no PR card governs: a live Claude outside the platform worktrees, in a
    worker window (code-nnn-branch, such as imp-22-rtl-eval) of the agents tmux session,
    idle for IDLE_PARK s or more (statusUpdatedAt). Coordinator windows (NO_PARK_PREFIXES)
    are never parked. Returns [{pid, session, cwd, pane, window, idle_s}]."""
    alive = alive or _alive
    now = time.time() if now is None else now
    windows = windows if windows is not None else _pane_windows()
    out = []
    for f in sorted((sessions or SESSIONS).glob('*.json')):
        try:
            d = json.loads(f.read_text())
            pid = int(d.get('pid') or f.stem)
        except (OSError, ValueError, TypeError):
            continue
        if not alive(pid) or is_platform_worktree(d.get('cwd')) or d.get('status') != 'idle':
            continue
        tm = d.get('tmux') or ''
        if not tm.startswith(TMUX_SESSION + ':'):
            continue
        pane = tm.rsplit('.', 1)[-1]
        window = windows.get(pane, '')
        if not WORKER_WINDOW.match(window) or window.startswith(NO_PARK_PREFIXES):
            continue
        idle = now - float(d.get('statusUpdatedAt') or d.get('updatedAt') or now * 1000) / 1000
        if idle >= IDLE_PARK:
            out.append(dict(pid=pid, session=d.get('sessionId') or '', cwd=d.get('cwd') or '', pane=pane,
                            window=window, idle_s=int(idle)))
    return out


def _pane_windows():
    r = subprocess.run(['tmux', 'list-panes', '-s', '-t', TMUX_SESSION, '-F', '#{pane_id} #{window_name}'],
                       capture_output=True, text=True, timeout=30)
    return dict(line.split(' ', 1) for line in (r.stdout or '').splitlines() if ' ' in line)


def park_idle_others(dry=False, out=print, found=None, park=None):
    """/exit each idle non-PR worker (idle_others), keeping its window and session record.
    Returns the parked windows."""
    done = []
    for c in (idle_others() if found is None else found):
        mins = c['idle_s'] // 60
        if dry:
            out(f"park: would park {c['window']} ({c['pane']}): idle {mins} min, no PR card")
            continue
        why = (park or _park)(c, out)
        if why:
            out(f"park: {c['window']} not parked: {why}")
            continue
        with update() as st:
            st['parked'][c['window']] = dict(session=c['session'], pane=c['pane'], worktree=c['cwd'],
                                             window=c['window'], parked_at=et_now())
        done.append(c['window'])
        out(f"park: {c['window']} parked after {mins} min idle; session {c['session']} kept; its window stays")
        _event('park', actor=c['window'], notes=[f"idle {mins} min, no PR card; session {c['session']}"])
    return done


def revive_reason(text):
    """Why a Claude was revived, from its feedback: 'shots' for the screenshot job."""
    return 'shots' if 'needs screenshots before Human Review' in (text or '') else 'rework'


def rework_open(rw, pr, head):
    """Why a rework Claude whose PR is in Ready must keep running, or None (park it).
    MethBot 2026-11-15 3:35 AM ET: #519's worker added the screenshots to the body (no new
    head) and stayed in a rework slot, so rework=3/3 held #516 with NEEDS_WORKER.
      - revived for the screenshot job: it runs until the body has an image;
      - revived for anything else: the card is back in Ready, so the reason is gone."""
    if not rw:
        return None
    if (rw.get('reason') or revive_reason(rw.get('feedback'))) == 'shots':
        from .gatechecks import image_urls
        if not image_urls(pr.get('body') or ''):
            return 'its PR body has no screenshot yet'
    return None


def park_ready(prs, cols, dry=False, out=print, claudes=None, park=None, path=None):
    """(a) Park the Claude of each PR in Ready, Staging or Human Review: record its session
    id, then /exit it (swarm_parking.park_one parks only an idle Claude at an empty prompt).
    MethBot 2026-11-15 4:13 AM ET: #531 and #532 sat in Staging with idle Claudes that held
    two of the cap slots, because park looked only at Ready. The tmux window, its shell, the
    branch and the worktree stay. A rework Claude keeps running only while the reason it was
    revived for is open (rework_open). Returns the parked PR numbers."""
    ready = {p['number']: col for col in DONE_COLS for p in cols.get(col, [])}
    claudes = attach_prs(running() if claudes is None else claudes, prs)
    st = load(path)
    gone = [k for k in st['rework'] if prs and (prs.get(int(k)) or {}).get('state', 'GONE') != 'OPEN']  # closed, merged
    if gone and not dry:
        with update(path) as st:
            for k in gone:
                st['rework'].pop(k, None)
    done = []
    for c in claudes:
        n = c.get('pr')
        if n not in ready:
            continue
        head = (prs.get(n) or {}).get('head') or ''
        rw = st['rework'].get(str(n))
        keep = rework_open(rw, prs.get(n) or {}, head)
        if keep:
            out(f"park: #{n} is in rework and {keep}: it keeps running")
            continue
        if not c.get('pane'):
            out(f"park: #{n} Claude pid {c['pid']} is not in the {TMUX_SESSION} tmux session: not parked")
            continue
        col = 'Staging' if ready[n] == board.STAGING else ready[n]
        if ready[n] != board.READY and c.get('status') not in (None, 'idle'):
            continue  # MethBot 4:13 AM ET: past Ready, park only an idle Claude
        if dry:
            out(f"park: would park #{n} (pane {c['pane']}, session {c['session'][:8]}): its PR is in {col}")
            continue
        with update(path) as st:  # record the session first: the revive needs it even if the /exit fails
            st['parked'][str(n)] = dict(session=c['session'], pane=c['pane'], worktree=c['cwd'],
                                        branch=c.get('branch') or '', head=head, parked_at=et_now())
        try:
            why = (park or _park)(c, out)
        except Exception as e:  # the cascade goes on
            why = f"{type(e).__name__}: {e}"
        if why:
            out(f"park: #{n} not parked: {why}")
            if 'process' in why:
                try:
                    relay_leftovers(n, c['cwd'], out=out, path=path)
                except Exception as e:
                    out(f"park: #{n}: leftover relay failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        with update(path) as st:
            st['parked'].setdefault(str(n), {})['window'] = c.get('window', '')
            st['rework'].pop(str(n), None)
        done.append(n)
        out(f"park: #{n} parked; session {c['session']} kept for swarm-revive {n}")
        _event('park', pr=n, actor=c.get('window'), notes=[f"its PR is in {col}; session {c['session']}"])
    return done


def tell_worker(n, text, sp=None, host=None, out=print):
    """Send one line to PR n's running Claude (not a revive: no rework slot). Returns True
    when it was sent."""
    sp = sp or parking()
    host = host or make_host(sp)
    window, wt, branch, entry = sp.resolve(host, str(n))
    existing = sp.live_pane(host, window)
    if not (existing and existing.get('agent') == 'claude'):
        out(f"#{n}: no running Claude in {window}: not told")
        return False
    host.tell(existing['pane'], one_line(text))
    _event('tell', pr=n, actor=window, notes=[one_line(text)[:600]])
    return True


def relay_leftovers(n, cwd, out=print, path=None, find=None, tell=None):
    """Park was refused because a process of the worker still runs: name the leftovers to
    the worker once per set of pids (MethBot 2026-11-15 4:31 AM ET: no human kill)."""
    from . import workerprocs
    left = (find or workerprocs.leftovers)(cwd)
    if not left:
        return None
    key = ','.join(str(p) for p, _ in left)
    if load(path)['told'].get(str(n)) == key:
        return None
    (tell or tell_worker)(n, workerprocs.stop_text(n, left))
    with update(path) as st:
        st['told'][str(n)] = key
    out(f"park: #{n}: asked its worker to stop {len(left)} leftover process(es): {key}")
    return key


# -- (b) revive a parked Claude with feedback -------------------------------------------------

def one_line(text):
    """The feedback as one line: each newline would submit a separate prompt."""
    t = ' '.join((text or '').split())
    if not t:
        raise ValueError('the feedback is empty')
    if len(t) > FEEDBACK_MAX:
        raise ValueError(f'the feedback is {len(t)} chars; max {FEEDBACK_MAX}')
    return t


def _event(event, **kw):
    from . import eventdb
    eventdb.log_event(event, source='slots', **kw)


def revive(n, text, dry=False, out=print, sp=None, host=None, claudes=None, path=None, refuse=None,
           priority=False, capacity=None):
    """(b) Resume PR n's Claude (claude --resume SESSION) in its window, in a rework slot,
    and send the feedback. 0 done, 1 refused or failed, 3 held (no rework slot or memory).
    Every non-dry call is one events DB row: 'resume' (a new Claude), 'feedback' (to a
    running one), or the hold or refusal (tangentswarm #32). priority (a conflict fix for fill,
    tangentswarm #65): no rework-slot wait; a new Claude starts only within conflict_capacity
    (capacity: its stand-in for tests)."""
    lines = []

    def said(line):
        lines.append(line)
        out(line)
    rc, error = None, None
    try:
        rc = _revive(n, text, dry, said, sp, host, claudes, path, refuse, priority, capacity)
        return rc
    except Exception as e:
        error = f'{type(e).__name__}: {str(e)[:300]}'
        raise
    finally:
        if not dry:
            ev = 'feedback' if any('already runs' in l for l in lines) else 'resume'
            _event(ev, action={0: 'done', 1: 'refused', 3: 'held'}.get(rc, 'failed'),
                   pr=int(str(n).lstrip('#')) if str(n).lstrip('#').isdigit() else None,
                   notes=lines[-3:] + [one_line_safe(text)[:600]], error=error)


def one_line_safe(text):
    try:
        return one_line(text)
    except ValueError:
        return str(text or '')[:200]


def _revive(n, text, dry=False, out=print, sp=None, host=None, claudes=None, path=None, refuse=None,
            priority=False, capacity=None):
    n = int(str(n).lstrip('#'))
    try:
        text = one_line(text)
    except ValueError as e:
        out(f"refused: {e}")
        return 1
    sp = sp or parking()
    try:
        host = host or make_host(sp)
        window, wt, branch, entry = sp.resolve(host, str(n))
        wt = str(wt)
        st = load(path)
        claudes = running() if claudes is None else claudes
        mine = [c for c in claudes if c['cwd'] == wt]
        rework_wts = {r.get('worktree') for k, r in st['rework'].items() if k != str(n)}
        busy = [c for c in claudes if c['cwd'] in rework_wts]
        if not mine and not priority and len(busy) >= REWORK_SLOTS:
            out(f"held: the {REWORK_SLOTS} rework slots are in use ("
                + ', '.join('#' + k for k, r in st['rework'].items() if r.get('worktree') in {c['cwd'] for c in busy})
                + "); try again when one is parked")
            return 3
        rec = st['parked'].get(str(n)) or {}
        sid = rec.get('session') if rec.get('worktree') in (None, wt) else None
        sid = sid or latest_session(wt)

        def mark(pane):
            if dry:
                return
            head = _git(wt, 'rev-parse', 'HEAD')
            with update(path) as d:
                d['rework'][str(n)] = dict(session=sid or '', window=window, pane=pane, worktree=wt, branch=branch,
                                           head=head, started=et_now(), feedback=text[:200],
                                           reason=revive_reason(text))
                d['parked'].pop(str(n), None)

        with sp.flocked(sp.resume_lock(window), blocking=False) as got:
            if not got:
                raise sp.Refused(f"{window}: another resume of this window is running")
            existing = sp.live_pane(host, window)
            if existing and existing['agent'] == 'claude':  # still running: only send the feedback
                if dry:
                    out(f"would send the feedback to the running Claude of #{n} in {existing['pane']}")
                    return 0
                host.tell(existing['pane'], text)
                mark(existing['pane'])
                out(f"#{n}: Claude already runs in {window} ({existing['pane']}); feedback sent; it holds a rework slot")
                return 0
            if existing and existing['agent']:
                raise sp.Refused(f"{window}: runs {existing['agent']}, not Claude; not launching into it")
            from . import workerprocs  # tangentswarm #40: never a second Claude in one worktree
            second = (refuse or workerprocs.refuse_second_claude)(wt)
            if second:
                raise sp.Refused(f"{window}: {second}")
            if not Path(wt).is_dir():
                raise sp.Refused(f"{window}: worktree {wt} is gone")
            cur = _git(wt, 'symbolic-ref', '--short', '-q', 'HEAD')
            if cur != branch:
                raise sp.Refused(f"{window}: {wt} is on {cur or 'a detached HEAD'}, expected {branch}")
            if existing:
                why = sp.idle_shell_problem(host, existing, wt)
                if why:
                    raise sp.Refused(f"{window}: window {existing['pane']} is not an idle shell in {wt} ({why})")
            argv, unit = sp.scoped_command(window, resume=False)
            argv += ['--resume', sid] if sid else ['--continue']
            line = sp.launch_line(argv)
            how = f"session {sid}" if sid else "no session id found: claude --continue"
            if dry:
                out(f"would resume #{n} in {window} ({how}) in "
                    f"{'its idle shell ' + existing['pane'] if existing else 'a new shell window'}: {line}")
                out(f"would then send: {text[:120]}")
                return 0
            if priority:  # tangentswarm #65: only the live Claudes and MemAvailable hold it
                ok, why = (capacity or conflict_capacity)()
                if not ok:
                    out(f"held: {why}; #{n} resumes on the next pass")
                    return 3
            busy = 0 if priority else claude_load()
            if busy >= MAX_CLAUDES:  # Codex on PR #26: a resume started a Claude over the global cap
                out(f"held: {busy} Claudes run or are reserved (cap {MAX_CLAUDES}); #{n} resumes when one ends")
                return 3
            room = None if priority else sp.headroom(host)
            if room is not None and room < sp.RESUME_PARK_NEED_MB * sp.MB:
                out(f"headroom {room // sp.MB} MB; asking swarm-park for {sp.RESUME_PARK_NEED_MB} MB")
                host.run_park(sp.RESUME_PARK_NEED_MB)
                room = sp.headroom(host)
            if room is not None and room < sp.RESUME_MIN_MB * sp.MB:
                out(f"held: {window} needs {sp.RESUME_MIN_MB} MB under {sp.SLICE} MemoryHigh, have {room // sp.MB} MB")
                return 3
            pane, reused = sp.shell_for(host, window, wt, existing)
            host.type_line(pane, line)
            out(f"#{n}: resuming in {'reused' if reused else 'new'} shell {pane} ({how}) as {unit}")
            ok, why = sp.wait_ready(host, pane, unit)
            if not ok:
                out(f"failed: #{n}: {why}")
                return 1
            mark(pane)
            if entry:
                sp.remove_window(window)
            host.tell(pane, text)
            out(f"#{n}: Claude resumed in {window} ({pane}) in a rework slot; feedback sent")
            return 0
    except sp.Refused as e:
        out(f"refused: {e}")
        return 1


# -- CLI ------------------------------------------------------------------------------

def report(out=print, path=None):
    st = load(path)
    claudes = running()
    rw = {r.get('worktree'): k for k, r in st['rework'].items()}
    for c in claudes:
        k = rw.get(c['cwd'])
        out(f"{'rework #' + k if k else 'new work':12} pid {c['pid']} {c['pane'] or '-':6} {c['status']:6} "
            f"{c['branch']} (session {c['session'][:8]})")
    nr = sum(1 for c in claudes if c['cwd'] in rw)
    out(f"running: {len(claudes) - nr} not rework (new work, or done and waiting to be parked; "
        f"keep {NEW_SLOTS} new), {nr}/{REWORK_SLOTS} rework")
    for k, r in sorted(st['parked'].items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else kv[0])):
        label = f"#{k}" if k.isdigit() else k
        out(f"parked {label} {r.get('window') or r.get('pane')} session {r.get('session')} since {r.get('parked_at')}")
    return 0


def board_now():
    """(open board PRs {n: pr}, columns) from Project 4, as the cascade reads them."""
    import runpy
    from . import tick
    from .promoted import BIN
    g = runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_slots')
    prs = {n: p for n, p in tick.board_prs_open({'gql': g['gql'], 'PARKED': g.get('PARKED', set())}).items()
           if not p['parked']}
    cols = {}
    for p in prs.values():
        cols.setdefault(p['col'], []).append(p)
    return prs, cols


def main(argv=None):
    """swarm-slots: the running Claudes by slot, and the parked sessions."""
    ap = argparse.ArgumentParser(prog='swarm-slots', description=main.__doc__)
    ap.add_argument('--park', action='store_true', help='park the Claude of each PR in Ready (the cascade does this)')
    ap.add_argument('--dry-run', action='store_true', help='with --park: print what it would park')
    a = ap.parse_args(argv)
    if a.park:
        prs, cols = board_now()
        park_ready(prs, cols, dry=a.dry_run)
    return report()


def revive_main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-revive', description=
        "Resume a PR's parked Claude (claude --resume SESSION) in a rework slot and send it feedback.")
    ap.add_argument('pr', help='PR number (480 or #480)')
    ap.add_argument('feedback', nargs='+', help="the feedback text; '-' reads it from stdin")
    ap.add_argument('--dry-run', action='store_true', help='check and print the launch line; change nothing')
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.feedback == ['-'] else ' '.join(a.feedback)
    return revive(a.pr, text, dry=a.dry_run)


if __name__ == '__main__':
    sys.exit(main())
