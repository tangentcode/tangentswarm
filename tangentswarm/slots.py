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
               NEVER_START list (#377 is held): the active fast-track first, then a
               milestone (the priority milestone first), then the lowest number.

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
import json
import os
import re
import subprocess
import sys
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
            'relayed': dict(d.get('relayed') or {}), 'named': dict(d.get('named') or {})}


def save(d, path=None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, path)


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

def backlog_issues(snap, active_ft=None, prs=None, named=None, now=None):
    """The launch picker (Michal 2026-11-15 12:16 AM ET: START_CLAUDE named Parked #344 while
    Backlog held 16 approved issues). Open platform issues in the BACKLOG column (by option id;
    so never Triage, Inbox, Discuss, Active or Parked) that no open PR closes, without the label
    "parked" or "triage", not in NEVER_START, and not named by START_CLAUDE in the last
    LAUNCH_GRACE s (named: {number: time}; its Claude has no PR yet). Order: the active
    fast-track, then a priority milestone, then any other milestone, then none; then the
    lowest number. snap: the board snapshot (boardcache); prs: board PRs ({n: {closes}})."""
    from . import boardcache
    from .tick import milestone_rank
    now = time.time() if now is None else now
    taken = {i['number'] for _, c in boardcache.open_prs(snap) for i in boardcache.closes(c)}
    taken |= {i for p in (prs or {}).values() if p.get('state', 'OPEN') == 'OPEN' for i in p.get('closes') or ()}
    recent = {int(k) for k, t in (named or {}).items() if now - float(t) < LAUNCH_GRACE}
    out = []
    for it, c in boardcache.open_issues(snap):
        n = c['number']
        if boardcache.col(it) != board.BACKLOG or n in taken or n in NEVER_START or n in recent:
            continue
        if {'parked', 'triage'} & {l['name'].lower() for l in (c.get('labels') or {}).get('nodes') or []}:
            continue
        ms = (c.get('milestone') or {}).get('title')
        ft = board.is_active_fast_track(n, (), active_ft)
        rank = (0 if ft else 1, milestone_rank([ms]), 0 if ms else 1)
        out.append(dict(number=n, title=c.get('title') or '', rank=rank, milestone=ms))
    return sorted(out, key=lambda i: (i['rank'], i['number']))


def mark_named(numbers, path=None, now=None):
    """Keep the issues START_CLAUDE named, so the next pass does not name them again while
    their Claude has no PR yet. Entries older than LAUNCH_GRACE are dropped."""
    now = time.time() if now is None else now
    d = load(path)
    d['named'] = {k: t for k, t in d['named'].items() if now - float(t) < LAUNCH_GRACE}
    d['named'].update({str(n): now for n in numbers})
    save(d, path)


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


def park_ready(prs, cols, dry=False, out=print, claudes=None, park=None, path=None):
    """(a) Park the Claude of each PR in Ready: record its session id, then /exit it. The
    tmux window, its shell, the branch and the worktree stay. A rework Claude is kept until
    it pushes a new head. Returns the parked PR numbers."""
    ready = {p['number'] for p in cols.get(board.READY, [])}
    claudes = attach_prs(running() if claudes is None else claudes, prs)
    st = load(path)
    gone = [k for k in st['rework'] if prs and (prs.get(int(k)) or {}).get('state', 'GONE') != 'OPEN']  # closed, merged
    if gone and not dry:
        for k in gone:
            st['rework'].pop(k)
        save(st, path)
    done = []
    for c in claudes:
        n = c.get('pr')
        if n not in ready:
            continue
        head = (prs.get(n) or {}).get('head') or ''
        rw = st['rework'].get(str(n))
        if rw and rw.get('head') and rw['head'] == head:
            out(f"park: #{n} is in rework and has pushed no new head since {head[:8]}: it keeps running")
            continue
        if not c.get('pane'):
            out(f"park: #{n} Claude pid {c['pid']} is not in the {TMUX_SESSION} tmux session: not parked")
            continue
        if dry:
            out(f"park: would park #{n} (pane {c['pane']}, session {c['session'][:8]}): its PR is in Ready")
            continue
        st = load(path)  # record the session first: the revive needs it even if the /exit fails
        st['parked'][str(n)] = dict(session=c['session'], pane=c['pane'], worktree=c['cwd'],
                                    branch=c.get('branch') or '', head=head, parked_at=et_now())
        save(st, path)
        try:
            why = (park or _park)(c, out)
        except Exception as e:  # the cascade goes on
            why = f"{type(e).__name__}: {e}"
        if why:
            out(f"park: #{n} not parked: {why}")
            continue
        st = load(path)
        st['parked'][str(n)]['window'] = c.get('window', '')
        st['rework'].pop(str(n), None)
        save(st, path)
        done.append(n)
        out(f"park: #{n} parked; session {c['session']} kept for swarm-revive {n}")
    return done


# -- (b) revive a parked Claude with feedback -------------------------------------------------

def one_line(text):
    """The feedback as one line: each newline would submit a separate prompt."""
    t = ' '.join((text or '').split())
    if not t:
        raise ValueError('the feedback is empty')
    if len(t) > FEEDBACK_MAX:
        raise ValueError(f'the feedback is {len(t)} chars; max {FEEDBACK_MAX}')
    return t


def revive(n, text, dry=False, out=print, sp=None, host=None, claudes=None, path=None):
    """(b) Resume PR n's Claude (claude --resume SESSION) in its window, in a rework slot,
    and send the feedback. 0 done, 1 refused or failed, 3 held (no rework slot or memory)."""
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
        if not mine and len(busy) >= REWORK_SLOTS:
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
            d = load(path)
            d['rework'][str(n)] = dict(session=sid or '', window=window, pane=pane, worktree=wt, branch=branch,
                                       head=_git(wt, 'rev-parse', 'HEAD'), started=et_now(), feedback=text[:200])
            d['parked'].pop(str(n), None)
            save(d, path)

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
            room = sp.headroom(host)
            if room < sp.RESUME_PARK_NEED_MB * sp.MB:
                out(f"headroom {room // sp.MB} MB; asking swarm-park for {sp.RESUME_PARK_NEED_MB} MB")
                host.run_park(sp.RESUME_PARK_NEED_MB)
                room = sp.headroom(host)
            if room < sp.RESUME_MIN_MB * sp.MB:
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
    for k, r in sorted(st['parked'].items(), key=lambda kv: int(kv[0])):
        out(f"parked #{k} {r.get('window') or r.get('pane')} session {r.get('session')} since {r.get('parked_at')}")
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
