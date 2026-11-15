"""swarm-stop-hook: the Claude Code Stop hook of each swarm worktree (tangentswarm #28).

When a swarm Claude finishes and has pushed, nothing advanced its pull request until the
15-minute swarm-tick ran. On 2026-11-14/15 that cost 387 minutes: platform #495 sat 192
minutes Codex-clean in In Progress, #513 123 minutes and #514 71 minutes not marked ready.
Now the Stop hook does the handoff at once; swarm-tick stays as the slow backstop.

    swarm-stop-hook                 the hook (stdin: the Stop event JSON). It returns at once
                                    and runs `swarm-stop-hook run CWD` in the background, so
                                    the Claude never waits on GitHub.
    swarm-stop-hook run CWD [--dry-run]
                                    the handoff for the worktree CWD (also by hand)
    swarm-stop-hook install WORKTREE... | --all
                                    add the hook to WORKTREE/.claude/settings.local.json (the
                                    file git ignores; other keys are kept)

The handoff (run), only for a worker worktree ~/ver/platform.<slug>:
  1. The worktree is clean (tracked files) and its HEAD is the pushed branch head
     (git ls-remote). Else: nothing (the Claude has not finished).
  2. Once per branch head: the same head on a later Stop does nothing.
  3. The open PR of the branch, from the board snapshot (column by option id). Only a card
     in In Progress or Auto Review is handled.
  4. Codex findings open on the head: nothing (mn-workflow relays them). A head that does
     not merge cleanly onto origin/main: nothing (the tick relays a merge-main).
  5. Mark the PR ready (if it is a draft); move the card In Progress -> Auto Review.
  6. Codex already clean on the head: `swarm-advance --minbot N` (the MinBot check: CI
     green, mergeable, not a draft; a pass moves the card to Ready; a busy lock queues it
     for swarm-promoted). Else post "@codex review" once per head (the key is shared with
     swarm-tick, which does not post it again).
Each run writes one line to LOG, for example:
  #514 head 7f3a1c2 pushed. Marked the pull request ready, posted "@codex review", and moved
  the card to Auto Review.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import board

HOME = Path.home()
STATE_DIR = Path(os.environ.get('SWARM_FLOW_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'pull.json'))).parent
LOG = Path(os.environ.get('SWARM_STOP_HOOK_LOG', str(STATE_DIR / 'stop-hook.log')))
SEEN = Path(os.environ.get('SWARM_STOP_HOOK_SEEN', str(STATE_DIR / 'stop-hook-seen.json')))
REPO = 'tangentcode/platform'
HEAD_TRIES, HEAD_WAIT = 4, 5  # GitHub may show the new head a few seconds after the push


def bin_dir():
    from .promoted import BIN
    return BIN


def say(line, out=None):
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        now = datetime.now()
    line = f"{now.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    if out:
        out(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass
    return line


def git(cwd, *args, timeout=120):
    r = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or '').strip()


def seen(key, path=None):
    try:
        return key in json.loads((path or SEEN).read_text())
    except (OSError, ValueError):
        return False


def _mark_seen(key, path=None, keep=400):
    path = path or SEEN
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        d = {}
    d[key] = time.time()
    while len(d) > keep:
        del d[next(iter(d))]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(d))
    os.replace(tmp, path)


def pushed_head(cwd):
    """(branch, head) when the worktree is clean (tracked files) and HEAD is the pushed
    branch head; else (branch or None, why it is not finished)."""
    rc, branch = git(cwd, 'symbolic-ref', '--short', '-q', 'HEAD')
    if rc or not branch:
        return None, 'detached HEAD'
    if branch in ('main', 'work'):
        return None, f'on {branch}'
    rc, dirty = git(cwd, 'status', '--porcelain', '--untracked-files=no')
    if rc or dirty:
        return branch, 'uncommitted changes'
    _, head = git(cwd, 'rev-parse', 'HEAD')
    rc, remote = git(cwd, 'ls-remote', 'origin', f'refs/heads/{branch}', timeout=60)
    remote = (remote.split() or [''])[0]
    if rc or remote != head:
        return branch, f"HEAD {head[:8]} is not pushed (origin has {remote[:8] or 'no branch'})"
    return branch, head


def find_pr(g, branch, max_age=None):
    """(number, item id, column, draft) of the open platform PR whose head branch is branch."""
    from . import boardcache
    for age in ((max_age,) if max_age is not None else (None, 0)):  # a stale snapshot: read once more
        snap = boardcache.snapshot(g['gql'], max_age=age)
        for it, c in boardcache.open_prs(snap):
            if c.get('headRefName') == branch:
                return c['number'], it['id'], boardcache.col(it), bool(c.get('isDraft'))
    return None


def merges_onto_main(cwd, head):
    git(cwd, 'fetch', '-q', 'origin', 'main', timeout=120)
    rc, _ = git(cwd, 'merge-tree', '--write-tree', '--no-messages', 'origin/main', head)
    return rc == 0


def hr_gate():
    from .advance import hr_gate as load
    return load()


def minbot_check(n):
    """swarm-advance --minbot N; returns its last output line."""
    r = subprocess.run([str(bin_dir() / 'swarm-advance'), '--minbot', str(n)],
                       capture_output=True, text=True, timeout=900)
    return ([l for l in (r.stdout + r.stderr).splitlines() if l.strip()] or [f'rc={r.returncode}'])[-1]


def run(cwd, g=None, dry=False, out=None, sh=None, sleep=time.sleep, merges=merges_onto_main, minbot=minbot_check):
    """The handoff for one worktree. Returns the logged line, or None when nothing was due."""
    from . import slots, tick
    sh = sh or (lambda argv: tick.sh(argv, check=True))
    cwd = Path(cwd)
    if not slots.is_platform_worktree(cwd):
        return None
    branch, head = pushed_head(cwd)
    if branch is None or len(head) != 40:
        return say(f"{cwd.name}: not finished: {head}", out) if branch else None
    key = f"{branch}@{head}"
    if seen(key):
        return None
    mark_seen = (lambda k: None) if dry else _mark_seen  # a dry run records nothing
    g = g if g is not None else hr_gate()
    found = find_pr(g, branch)
    if not found:
        return say(f"{branch} head {head[:7]} pushed, but no open PR on the board has this branch", out)
    n, item, col, _ = found
    if col not in (board.IN_PROGRESS, board.AUTO_REVIEW):
        mark_seen(key)
        return say(f"#{n} head {head[:7]} pushed; the card is in {col}: nothing to do", out)
    full = None
    for i in range(HEAD_TRIES):
        full = g['fetch'](n, refresh=True)
        if full.get('headRefOid') == head:
            break
        sleep(HEAD_WAIT)
    else:
        return say(f"#{n} head {head[:7]} pushed, but GitHub still shows {str(full.get('headRefOid'))[:7]}: "
                   "left to swarm-tick", out)
    status, detail = tick.codex_status(full)
    if status == 'findings':
        mark_seen(key)
        return say(f"#{n} head {head[:7]} pushed; Codex findings are open ({detail}): left to mn-workflow", out)
    if not merges(cwd, head):
        mark_seen(key)
        return say(f"#{n} head {head[:7]} pushed, but it does not merge cleanly onto origin/main: "
                   "not marked ready (swarm-tick relays a merge-main)", out)
    did = []
    if full.get('isDraft'):
        if not dry:
            sh(['gh', 'pr', 'ready', str(n), '-R', REPO])
        did.append('marked the pull request ready')
    if status == 'clean':
        if col == board.IN_PROGRESS and not dry:
            g['move'](item, 'Auto Review')
        last = minbot(n) if not dry else f'would run swarm-advance --minbot {n}'
        what = (', '.join(did) + ', and ' if did else '') + 'ran the MinBot check'
        line = f"#{n} head {head[:7]} is already Codex-clean. {what[0].upper() + what[1:]}: {last}"
    else:
        state = tick.State(tick.STATE_DIR / 'state.json')
        ck = f"codex-request:{n}@{head}"
        if not (state.done(ck) or tick.codex_requested(full)):
            if not dry:
                sh(['gh', 'pr', 'comment', str(n), '-R', REPO, '--body', '@codex review'])
                state.mark(ck)
            did.append('posted "@codex review"')
        if col == board.IN_PROGRESS:
            if not dry:
                g['move'](item, 'Auto Review')
            did.append('moved the card to Auto Review')
        if not did:
            mark_seen(key)
            return say(f"#{n} head {head[:7]} pushed; already ready, in Auto Review and asked for a Codex review", out)
        what = ', '.join(did[:-1]) + (', and ' if len(did) > 1 else '') + did[-1]
        line = f"#{n} head {head[:7]} pushed. {what[0].upper() + what[1:]}."
    mark_seen(key)
    return say(line + (' (dry run)' if dry else ''), out)


# -- install ---------------------------------------------------------------------------

def hook_command():
    return str(bin_dir() / 'swarm-stop-hook')


def install(worktree, command=None):
    """Add the Stop hook to WORKTREE/.claude/settings.local.json. Returns 'added', 'present'
    or why not."""
    command = command or hook_command()
    path = Path(worktree) / '.claude' / 'settings.local.json'
    try:
        d = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as e:
        return f'not changed: {path} is not JSON ({e})'
    if not isinstance(d, dict):
        return f'not changed: {path} is not a JSON object'
    stops = d.setdefault('hooks', {}).setdefault('Stop', [])
    if any(h.get('command') == command for grp in stops for h in grp.get('hooks') or ()):
        return 'present'
    stops.append({'hooks': [{'type': 'command', 'command': command}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(d, indent=2) + '\n')
    os.replace(tmp, path)
    return 'added'


def worktrees():
    from . import slots
    return sorted(p for p in slots.WORKTREE_PARENT.glob('platform.*') if p.is_dir())


# -- the hook ----------------------------------------------------------------------------

def hook(stdin=sys.stdin, spawn=subprocess.Popen):
    """The Stop hook: start the handoff in the background and return at once (exit 0, no
    output: a Stop hook must never block the Claude)."""
    from . import slots
    try:
        event = json.loads(stdin.read() or '{}')
    except ValueError:
        event = {}
    cwd = event.get('cwd') or os.getcwd()
    if not slots.is_platform_worktree(Path(cwd)):
        return 0
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as log:
            spawn([hook_command(), 'run', cwd], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                  start_new_session=True)
    except OSError:
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return hook()
    ap = argparse.ArgumentParser(prog='swarm-stop-hook', description=__doc__.split('\n', 1)[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('cwd')
    r.add_argument('--dry-run', action='store_true')
    i = sub.add_parser('install')
    i.add_argument('worktrees', nargs='*')
    i.add_argument('--all', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'run':
        try:
            line = run(a.cwd, dry=a.dry_run, out=print)
        except Exception as e:  # never a traceback into a Claude session
            say(f"{Path(a.cwd).name}: stop hook failed: {type(e).__name__}: {str(e)[:200]}", print)
            return 2
        if line is None:
            print('nothing to do')
        return 0
    targets = worktrees() if a.all else [Path(w) for w in a.worktrees]
    for w in targets:
        print(f"{w.name}: {install(w)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
