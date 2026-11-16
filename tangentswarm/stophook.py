"""swarm-stop-hook: the Claude Code Stop hook of each swarm worktree (tangentswarm #28).

When a swarm Claude finishes and has pushed, nothing advanced its pull request until the
15-minute swarm-tick ran. On 2026-11-14/15 that cost 387 minutes: platform #495 sat 192
minutes Codex-clean in In Progress, #513 123 minutes and #514 71 minutes not marked ready.
Now the Stop hook does the handoff at once; swarm-tick stays as the slow backstop.

    swarm-stop-hook                 the hook (stdin: the Stop event JSON). It returns at once:
                                    it only writes a request into the spool (tangentswarm.spool).
                                    The swarm-promoted daemon, outside swarm-workers.slice
                                    (where sudo and so the gh shim fail), runs the handoff.
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
  4. Codex findings open AND Codex already reviewed this exact head: nothing (mn-workflow
     relays them). Findings from an older head, with a new push Codex has not reviewed: the
     handoff goes on, so Codex reviews the fix (MethBot 2026-11-15: #518, #524, #525 sat
     61-75 min as idle drafts). A head that does
     not merge cleanly onto origin/main: nothing (the tick relays a merge-main).
  4b. A dev server or other background process still runs in the worktree: no handoff; the
     worker is asked to stop it (workerprocs), and its next Stop runs the handoff.
  5. Mark the PR ready (if it is a draft); move the card In Progress -> Auto Review.
  0. One run at a time per worktree (a lock). FIRST, whatever the head: the Claude's last
     message ends with a question, or says partial scope or that it waits for a decision:
     the card moves to Needs Input, the question goes on the issue, and nothing else runs
     (platform #541, 2026-11-15). It is never marked ready.
  6. Codex already clean on the head: `swarm-advance --minbot N` (the MinBot check: CI
     green, mergeable, not a draft; a pass moves the card to Ready; a busy lock queues it
     for swarm-promoted). Else post "@codex review" once per head (the key is shared with
     swarm-tick, which does not post it again).
Each run writes one line to LOG, for example:
  #514 head 7f3a1c2 pushed. Marked the pull request ready, posted "@codex review", and moved
  the card to Auto Review.
"""
import argparse
import contextvars
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import board

HOME = Path.home()
STATE_DIR = Path(os.environ.get('SWARM_FLOW_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'pull.json'))).parent
LOG = Path(os.environ.get('SWARM_STOP_HOOK_LOG', str(STATE_DIR / 'stop-hook.log')))
SEEN = Path(os.environ.get('SWARM_STOP_HOOK_SEEN', str(STATE_DIR / 'stop-hook-seen.json')))
LOCKS = Path(os.environ.get('SWARM_STOP_HOOK_LOCKS', str(Path.home() / '.cache' / 'swarm-stop-hook-locks')))
REPO = 'tangentcode/platform'
HEAD_TRIES, HEAD_WAIT = 4, 5  # GitHub may show the new head a few seconds after the push


def bin_dir():
    from .promoted import BIN
    return BIN


_LINES = contextvars.ContextVar('stophook_lines', default=None)


def say(line, out=None):
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        now = datetime.now()
    if _LINES.get() is not None:
        _LINES.get().append(line)  # the events DB row of this hook run (tangentswarm #32)
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


def find_pr(g, branch, max_age=None, issues=False):
    """(number, item id, column, draft[, closed issue numbers]) of the open platform PR whose
    head branch is branch."""
    from . import boardcache
    for age in ((max_age,) if max_age is not None else (None, 0)):  # a stale snapshot: read once more
        snap = boardcache.snapshot(g['gql'], max_age=age)
        for it, c in boardcache.open_prs(snap):
            if c.get('headRefName') == branch:
                row = (c['number'], it['id'], boardcache.col(it), bool(c.get('isDraft')))
                return row + ([i['number'] for i in boardcache.closes(c)],) if issues else row
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


WAITING_RE = __import__('re').compile(
    r'partial scope|waiting (?:for|on) (?:a |your |michal.s )?decision|needs? (?:a |your |michal.s )?decision|'
    r'question for michal|before i (?:go on|continue)', __import__('re').I)
QUESTION_MARK = '<!-- swarm-needs-input -->'


def last_question(transcript):
    """The last assistant text of a Claude transcript (JSONL) when it ends in a question
    (its last non-empty line ends with "?"), else None."""
    try:
        lines = Path(transcript).read_text(errors='replace').splitlines()[-400:]
    except (OSError, TypeError):
        return None
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get('message') or {}
        if d.get('type') != 'assistant' or msg.get('role', 'assistant') != 'assistant':
            continue
        content = msg.get('content')
        texts = [c.get('text') or '' for c in content if isinstance(c, dict) and c.get('type') == 'text'] \
            if isinstance(content, list) else [content or '']
        text = '\n'.join(t for t in texts if t).strip()
        if not text:
            continue  # a tool call: look further back
        last = [l for l in text.splitlines() if l.strip()][-1].strip().rstrip('*_` ')
        return text if last.endswith('?') or WAITING_RE.search(text[-600:]) else None
    return None


def move_card(g, item, col, sh, n=None):
    board.move_card(g, item, col, sh, n=n)


def needs_input(cwd, branch, transcript, g=None, dry=False, out=None, sh=None):
    """A Claude that stops with a question for Michal (a final "?", or partial scope, or
    waiting for a decision) goes to Needs Input, ALWAYS before any handoff, even with a clean,
    pushed head (platform #541, 2026-11-15: the hook marked it ready, asked Codex, the MinBot
    pass moved it to Ready, and only a racing later run moved it to Needs Input). The card
    moves from In Progress or Auto Review to Needs Input; the question is posted on the
    issue (with QUESTION_MARK, which the MinBot pass reads); once per question. Never ready."""
    import hashlib
    q = last_question(transcript)
    if not q:
        return None
    key = f"q:{branch}:{hashlib.sha1(q.encode()).hexdigest()[:12]}"
    if seen(key):
        return say(f"{branch}: the question was already handed to Michal: no handoff", out)
    g = g if g is not None else hr_gate()
    found = find_pr(g, branch, issues=True)
    if not found:
        return None
    n, item, col, _, issues = found
    if col not in (board.IN_PROGRESS, board.AUTO_REVIEW):
        return say(f"#{n}: the Claude asks a question, but the card is in {col}: not moved", out)
    body = (f"{QUESTION_MARK}\nQuestion from the worker on #{n} (the card is in Needs Input):\n\n"
            + '\n'.join('> ' + l for l in q.strip()[-1500:].splitlines()))
    if not dry:
        move_card(g, item, board.NEEDS_INPUT, sh, n=n)
        for i in (issues or [n])[:1]:
            sh(['gh', 'issue', 'comment', str(i), '-R', REPO, '--body', body])
        _mark_seen(key)
    short = ' '.join(q.split())[-160:]
    where = f"issue #{issues[0]}" if issues else f"#{n}"
    return say(f"#{n} the Claude stopped with a question: ...{short} Moved the card to Needs Input and posted "
               f"the question on {where}; not marked ready." + (' (dry run)' if dry else ''), out)


def tell_worker(n, text):
    from . import slots
    return slots.tell_worker(n, text)


def run(cwd, g=None, dry=False, out=None, sh=None, sleep=time.sleep, merges=merges_onto_main, minbot=minbot_check,
        transcript=None, procs=None, tell=None):
    """The handoff for one worktree. Returns the logged line, or None when nothing was due."""
    from . import slots, tick, workerprocs
    sh = sh or (lambda argv: tick.sh(argv, check=True))
    procs = procs or workerprocs.leftovers
    tell = tell or tell_worker
    cwd = Path(cwd)
    if not slots.is_platform_worktree(cwd):
        return None
    from . import eventdb
    lines, error, token = [], None, _LINES.set([])
    lines = _LINES.get()
    try:
        with branch_lock(cwd):  # platform #541: two hook runs for one PR must never race
            return _run(cwd, g, dry, out, sh, sleep, merges, minbot, transcript, procs, tell)
    except Exception as e:
        error = f'{type(e).__name__}: {str(e)[:300]}'
        raise
    finally:
        _LINES.reset(token)
        if not dry and (lines or error):  # one row per hook run with its decision
            m = re.search(r'#(\d+)', ' '.join(lines))
            eventdb.log_event('stop', source='stop-hook', actor=cwd.name, pr=int(m.group(1)) if m else None,
                              notes=lines, error=error)


def branch_lock(cwd):
    """An exclusive lock per worktree (one PR): a second hook run waits for the first."""
    import contextlib
    import fcntl

    @contextlib.contextmanager
    def held():
        LOCKS.mkdir(parents=True, exist_ok=True)
        with open(LOCKS / f"{Path(cwd).name}.lock", 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    return held()


def _run(cwd, g, dry, out, sh, sleep, merges, minbot, transcript, procs, tell):
    from . import tick, workerprocs
    branch, head = pushed_head(cwd)
    if branch and transcript:  # a question for Michal goes first, whatever the head
        asked = needs_input(cwd, branch, transcript, g=g, dry=dry, out=out, sh=sh)
        if asked:
            return asked
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
    from . import agent_report
    if agent_report.pending_input(n):  # tangentswarm #53: the report goes first; not marked seen
        return say(f"#{n} head {head[:7]} pushed, but its worker reported needs-input or blocked and the "
                   "report is not handled yet: no handoff", out)
    if col not in (board.IN_PROGRESS, board.AUTO_REVIEW):
        mark_seen(key)
        return say(f"#{n} head {head[:7]} pushed; the card is in {col}: nothing to do", out)
    left = procs(cwd)
    if left:  # MethBot 2026-11-15: stop dev servers before the handoff; no human kill
        if not dry:
            tell(n, workerprocs.stop_text(n, left))
        return say(f"#{n} head {head[:7]} pushed, but {len(left)} process(es) still run in the worktree "
                   f"({', '.join(str(p) for p, _ in left[:5])}): asked the worker to stop them; no handoff yet", out)
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
    pushed_after = status == 'findings' and not tick.codex_reviewed_head(full)
    if status == 'findings' and not pushed_after:
        mark_seen(key)
        return say(f"#{n} head {head[:7]} pushed; Codex findings are open on this head ({detail}): "
                   "left to mn-workflow", out)
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
            move_card(g, item, board.AUTO_REVIEW, sh, n=n)
        last = minbot(n) if not dry else f'would run swarm-advance --minbot {n}'
        what = (', '.join(did) + ', and ' if did else '') + 'ran the MinBot check'
        line = f"#{n} head {head[:7]} is already Codex-clean. {what[0].upper() + what[1:]}: {last}"
    else:
        state = tick.State(tick.STATE_DIR / 'state.json')
        ck = f"codex-request:{n}@{head}"
        if not (state.done(ck) or tick.codex_requested(full)):
            if not dry:
                from . import codexrounds
                codexrounds.request(n, head, 'stop-hook', sh=sh, state=state)
            did.append('posted "@codex review"')
        if col == board.IN_PROGRESS:
            if not dry:
                move_card(g, item, board.AUTO_REVIEW, sh, n=n)
            did.append('moved the card to Auto Review')
        if not did:
            mark_seen(key)
            return say(f"#{n} head {head[:7]} pushed; already ready, in Auto Review and asked for a Codex review", out)
        what = ', '.join(did[:-1]) + (', and ' if len(did) > 1 else '') + did[-1]
        line = f"#{n} head {head[:7]} pushed. {what[0].upper() + what[1:]}." + (
            f" (Codex threads from an older head are open: {detail}; Codex re-reviews this head)"
            if pushed_after else '')
    mark_seen(key)
    return say(line + (' (dry run)' if dry else ''), out)


# -- install ---------------------------------------------------------------------------

def hook_command():
    return str(bin_dir() / 'swarm-stop-hook')


def linked_dirs(worktree):
    """The real paths of the top-level symlinks of a worktree that point outside it (such as
    node_modules -> the main checkout's node_modules, from .swarm.yaml)."""
    wt = Path(worktree).resolve()
    out = []
    try:
        entries = sorted(Path(worktree).iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_symlink():
            continue
        real = Path(os.path.realpath(p))
        if real.is_dir() and wt not in real.parents and real != wt:
            out.append(str(real))
    return out


def install(worktree, command=None):
    """Write the worktree's .claude/settings.local.json (the file git ignores; other keys
    are kept): the Stop hook, and permissions.additionalDirectories for each linked directory
    outside the worktree (MethBot 2026-11-15 3:05 AM ET: the workers of ptw-521 and ptw-524
    stopped at a permission prompt for node_modules, a symlink to the main checkout).
    Returns 'added', 'present' or why not."""
    command = command or hook_command()
    path = Path(worktree) / '.claude' / 'settings.local.json'
    try:
        d = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as e:
        return f'not changed: {path} is not JSON ({e})'
    if not isinstance(d, dict):
        return f'not changed: {path} is not a JSON object'
    changed = []
    stops = d.setdefault('hooks', {}).setdefault('Stop', [])
    if not any(h.get('command') == command for grp in stops for h in grp.get('hooks') or ()):
        stops.append({'hooks': [{'type': 'command', 'command': command}]})
        changed.append('stop hook')
    dirs = (d.get('permissions') or {}).get('additionalDirectories') or []
    new = [x for x in linked_dirs(worktree) if x not in dirs]
    if new:
        d.setdefault('permissions', {})['additionalDirectories'] = dirs + new
        changed.append('allowed ' + ', '.join(new))
    if not changed:
        return 'present'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(d, indent=2) + '\n')
    os.replace(tmp, path)
    return 'added ' + '; '.join(changed)


def worktrees():
    from . import slots
    return sorted(p for p in slots.WORKTREE_PARENT.glob('platform.*') if p.is_dir())


# -- the hook ----------------------------------------------------------------------------

def hook(stdin=sys.stdin, submit=None):
    """The Stop hook: queue the handoff in the spool and return at once (exit 0, no output: a
    Stop hook must never block the Claude). It runs inside swarm-workers.slice, where sudo
    (and so every gh read through the minbot shim) fails: the swarm-promoted daemon runs
    the handoff outside the slice (tangentswarm.spool)."""
    from . import slots, spool
    try:
        event = json.loads(stdin.read() or '{}')
    except ValueError:
        event = {}
    cwd = event.get('cwd') or os.getcwd()
    if not slots.is_platform_worktree(Path(cwd)):
        return 0
    try:
        (submit or spool.submit)('stophook', cwd, transcript=event.get('transcript_path'))
    except (OSError, ValueError) as e:
        say(f"{Path(cwd).name}: stop hook not queued: {e}")
    return 0


def main(argv=None):
    from . import eventdb
    eventdb.set_source('stop-hook')
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return hook()
    ap = argparse.ArgumentParser(prog='swarm-stop-hook', description=__doc__.split('\n', 1)[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('cwd')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--transcript', help='the Claude transcript (JSONL): a final question moves the card to Needs Input')
    sv = sub.add_parser('serve', help='(the daemon) run one claimed spool request')
    sv.add_argument('file')
    i = sub.add_parser('install')
    i.add_argument('worktrees', nargs='*')
    i.add_argument('--all', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'serve':
        from . import spool
        return spool.serve(a.file)
    if a.cmd == 'run':
        try:
            line = run(a.cwd, dry=a.dry_run, out=print, transcript=a.transcript)
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
