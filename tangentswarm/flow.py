"""swarm-flow: the lean just-in-time Human Review flow (methodology#56), one step at a time.

Columns (matched by option id, see tangentswarm.board):
  Ready         finished PRs (Claude done, Codex clean on head, exact-head MinBot pass).
                They get no merge, no gate and no GitHub check. They wait in the shared pick
                order (board.pick_key, tangentswarm #65): fast-track by the lowest number,
                then the soonest milestone due date, then the lowest number.
  Staging       3 to 5 fully prepared PRs (internal key "Pending"): merged into work,
                served on staging, CI green, body complete, every gate condition passes.
  Human Review  exactly 2.

One step, under the one global serial lock (a second caller exits 75 "busy"):
  1. Promote: if Human Review has fewer than 2, run `pending-promote --apply`
     (Staging head -> Human Review through hr-gate). Never --fill.
  2. Pull: if Staging has fewer than STAGING_MIN and no pull is running, pick the top
     Ready PR that stacks cleanly on main + the Human Review heads + the Staging heads
     (local merge-tree only). A PR that does not merge onto main goes back to In
     Progress with a merge-main relay. Then merge its head into work (one merge commit,
     pushed as a fast-forward; the push redeploys staging).
  3. Advance a running pull: evaluate hr-gate on the pulled head. All conditions pass:
     `hr-gate --apply N` puts it into Staging. tangentswarm #83 (Michal 2026-11-10): only
     a real conflict at the final mergeability check (row 2: the head conflicts with
     origin/main) sends it to In Progress, with a merge-main relay. Any other way out of
     the pull (a hard failure, a timeout, a new head) drops the pull and the card stays in
     Ready at its place in the pick order. Waiting reasons (CI running, screenshots or the
     where-to-see section still missing) keep the pull until PULL_TIMEOUT, then fail.

Only the pulled PR is merged, deployed or gated. No -vN copies, no re-merge of the queue.

    swarm-flow step [--dry-run]     one step; prints did/would lines and notes
    swarm-flow event NAME < JSON    run a step only for a Status change or a merge
    swarm-flow status               the columns, the Ready order and the running pull
    swarm-flow merge-work N SHA [--label B] [--dry-run]
                                    merge one tip (such as a -vN) into work and push it, under
                                    the lock; refused unless the merge is clean (no conflict,
                                    no conflict marker). Use this, never a hand merge-tree.
Exit 0 = step done, 75 = busy (another merge/gate step runs), 2 = error.
"""
import argparse
import contextlib
import json
import os
import re
import runpy
import subprocess
import sys
import time
from pathlib import Path

from . import board, gatelock, gqlmeter, proc

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
STATE = Path(os.environ.get('SWARM_FLOW_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'pull.json')))
LOG = STATE.with_name('flow.log')
REPO = 'tangentcode/platform'
# tangentswarm #65: fixed WIP targets (configurable; the number is not settled). Never past them.
HR_TARGET = int(os.environ.get('SWARM_HR_TARGET', '2'))
STAGING_TARGET = int(os.environ.get('SWARM_STAGING_TARGET', '2'))
STAGING_MIN = int(os.environ.get('SWARM_STAGING_MIN', str(STAGING_TARGET)))
STAGING_MAX = int(os.environ.get('SWARM_STAGING_MAX', '5'))
PULL_TIMEOUT = int(os.environ.get('SWARM_PULL_TIMEOUT', str(90 * 60)))
WORK = 'work'
# tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): pre-merge the top Ready PRs onto future main.
# tangentswarm #111 (Michal 2026-11-17 10:45 AM ET): only the top one. Nothing else in Ready is
# checked; a PR is pre-merged when it becomes the top.
PREMERGE_AHEAD = int(os.environ.get('SWARM_PREMERGE_AHEAD', '1'))
PRE_MERGING, MERGE_READY = 'pre-merging', 'merge-ready'
# A wait reason that is a job for the PR's worker, not a timeout (screenshots; status text in
# the body, tangentswarm #130).
STATUS_ROW = 'body: '
WORKER_JOB_ROWS = ('7a', STATUS_ROW)
# hr-gate rows that mean "wait", not "fail", while a pull is running.
WAIT_ROWS = ('5 ', '6 ', '7a', '7b')
CI_WAIT_WORDS = ('IN_PROGRESS', 'QUEUED', 'PENDING', 'WAITING', 'REQUESTED', 'no Linux GitHub checks')


# ---------------------------------------------------------------------------
# state of the one running pull

def load_pulls():
    """The pulls in flight: [{pr, head, branch, item, work, started}], oldest first.
    A batch (Cascade fill) shares one work commit. The old one-pull file is read too."""
    try:
        d = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return []
    if isinstance(d, dict) and isinstance(d.get('pulls'), list):
        return [p for p in d['pulls'] if isinstance(p, dict) and p.get('pr')]
    return [d] if isinstance(d, dict) and d.get('pr') else []


def save_pulls(pulls):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not pulls:
        STATE.unlink(missing_ok=True)
        return
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'pulls': list(pulls)}, indent=1))
    os.replace(tmp, STATE)


def load_pull():
    """The oldest pull in flight, or None."""
    pulls = load_pulls()
    return pulls[0] if pulls else None


def save_pull(d):
    """Put pull d (replaces the pull of the same PR). None clears every pull."""
    if d is None:
        save_pulls([])
        return
    save_pulls([p for p in load_pulls() if p['pr'] != d['pr']] + [d])


def drop_pull(n):
    """Pull n ended (moved, failed, dropped)."""
    pulls = load_pulls()
    if any(p['pr'] == n for p in pulls):
        save_pulls([p for p in pulls if p['pr'] != n])


HELD = STATE.with_name('held.json')  # retired (tangentswarm #101): clear_held deletes it
PREMERGE_STATE = STATE.with_name('premerge.json')  # {PR: {head, base, vbranch, tip|seed, status, relayed}}


def load_premerge():
    try:
        d = json.loads(PREMERGE_STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_premerge(d):
    """Write the whole state (a tmp file of this process and thread, then a rename). Writers
    that change records use premerge_update, which holds the lock."""
    import threading
    PREMERGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREMERGE_STATE.with_name(f'{PREMERGE_STATE.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, PREMERGE_STATE)


@contextlib.contextmanager
def premerge_update():
    """One locked read-modify-write of PREMERGE_STATE (Codex on PR #66: the webhook and the
    cascade both write it). Yields the records; they are saved on a normal exit."""
    import fcntl
    PREMERGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(PREMERGE_STATE.with_name(PREMERGE_STATE.name + '.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        recs = load_premerge()
        yield recs
        save_premerge(recs)


def set_premerge(n, rec, only_if=None):
    """Put (or with rec None, drop) the record of PR n under the lock. only_if(old record)
    must be true for the change (a check against a newer writer)."""
    with premerge_update() as recs:
        if only_if is not None and not only_if(recs.get(str(n)) or {}):
            return False
        if rec is None:
            recs.pop(str(n), None)
        else:
            recs[str(n)] = rec
        return True


def label_write(n, op, names, repo=REPO):
    """minbot label add|remove N NAMES on repo (default: the platform repo). Returns None, or the error."""
    from .labels import MINBOT
    r = proc.sh(MINBOT, '--apply', 'label', op, str(n), *names, '--repo', repo, check=False, who='swarm-flow')
    return None if r.returncode == 0 else ((r.stderr or r.stdout or '').strip()[:200] or f'rc={r.returncode}')


def drop_premerge_labels(n, repo=REPO):
    """Remove merge-ready and pre-merging from PR n of repo, one write each. A label the PR
    does not carry is not an error. Returns the errors, as 'name: error' lines."""
    errs = []
    for name in (MERGE_READY, PRE_MERGING):
        err = label_write(n, 'remove', [name], repo=repo)
        if err and 'not found' not in err.lower() and '404' not in err:
            errs.append(f"{name}: {err}")
    return errs
RELAY_DIR = Path(os.environ.get('SWARM_RELAY_DIR', '/tmp'))  # relay-<N>-<kind>.md for swarm-resume


def shots_text(n):
    from .gatechecks import SCRATCH_RULE, SHOTS_RULE, STATUS_RULE
    return (f"Coordinator: #{n} needs screenshots before Human Review. Take them from staging (it serves "
            f"work with your head) or from a local run of the real app, never an isolated component page. "
            f"{SCRATCH_RULE} {SHOTS_RULE} {STATUS_RULE} Do not change code; push only the PR body edit.")


def clear_held():
    """tangentswarm #101 (Michal 2026-11-17 8:49 AM ET: drop parking): held.json is retired.
    The daemon calls this once at start. It deletes the file and returns the PR numbers it
    held. Nothing else is needed: the next main check (Ready) and the main recheck at daemon
    start (Staging) find each conflict again and send it to In Progress."""
    try:
        d = json.loads(HELD.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        d = {}
    try:
        HELD.unlink()
    except FileNotFoundError:
        pass
    return sorted(int(k) for k in (d if isinstance(d, dict) else {}) if str(k).isdigit())


def pulled_pr():
    """PR number of the oldest pull in flight, or None."""
    d = load_pull()
    return d['pr'] if d else None


PICK_FIELDS = ('ft', 'rank', 'labels', 'closes', 'due', 'milestone')


def pull_record(p, **kw):
    """The pull-in-flight record of Ready PR p. It keeps p's pick fields (board.pick_key), so
    future main sorts the pull with Staging in its own place (tangentswarm #69)."""
    pick = {k: p[k] for k in PICK_FIELDS if p.get(k) is not None}
    return dict(pr=p['number'], head=p['head'], branch=p.get('branch'), item=p.get('item'), pick=pick, **kw)


def pulled_prs():
    """The PR numbers of every pull in flight (read by the hook gate)."""
    return {d['pr'] for d in load_pulls()}


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{line}\n")


# ---------------------------------------------------------------------------
# pure helpers

def gate_verdict(rows):
    """('pass', []) | ('wait', reasons) | ('fail', reasons) for hr-gate rows of a pulled PR."""
    fails = [(n, why) for n, ok, why in rows if not ok]
    if not fails:
        return 'pass', []
    hard, wait = [], []
    for n, why in fails:
        if n.startswith(WAIT_ROWS) and not (n.startswith('5 ') and 'FAILURE' in why.upper()):
            if n.startswith('5 ') and not any(w in why for w in CI_WAIT_WORDS):
                hard.append(f"{n.strip()}: {why}")
            else:
                wait.append(f"{n.strip()}: {why}")
        else:
            hard.append(f"{n.strip()}: {why}")
    return ('fail', hard) if hard else ('wait', wait)


def gate_conflicts(rows):
    """tangentswarm #83: the failed hr-gate rows that are a real conflict at the final
    mergeability check: row 2 says the head conflicts with origin/main (a local merge-tree
    conflict, or GitHub CONFLICTING). "cannot check merge" is not a conflict."""
    return [f"{n.strip()}: {why}" for n, ok, why in rows
            if not ok and n.startswith('2 ') and 'CONFLICT' in (why or '').upper()]


def merge_main_text(n, reason):
    """The relay for a pulled PR that conflicts with origin/main."""
    return (f"Coordinator: #{n} left the Staging pull: {reason}. Merge origin/main into your branch "
            "(git fetch origin; git merge origin/main). Do not rebase and do not force-push. Resolve "
            "the conflicts, run the tests, push, and report the new head and the test counts.")


def ready_order(ready, conflicts=None, active=None):
    """Ready PRs in pull order: the shared pick order (board.pick_key: fast-track, the soonest
    milestone due date, the lowest number; tangentswarm #35, Michal 2026-11-15 6:09 PM ET: MVP
    PRs due Dec 1 went ahead of ptw 15 PRs due Nov 15). The pull still takes the first one in
    this order that merges cleanly; conflicts no longer reorder them. active: the active
    fast-track ticket (default: board.active_fast_track(); tangentswarm #65)."""
    if active is None:
        active = board.active_fast_track()
    return sorted(ready, key=lambda p: board.pick_key(p, active))


def vn_text(n, branch, rec):
    """The relay for a pre-merge conflict (tangentswarm #65): resolve it on the speculative
    -vN branch only; the PR's own branch stays as it is (clean against main)."""
    vn, files = rec['vbranch'], ', '.join(rec.get('files') or []) or 'files unknown'
    if rec.get('bad_tip'):
        return (f"Coordinator: #{n}: your {vn} tip {rec['bad_tip'][:8]} still has conflict markers (<<<<<<< or "
                f">>>>>>>). Finish the merge on {vn} only: remove every marker, run the tests, commit, and git push "
                f"origin {vn}. Never change {branch}. No rebase, no force-push. Report the new {vn} tip.")
    return (f"Coordinator: #{n} is next in Ready, and its head {rec['head'][:8]} conflicts with future main "
            f"(main + the Human Review and Staging PRs: {rec.get('base')}) in {files}. Resolve it on the NEW "
            f"speculative branch {vn} ONLY. Never merge main, work or staging into {branch}, and do not change {branch}. "
            f"Steps: commit or stash nothing new on {branch}; git fetch origin; git switch -c {vn} origin/{vn} "
            f"(it holds future main); git merge {rec['head']} (your head); resolve the conflicts; run the tests; "
            f"git push origin {vn}; then git switch {branch}. No rebase, no force-push. Report the {vn} tip and "
            "the test counts.")


def head_branch(n):
    """The head branch of platform PR n from GitHub, or '' when it cannot be read (Codex on
    PR #109: a board record may have no branch, and a worker start needs it)."""
    try:
        r = proc.sh('gh', 'pr', 'view', str(n), '-R', REPO, '--json', 'headRefName', '--jq', '.headRefName',
                    check=False, who='swarm-flow')
    except Exception:
        return ''
    return (r.stdout or '').strip() if r.returncode == 0 else ''


def pr_branch(p):
    """The head branch of board PR p: its record, else GitHub (head_branch), else ''."""
    return p.get('branch') or head_branch(p['number'])


def no_worker(said):
    """True when give_worker failed because the PR has no worktree or window to resume."""
    s = said or ''
    return any(w in s for w in ('no surviving worktree', 'not in the registry', 'is gone'))


def event_wants_step(name, payload):
    """Cheap filter (no API call): does this webhook event change the flow?"""
    if name == 'projects_v2_item':
        ch = (payload.get('changes') or {}).get('field_value') or {}
        return payload.get('action') in ('edited', 'reordered', 'deleted', 'restored', 'created') and (
            not ch or ch.get('field_node_id') == board.STATUS_FIELD or ch.get('field_name') == 'Status')
    if name == 'pull_request':
        pr = payload.get('pull_request') or {}
        return payload.get('action') == 'closed' and bool(pr.get('merged'))
    return False


# ---------------------------------------------------------------------------
# git in hr-gate's bare scratch repo (no worktree; never touches a Claude checkout)

def git(scratch, *args, check=True):
    r = proc.sh('git', '--git-dir', scratch.dir, *args, check=False, timeout=300, who='swarm-flow')
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:200]}")
    return r


MARKER_RE = re.compile(r'^\+(<{7}|>{7})( |$)', re.M)


def merge_problem_text(rc, out, diff):
    """Why a merge-tree result must not be committed, or None. git merge-tree --write-tree
    prints a tree SHA even on a conflict, and that tree holds <<<<<<< markers: work commit
    0d249b69 (#447) was made from such a tree. So refuse on a non-zero exit, on any CONFLICT
    line, and on any conflict marker the merge adds (diff: git diff a..tree)."""
    if rc:
        return f"merge-tree exit {rc} (conflict)"
    bad = [l for l in out.splitlines() if l.startswith('CONFLICT')]
    if bad:
        return bad[0][:160]
    m = MARKER_RE.search(diff or '')
    if m:
        return f"the merged tree adds a conflict marker ({m.group(1)})"
    return None


def merge_commit(scratch, a, b, msg, why=None):
    """Commit merging b into a, or None if the merge is not clean (merge_problem_text).
    why: a list that gets the reason on a refusal."""
    r = git(scratch, 'merge-tree', '--write-tree', '--messages', a, b, check=False)
    tree = (r.stdout.split() or [''])[0]
    diff = git(scratch, 'diff', a, tree, check=False).stdout if r.returncode == 0 and tree else ''
    bad = merge_problem_text(r.returncode, r.stdout, diff)
    if bad:
        if why is not None:
            why.append(bad)
        return None
    return git(scratch, 'commit-tree', tree, '-p', a, '-p', b, '-m', msg).stdout.strip()


def marker_problem(scratch, base, tip):
    """Why tip must not reach work, or None: the diff base..tip adds a conflict marker (Codex
    on PR #66: a worker can push a -vN with an unresolved merge; the same rule as
    merge_problem_text)."""
    r = git(scratch, 'diff', base, tip, check=False)
    if r.returncode:
        return f"cannot diff {str(base)[:8]}..{str(tip)[:8]}"
    m = MARKER_RE.search(r.stdout or '')
    return f"{str(tip)[:8]} adds a conflict marker ({m.group(1)})" if m else None


def conflict_files(scratch, a, b):
    """The paths that conflict when b merges into a (git merge-tree --name-only; local)."""
    r = git(scratch, 'merge-tree', '--write-tree', '--name-only', '--no-messages', a, b, check=False)
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in (r.stdout or '').splitlines()[1:] if l.strip()]
    return sorted(set(lines))


def merge_rows(run, base, top):
    """[(commit, first parent, second parent)] of each first-parent commit in base..top,
    oldest first; None if base is not an ancestor of top or a commit there is not a
    two-parent merge. run: a git runner (run(*args, check=False))."""
    if run('merge-base', '--is-ancestor', base, top, check=False).returncode != 0:
        return None
    r = run('rev-list', '--first-parent', '--parents', f'{base}..{top}', check=False)
    if r.returncode != 0:
        return None
    rows = [line.split() for line in (r.stdout or '').splitlines() if line.strip()]
    return None if any(len(row) != 3 for row in rows) else rows[::-1]


def built_from_heads(run, tip, head, base_main, heads):
    """Codex on PR #68: True if the pre-merged tip is the merge of future main and the PR
    head (second parent: the head), and that future main is base_main plus one merge per
    queued head, in queue order (second parent: that head, never a resolved -vN tip).
    Then the head itself merged cleanly onto main + the queued heads."""
    r = run('rev-list', '--parents', '-n', '1', tip, check=False)
    row = (r.stdout or '').split() if r.returncode == 0 else []
    if len(row) != 3 or row[2] != head:
        return False
    rows = merge_rows(run, base_main, row[1])
    return (rows is not None and len(rows) == len(heads)
            and all(x[2].startswith(h) for x, h in zip(rows, heads)))


def merged_onto(run, main, base_main, heads):
    """True if main is base_main plus one merge commit per head (first parent: the main
    before it; second parent: that head), in any order, and nothing else."""
    rows = merge_rows(run, base_main, main)
    if rows is None:
        return False
    left = list(heads)
    for row in rows:
        hit = next((h for h in left if row[2].startswith(h)), None)
        if hit is None:
            return False
        left.remove(hit)
    return not left


def stack_conflict(scratch, main, bases, head):
    """None if head stacks cleanly on main + bases (in order); else the base it conflicts with."""
    cur = main
    for name, sha in bases:
        nxt = merge_commit(scratch, cur, sha, f'stack {name}')
        if nxt is None:
            return f'stack below it ({name})'
        cur = nxt
        if merge_commit(scratch, cur, head, 'probe') is None:
            return name
    return None


# ---------------------------------------------------------------------------
# the step

class Flow:
    def __init__(self, dry_run=False, out=print, g=None, now=time.time):
        self.dry, self.out, self.now = dry_run, out, now
        self.did, self.notes = [], []
        self._fm, self._premerged = {}, {}  # one future main and one pre-merge per PR per pass
        self.dry_pulls = []  # a dry run's pulls: not saved, but in future main like a real pull
        self.tool_timeout = 1800  # one hr-gate / pending-promote run; the cascade sets less
        self.stale_ok = False  # the cascade moves cards from a cache that is only stale (tangentswarm #136)
        from .conflictmap import ConflictMap
        self.cmap = ConflictMap()  # merge-tree answers per SHA pair
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_flow')

    def change(self, text):
        self.did.append(('would: ' if self.dry else 'did: ') + text)
        if not self.dry:
            log('did\t' + text)

    def note(self, text):
        self.notes.append(text)

    def tool(self, argv):
        env = dict(os.environ, **{gatelock.HELD_ENV: '1', gatelock.OLD_HELD_ENV: '1'})
        r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=self.tool_timeout, env=env)
        out = (r.stdout or '') + (r.stderr or '')
        log(f"tool\t{' '.join(str(a) for a in argv)}\trc={r.returncode}\n{out.rstrip()}")
        if r.returncode and gqlmeter.is_rate_limit(out):  # not a gate verdict: stop, do not fail the PR
            raise gqlmeter.RateLimited(f"{Path(str(argv[0])).name}: GitHub rate limit", gqlmeter.reset_time())
        return r.returncode, out

    # -- board ----------------------------------------------------------------
    def columns(self):
        from . import tick
        prs = tick.board_prs_open({'gql': self.g['gql'], 'PARKED': self.g.get('PARKED', set())},
                                  stale_ok=self.stale_ok)
        cols = {}
        for n, p in prs.items():
            if p['state'] == 'OPEN' and not p['parked']:
                cols.setdefault(p['col'], []).append(p)
        return prs, cols

    # -- 1. promote -----------------------------------------------------------
    def promote(self, cols):
        hr, staging = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if len(hr) >= HR_TARGET or not staging:
            if len(hr) < HR_TARGET:
                self.note(f"Human Review {len(hr)}/{HR_TARGET} and Staging is empty: nothing to promote")
            return False
        if self.dry:
            self.change(f"pending-promote --apply (Human Review {len(hr)}/{HR_TARGET}, Staging {len(staging)})")
            return True
        rc, out = self.tool([BIN / 'pending-promote', '--apply'])
        moved = [l.strip() for l in out.splitlines() if 'moved #' in l]
        self.change('pending-promote --apply: ' + ('; '.join(moved) if moved else f'rc={rc}, nothing moved'))
        return True

    # -- 2. pull --------------------------------------------------------------
    def clean(self, scratch, a, b):
        """git merge-tree a b is clean? Kept per SHA pair in the conflict map."""
        return self.cmap.clean(a, b, lambda: self.g['merge_tree_clean'](scratch.dir, a, b))

    def files(self, scratch, a, b):
        """The conflicting paths of a and b. Kept per SHA pair in the conflict map."""
        return self.cmap.files(a, b, lambda: conflict_files(scratch, a, b))

    @staticmethod
    def sha_of(scratch, ref):
        r = git(scratch, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)
        sha = (r.stdout or '').strip()
        return sha if r.returncode == 0 and len(sha) == 40 else ref

    def pull(self, prs, cols, scratch, limit=None, strict=False):
        try:
            return self._pull(prs, cols, scratch, limit, strict)
        finally:
            self.cmap.save()

    def _pull(self, prs, cols, scratch, limit=None, strict=False):
        """Start one pull. Returns the pulled PR, or None. limit: pull only while Staging has
        fewer PRs than this (default STAGING_MIN).
        tangentswarm #65: only the NEXT Ready PR in the pick order is pulled, as its
        pre-merged tip (premerge_one: <branch>-vN = future main + head); when it is not
        merge-ready, nothing behind it is pulled. Not strict (swarm-flow step): a PR that
        does not merge onto main first goes back to its worker with a merge-main relay.
        strict (the swarm-advance cascade): no such relay (main_check does it)."""
        staging = cols.get(board.STAGING, [])
        if len(staging) >= (STAGING_MIN if limit is None else limit):
            return None
        ready = [p for p in cols.get(board.READY, []) if not p['draft']]
        if not ready:
            self.note(f"Staging {len(staging)}/{STAGING_MIN} and Ready is empty: refill needed")
            return
        scratch.ensure_main()
        main = self.sha_of(scratch, scratch.main)  # a SHA, so the conflict map can key on it
        hr = cols.get(board.HUMAN_REVIEW, [])
        queue = self.staging_order(staging)
        bases = [(f"#{p['number']}", p['head']) for p in hr + queue]
        scratch.ensure(*[sha for _, sha in bases], *[p['head'] for p in ready])
        conflicts = {}
        for p in ready:
            conflicts[p['number']] = sum(not self.clean(scratch, sha, p['head'])
                                         for sha in [main] + [s for _, s in bases])
        fm = self.future_main(scratch, cols)
        for p in ready_order(ready, conflicts, board.active_fast_track()):
            n = p['number']
            if not strict and not self.clean(scratch, main, p['head']):
                self.fail(p, 'does not merge cleanly onto main: merge origin/main into the branch (no rebase)',
                          relay='merge-main')
                continue  # it left Ready (In Progress with its worker)
            # tangentswarm #65: strict order; the next PR must be pre-merged onto future main
            # (Codex on PR #66: also for swarm-flow step, so a queue conflict gets its -vN worker)
            tip = self.premerge_one(p, scratch, fm)
            if not tip:
                self.note(f"#{n} is next in the pick order and is not merge-ready: nothing behind it is pulled")
                return None
            return p if self.start_pull(p, scratch, tip, pre_merged=True, fm=fm) else None
        self.note('no Ready PR merges cleanly onto main: each one went back to its worker')

    def staging_order(self, staging):
        """Staging in the shared pick order (board.pick_key; tangentswarm #35)."""
        return sorted(staging, key=lambda p: board.pick_key(p, board.active_fast_track()))

    def fetch_work(self, scratch):
        """Fetch origin work into the scratch repo; return its commit."""
        git(scratch, 'fetch', '--no-tags', '--quiet', self.g['origin_url'](), f'{WORK}:refs/heads/{WORK}')
        return git(scratch, 'rev-parse', f'refs/heads/{WORK}').stdout.strip()

    def merge_into_work(self, scratch, n, tip, label, pre_merged=False, fm=None):
        """Merge tip into origin work and push it (a fast-forward; never with --dry-run).
        Returns (work commit, None), (work commit, 'already in work') or (None, why). The merge is refused unless it is clean
        (merge_problem_text): a conflict never reaches work. pre_merged: tip is a pre-merged
        -vN, so work gets the tree of future main fm + the tip (work_commit; tangentswarm #65)."""
        work = self.fetch_work(scratch)
        if pre_merged:
            tree = self.onto(scratch, (fm or (None, None, None))[1], tip, f"future main + #{n}")
            if tree is None:
                return None, f"pre-merged tip {tip[:8]} does not merge cleanly onto future main"
            merged = self.work_commit(scratch, work, tree, [tip], f"Staging: {WORK} = future main + #{n} ({label})\n\n"
                                                                  f"swarm-flow, pre-merged tip {tip}.")
            if merged is None:
                return None, f"pre-merged tip {tip[:8]} has a conflict marker"
            if not self.dry:
                git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{merged}:refs/heads/{WORK}')
                self.pushed_work(merged, f"#{n}")
            return merged, None
        if git(scratch, 'merge-base', '--is-ancestor', tip, work, check=False).returncode == 0:
            return work, 'already in work'
        why = []
        merged = merge_commit(scratch, work, tip, f"Merge #{n} ({label}) into {WORK} for Staging\n\n"
                                                  f"swarm-flow, tip {tip}.", why=why)
        if merged is None:
            return None, f"{label} {tip[:8]} does not merge cleanly into {WORK} {work[:8]}: {why[0]}"
        if not self.dry:
            git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{merged}:refs/heads/{WORK}')
            self.pushed_work(merged, f"#{n}")
        return merged, None

    def pushed_work(self, sha, what):
        """tangentswarm #136: after a push to work, start the staging deploy now (no timer wait).
        Never raises; the timer stays as the backup."""
        from . import stagingdeploy
        note = stagingdeploy.start(f"{WORK} {sha[:8]}: {what}")
        log('deploy\t' + note)
        self.note(note)

    def start_pull(self, p, scratch, tip=None, pre_merged=False, fm=None):
        """tip: the pre-merged -vN tip to merge into work (default: the head). fm: the future
        main the tip goes onto (a kept -vN may be built on an older one; tangentswarm #69)."""
        n, head = p['number'], p['head']
        tip = tip or head
        merged, why = self.merge_into_work(scratch, n, tip, p['branch'], pre_merged=pre_merged, fm=fm)
        if merged is None:
            self.note(f"#{n} refused, work NOT pushed: {why}")
            return False
        self.last_work = merged  # the work commit (also for a dry run, where it is not pushed)
        rec = pull_record(p, tip=tip, work=merged, started=self.now(), **({'pre_merged': True} if pre_merged else {}))
        if self.dry:
            self.dry_pulls.append(rec)
        else:
            save_pull(rec)
        what = f"head {head[:8]}" if tip == head else f"pre-merged tip {tip[:8]} (head {head[:8]})"
        self.change(f"#{n} pulled: merged {what} into {WORK} ({merged[:8]}); staging redeploys")
        return True

    def pull_batch(self, prs, cols, scratch, room):
        """Staging stocking: pull Ready PRs, up to room, in ONE work push, so one staging
        redeploy covers all of them. tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): strict
        order, no queue-jumping. The PRs go in the shared pick order; each one must be
        merge-ready (premerge_one: pre-merged onto future main as a <branch>-vN tip) and
        merge cleanly with the batch so far. The first one that is not stops the batch:
        nothing behind it is pulled. work gets the tree of future main + the batch
        (work_commit). Returns the pulled PRs."""
        try:
            return self._pull_batch(prs, cols, scratch, room)
        finally:
            self.cmap.save()

    def _pull_batch(self, prs, cols, scratch, room):
        ready = [p for p in cols.get(board.READY, []) if not p['draft']]
        if not ready or room < 1:
            return []
        fm = self.future_main(scratch, cols)
        work = self.fetch_work(scratch)
        target, batch, tips = None, [], {}  # target: future main + the batch so far
        for p in ready_order(ready, None, board.active_fast_track()):
            if len(batch) >= room:
                break
            n = p['number']
            t = self.premerge_one(p, scratch, fm)
            if not t:
                self.note(f"#{n} is next in the pick order and is not merge-ready: nothing behind it is pulled")
                break
            if target is not None:  # each tip is future main + one PR: they must merge with each other
                why = []
                merged = merge_commit(scratch, target, t, f"batch: + #{n}", why=why)
                if merged is None:
                    self.note(f"#{n} is next in the pick order and its tip {t[:8]} does not merge cleanly with "
                              f"{', '.join('#' + str(q['number']) for q in batch)} in this batch: {why[0]}; "
                              "nothing behind it is pulled (the next pass pre-merges it onto them)")
                    break
                t_target = merged
            else:  # a kept -vN may be built on an older future main (tangentswarm #69)
                t_target = self.onto(scratch, fm[1], t, f"batch: future main + #{n}")
                if t_target is None:
                    self.note(f"#{n} is next in the pick order and its tip {t[:8]} does not merge cleanly onto "
                              "future main: nothing behind it is pulled")
                    break
            target = t_target
            batch.append(p)
            tips[n] = t
        if not batch:
            return []
        # Codex on PR #66: work gets the tree of future main + the batch, never a merge with
        # work's old content (a PR sent back must not stay in it, or stay recorded as merged)
        tip = self.work_commit(scratch, work, target, [tips[p['number']] for p in batch],
                               f"Staging: {WORK} = future main + {', '.join('#' + str(p['number']) for p in batch)}"
                               f"\n\nswarm-flow batch, future main {fm[0]}.")
        if tip is None:
            return []
        if not self.dry:
            git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{tip}:refs/heads/{WORK}')
            self.pushed_work(tip, ', '.join(f"#{p['number']}" for p in batch))
        self.last_work = tip
        recs = [pull_record(p, tip=tips[p['number']], work=tip, started=self.now(), pre_merged=True) for p in batch]
        if self.dry:
            self.dry_pulls += recs
        else:
            save_pulls(load_pulls() + recs)
        names = ', '.join(f"#{p['number']} {tips[p['number']][:8]}" for p in batch)
        self.change(f"{names} pulled: {len(batch)} pre-merged tip(s) in {WORK} ({tip[:8]} = future main + the batch) "
                    "in one push; staging redeploys once")
        return batch

    # -- pre-merge onto future main (tangentswarm #65, Michal 2026-11-16 3:22 and 3:24 PM ET) --
    def onto(self, scratch, base, tip, msg):
        """A commit with base and tip in it: tip when base is None or already in tip, else the
        merge of tip into base (None if it is not clean). A kept -vN (tangentswarm #69) can be
        built on an older future main, so a pull never takes its tree as is."""
        if base is None or self.ancestor(scratch, base, tip):
            return tip
        return merge_commit(scratch, base, tip, msg)

    def pulls_in_flight(self):
        """The pulls in flight: the saved ones, then the ones of this dry run."""
        pulls = load_pulls()
        have = {d['pr'] for d in pulls}
        return pulls + [d for d in self.dry_pulls if d['pr'] not in have]

    def work_commit(self, scratch, work, tree_of, tips, msg):
        """The new work commit for a pull of pre-merged tips (tangentswarm #65; Codex on PR
        #66): its tree is `tree_of`'s tree (future main + the pulled PRs), never a merge with
        work's old content, and its parents are work and the tips, so the push is a
        fast-forward (no force) and a PR that was pulled, sent back and pulled again gets
        all of its content back. Nothing outside future main stays in work."""
        bad = marker_problem(scratch, work, tree_of)
        if bad:  # never a conflict marker in work
            self.note(f"{WORK} NOT pushed: {bad}")
            return None
        parents = [x for t in [work, *tips] for x in ('-p', t)]
        return git(scratch, 'commit-tree', f'{tree_of}^{{tree}}', *parents, '-m', msg).stdout.strip()

    def have(self, scratch, *shas, branch=None):
        """True when every commit is in the scratch repo: fetched when missing (scratch.ensure,
        then origin branch `branch`). A missing commit is never read as a conflict (Codex on PR #66)."""
        def missing():
            return [x for x in shas if not x or git(scratch, 'cat-file', '-e', f'{x}^{{commit}}', check=False).returncode]
        if not missing():
            return True
        try:
            scratch.ensure(*shas)
        except RuntimeError:
            pass
        if missing() and branch:
            self.fetch_branch(scratch, branch)
        return not missing()

    def future_main(self, scratch, cols):
        """(key, commit or None, why): main plus every queued PR merged in, in queue order
        (Human Review, then Staging and the pulls in flight together, each in the pick order;
        tangentswarm #69: a pull keeps its place from the pull until its card is in Staging). key
        names main and the queued heads, so a pre-merge is redone only when future main
        changes. A queued PR enters as its pre-merged tip (a resolved -vN) only while every
        PR that tip was built on is still ahead of it in the queue or in main (queued_tip;
        Codex on PR #66: a tip built on a PR that was sent back carried that PR along).
        Else as its head; if the head does not merge, it is pre-merged again onto the queue
        ahead of it (a new -vN; its worker resolves a real conflict)."""
        scratch.ensure_main()
        main = self.sha_of(scratch, scratch.main)
        active = board.active_fast_track()
        hr = cols.get(board.HUMAN_REVIEW, [])
        staging = list(cols.get(board.STAGING, []))
        seen = {q['number'] for q in hr + staging}
        cards = {q['number']: q for c in cols.values() for q in c}  # a pull's card (in Ready): its pick fields
        for d in self.pulls_in_flight():  # in work, on their way to Staging
            if d['pr'] in seen:
                continue
            seen.add(d['pr'])
            pick = dict(d.get('pick') or {}, **{k: v for k, v in (cards.get(d['pr']) or {}).items()
                                                 if k in PICK_FIELDS})
            staging.append(dict(pick, number=d['pr'], head=d['head'], branch=d.get('branch'), item=d.get('item'),
                                col='pull'))
        queue = [q for c in (hr, staging) for q in sorted(c, key=lambda q: board.pick_key(q, active))]
        toks = [f"#{q['number']}@{q['head'][:12]}" for q in queue]
        key = ' '.join([main[:12]] + toks)
        if key in self._fm:
            return self._fm[key]
        lost = [q for q in queue if not self.have(scratch, q['head'])]
        if lost:  # never cached: the next pass fetches again
            return key, None, f"#{lost[0]['number']} head {lost[0]['head'][:8]} is not in the scratch repo"
        recs = load_premerge()
        cur, why = main, None
        for i, q in enumerate(queue):
            use = self.queued_tip(scratch, q, recs.get(str(q['number'])), toks[:i], main) or q['head']
            if not self.ancestor(scratch, use, cur):
                nxt = merge_commit(scratch, cur, use, f"future main: + #{q['number']}")
                if nxt is None and q.get('branch'):  # pre-merge it again onto the queue ahead of it
                    nxt = self.premerge_one(q, scratch, (' '.join([main[:12]] + toks[:i]), cur, None))
                    nxt = nxt and self.onto(scratch, cur, nxt, f"future main: + #{q['number']}")
                if nxt is None:
                    cur, why = None, (f"#{q['number']} does not merge onto main + the queue ahead of it; "
                                      "it is pre-merged again (see its -vN)")
                    break
                cur = nxt
        self._fm[key] = (key, cur, why)
        return self._fm[key]

    def queued_tip(self, scratch, q, rec, ahead, main):
        """The pre-merged tip of queued PR q if it is still valid: the same head, and every
        PR in the future main it was built on (its record's base) is still in `ahead` (the
        queue tokens before q) or merged into main. Else None."""
        rec = rec or {}
        if not (rec.get('status') == 'ready' and rec.get('head') == q['head'] and rec.get('tip')):
            return None
        if not self.deps_kept(scratch, rec, ahead, main):
            return None
        if not self.have(scratch, rec['tip'], branch=rec.get('vbranch')):
            return None
        return None if marker_problem(scratch, main, rec['tip']) else rec['tip']

    def deps_kept(self, scratch, rec, ahead, main):
        """True when every PR in the future main that rec's tip was built on (its base) is
        still in `ahead` (queue tokens) or merged into main: the tip carries no PR that left."""
        for d in (rec.get('base') or '').split()[1:]:
            if d in ahead:
                continue
            h = d.split('@', 1)[-1]
            if not (h and self.ancestor(scratch, h, main)):
                return False
        return True

    def kept_tip(self, scratch, rec, key, base, head):
        """tangentswarm #69: the merge-ready -vN tip of rec, kept on a changed future main
        (key, base) when it still merges cleanly onto it: the same head, every PR it was built
        on still queued or in main, no conflict marker, and a clean merge with base. Else None."""
        if not (rec and rec.get('status') == 'ready' and rec.get('head') == head and rec.get('tip')
                and rec.get('vbranch') and base is not None):
            return None
        main, tip = key.split()[0], rec['tip']
        if not self.deps_kept(scratch, rec, key.split()[1:], main):
            return None
        if marker_problem(scratch, main, tip):
            return None
        return tip if self.onto(scratch, base, tip, 'probe') is not None else None

    def assumed(self, scratch, rec, key, head):
        """tangentswarm #111 (Michal 2026-11-17 10:45 AM ET): the tip of rec when future main
        changed only in a way the tip already assumed: some PRs it was built on (the first
        ones in its base) merged into main as their heads, and the rest of the queue ahead of
        it is the same. Then the tip needs no probe merge, no new -vN and no record write.
        Only a mechanical pre-merge counts (no seed: the tip is future main + the head), and
        its future main must be main + one merge per queued head (built_from_heads), so the
        new main holds exactly what the tip holds. Else None (kept_tip or a new -vN)."""
        if not (rec and rec.get('status') == 'ready' and rec.get('head') == head and rec.get('tip')
                and rec.get('vbranch') and not rec.get('seed') and isinstance(rec.get('base'), str)):
            return None
        old_main, *old = rec['base'].split() or ['']
        new_main, *new = key.split() or ['']
        cut = len(old) - len(new)
        gone = old[:cut]
        if cut < 1 or old[cut:] != new or any(x in new for x in gone):
            return None
        run = lambda *a, check=False: git(scratch, *a, check=check)  # noqa: E731
        try:
            if not built_from_heads(run, rec['tip'], head, old_main, [x.split('@', 1)[-1] for x in old]):
                return None
            if not merged_onto(run, self.sha_of(scratch, new_main), old_main, [x.split('@', 1)[-1] for x in gone]):
                return None
        except RuntimeError:
            return None
        return rec['tip']

    def premerge(self, cols, scratch):
        """Every cascade pass: keep the top PREMERGE_AHEAD Ready PRs pre-merged onto future
        main (premerge_one); every Staging and Human Review PR carries merge-ready; a PR sent
        back (In Progress, Auto Review, Needs Input) loses both labels (its pre-merge is
        void). Returns {PR: tip or None} for the pre-merged Ready PRs."""
        self._premerged = {}  # a new pass: each PR is looked at again
        for c in (board.STAGING, board.HUMAN_REVIEW):
            for p in cols.get(c, []):
                self.set_labels(p, MERGE_READY, drop=False)
        for c in (board.IN_PROGRESS, board.AUTO_REVIEW, board.NEEDS_INPUT):
            for p in cols.get(c, []):
                self.set_labels(p, None)
        flying = {d['pr'] for d in self.pulls_in_flight()}
        ready = [p for p in cols.get(board.READY, []) if not p.get('draft') and p['number'] not in flying]
        top = ready_order(ready, None, board.active_fast_track())[:PREMERGE_AHEAD]
        if not top:
            return {}
        fm = self.future_main(scratch, cols)
        if fm[1] is None:
            self.note(f"pre-merge: future main cannot be built ({fm[2]}); no Ready PR is pre-merged")
        return {p['number']: self.premerge_one(p, scratch, fm) for p in top}

    def premerge_one(self, p, scratch, fm):
        """The merge-ready tip of Ready PR p, or None. A new <branch>-vN = future main + the
        PR head, made mechanically when it merges cleanly (label merge-ready). On a real
        conflict, <branch>-vN = future main is pushed and the PR's worker resolves the
        merge there (label pre-merging); its pushed tip that holds both makes it
        merge-ready. Nothing is ever merged into the PR's own branch. A new head or a new
        future main rebuilds it as the next -vN. Kept per PR in PREMERGE_STATE."""
        n, head, branch = p['number'], p['head'], p.get('branch') or f'pr-{p["number"]}'
        key, base, why = fm
        if n in self._premerged:
            return self._premerged[n]
        if not self.have(scratch, head):  # Codex on PR #66: a missing head is not a conflict
            self.note(f"#{n}: head {head[:8]} is not in the scratch repo (fetch failed): not pre-merged this pass")
            self._premerged[n] = None
            return None
        recs = load_premerge()
        rec = recs.get(str(n))
        if (rec and rec.get('status') == 'ready' and rec.get('tip')
                and not self.have(scratch, rec['tip'], branch=rec.get('vbranch'))):
            rec = None  # its tip is gone (a deleted -vN): build the next -vN
        if rec and rec.get('head') == head and rec.get('base') == key and rec.get('vbranch'):
            main = key.split()[0]
            if rec.get('status') == 'ready' and not marker_problem(scratch, main, rec['tip']):
                self.set_labels(p, MERGE_READY)
                self._premerged[n] = rec['tip']
                return rec['tip']
            tip = self.fetch_branch(scratch, rec['vbranch'])
            bad = marker_problem(scratch, main, tip) if tip and tip != rec.get('seed') else None
            if bad:  # Codex on PR #66: an unresolved merge is never merge-ready
                self.note(f"#{n}: {rec['vbranch']} {bad}: not merge-ready; its worker is told")
                rec = dict(rec, status='conflict', bad_tip=tip)
                if not self.dry:
                    set_premerge(n, rec)
            elif (tip and tip != rec.get('seed') and self.ancestor(scratch, head, tip)
                    and self.ancestor(scratch, rec.get('seed') or '', tip)):
                if not self.dry:
                    rec.update(status='ready', tip=tip)
                    set_premerge(n, rec)
                self.set_labels(p, MERGE_READY)
                self.change(f"#{n} merge-ready: its worker resolved the pre-merge on {rec['vbranch']} ({tip[:8]})")
                self._premerged[n] = tip
                return tip
            self.set_labels(p, PRE_MERGING)
            self.vn_worker(p, rec)
            self._premerged[n] = None
            return None
        held = self.assumed(scratch, rec, key, head)
        if held:  # tangentswarm #111: only PRs it was built on merged: no probe, no write
            self.set_labels(p, MERGE_READY)
            self._premerged[n] = held
            return held
        kept = self.kept_tip(scratch, rec, key, base, head)
        if kept:  # tangentswarm #69: no new -vN for a tip that still merges onto future main
            if rec.get('checked') != key:
                if not self.dry:
                    set_premerge(n, dict(rec, checked=key),
                                 only_if=lambda old: (old or {}).get('vbranch') == rec['vbranch'])
                self.change(f"#{n} keeps {rec['vbranch']} {kept[:8]}: it still merges cleanly onto future main ({key})")
            self.set_labels(p, MERGE_READY)
            self._premerged[n] = kept
            return kept
        if base is None:
            self.note(f"#{n}: not pre-merged: future main cannot be built ({why})")
            self._premerged[n] = None
            return None
        vn = self.next_vn(scratch, branch, rec)
        merged = merge_commit(scratch, base, head, f"Pre-merge #{n} ({branch}) onto future main\n\n"
                                                   f"swarm-flow, future main {key}, head {head}.")
        if merged is not None:
            if not self.dry:
                git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{merged}:refs/heads/{vn}')
                set_premerge(n, dict(head=head, base=key, vbranch=vn, tip=merged, status='ready'))
            self.set_labels(p, MERGE_READY)
            self.change(f"#{n} pre-merged onto future main ({key}): {vn} {merged[:8]} is merge-ready")
            self._premerged[n] = merged
            return merged
        files = conflict_files(scratch, base, head)
        rec = dict(head=head, base=key, vbranch=vn, seed=base, status='conflict', files=files, relayed='')
        if not self.dry:
            git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{base}:refs/heads/{vn}')
            set_premerge(n, rec)
        self.set_labels(p, PRE_MERGING)
        self.change(f"#{n} conflicts with future main ({key}) in {', '.join(files) or 'files unknown'}: "
                    f"{vn} holds future main for its worker")
        self.vn_worker(p, rec)
        self._premerged[n] = None
        return None

    def next_vn(self, scratch, branch, rec=None):
        """<branch>-v(N+1): N is the highest -vN on origin (or in the record)."""
        r = git(scratch, 'ls-remote', '--heads', self.g['origin_url'](), f'refs/heads/{branch}-v*', check=False)
        ns = [int(m.group(1)) for line in (r.stdout or '').splitlines()
              for m in [re.search(re.escape(branch) + r'-v(\d+)$', line)] if m]
        m = re.search(r'-v(\d+)$', (rec or {}).get('vbranch') or '')
        ns += [int(m.group(1))] if m else []
        return f"{branch}-v{max(ns, default=0) + 1}"

    def fetch_branch(self, scratch, name):
        """The tip of origin branch name, fetched into the scratch repo, or None."""
        ref = f'refs/swarm-premerge/{name}'  # its own namespace: never a local branch
        r = git(scratch, 'fetch', '--no-tags', '--quiet', self.g['origin_url'](), f'+refs/heads/{name}:{ref}',
                check=False)
        if r.returncode:
            return None
        return git(scratch, 'rev-parse', ref, check=False).stdout.strip() or None

    @staticmethod
    def ancestor(scratch, a, b):
        return bool(a) and git(scratch, 'merge-base', '--is-ancestor', a, b, check=False).returncode == 0

    def set_labels(self, p, want, drop=True):
        """Make the PR carry `want` (MERGE_READY, PRE_MERGING or None) of the two pre-merge
        labels; drop=True also removes the other one. Writes go through minbot; a failed
        write is a note (the next pass tries again)."""
        n = p['number']
        have = set(p.get('own_labels') if p.get('own_labels') is not None else p.get('labels') or ())
        add = [want] if want and want not in have else []
        rm = [l for l in (MERGE_READY, PRE_MERGING) if l != want and l in have] if drop else []
        for op, names in (('add', add), ('remove', rm)):
            if not names:
                continue
            if self.dry:
                self.change(f"#{n} would {op} label {', '.join(names)}")
                continue
            err = label_write(n, op, names)
            if err:
                self.note(f"#{n}: label {op} {', '.join(names)} failed: {err}")
                continue
            have = (have | set(names)) if op == 'add' else (have - set(names))
            self.change(f"#{n} label {op} {', '.join(names)}")
        p['own_labels'] = sorted(have)

    def vn_worker(self, p, rec):
        """Send the PR's worker the -vN conflict relay, once per record (head, future main,
        -vN). give_worker with priority: no rework-slot wait (slots.conflict_capacity is the
        only limit); a PR with no worktree gets a worker start (start_worker). The card stays
        in Ready (label pre-merging): it keeps its place in the order. NEEDS_WORKER when no
        worker takes it; the next pass tries again."""
        n = p['number']
        token = rec.get('vbranch') + (f"@{rec['bad_tip']}" if rec.get('bad_tip') else '')
        if rec.get('relayed') == token:
            return True
        text = vn_text(n, p.get('branch') or '', rec)
        if self.dry:
            self.change(f"#{n} would give its worker the pre-merge conflict on {rec['vbranch']}")
            return False
        relay_path = RELAY_DIR / f"relay-{n}-premerge.md"
        try:
            relay_path.write_text(text + '\n')
        except OSError:
            pass
        rc, said = self.give_worker(n, text, priority=True)
        if rc != 0 and no_worker(said):
            said = self.start_worker(p, relay_path, said)
        if rc != 0:
            self.note(f"NEEDS_WORKER #{n}: its pre-merge on {rec['vbranch']} conflicts; no worker took the relay "
                      f"({said}); the next pass tries again")
            return False
        with premerge_update() as recs:  # only the record of this -vN (a newer one may exist)
            if (recs.get(str(n)) or {}).get('vbranch') == rec['vbranch']:
                recs[str(n)]['relayed'] = token
        rec['relayed'] = token
        self.change(f"#{n}: worker assigned to resolve {rec['vbranch']} ({said})")
        return True

    def merge_work(self, n, tip, label):
        """swarm-flow merge-work: the one safe staging merge for a hand step (a -vN tip)."""
        with self.g['ScratchRepo']() as scratch:
            scratch.ensure(tip)
            merged, why = self.merge_into_work(scratch, n, tip, label)
        if merged is None:
            self.out(f"refused: {why}. {WORK} is unchanged.")
            return 1
        if why:
            self.out(f"#{n} {label} {tip[:8]} is already in {WORK} {merged[:8]}: nothing to push")
            return 0
        self.out(f"{'would push' if self.dry else 'pushed'} {WORK} {merged[:8]}: #{n} {label} {tip[:8]}")
        return 0

    # -- 3. advance -----------------------------------------------------------
    def advance(self, d, prs, scratch, served=False):
        """served: the caller already confirmed the priority staging site serves the pull
        (Cascade.gate_pulls), so a pre-merged Ready PR moves at once (stage)."""
        n = d['pr']
        p = prs.get(n)
        if p is None or p['state'] != 'OPEN':
            self.change(f"#{n} pull dropped: the PR is no longer open on the board")
            if not self.dry:
                drop_pull(n)
            return
        if p['col'] in (board.STAGING, board.HUMAN_REVIEW):
            self.change(f"#{n} pull finished: the card is in {p['col']}")
            if not self.dry:
                drop_pull(n)
            return
        if p['head'] != d['head']:  # tangentswarm #83: not a conflict; the new head goes to Auto Review
            self.leave(p, f"head moved during the pull ({d['head'][:8]} -> {p['head'][:8]}): needs Codex and "
                          "MinBot again", tell=False)
            return
        full = self.g['fetch'](n, refresh=True)
        if d.get('pre_merged') and p['col'] == board.READY and not p.get('draft'):
            why = self.identity_problem(d, full) or self.evidence_problem(full) or self.work_health(full)
            if not why:
                return self.stage(d, p, served, full)  # tangentswarm #136: no gate re-run for a Ready PR
            # Codex on PR #138: Ready does not prove 7a/7b (a UI PR may reach Ready while its
            # screenshot job runs): the full gate path below waits for it, as before.
            self.note(f"#{n}: {why}: the full gate runs")
        rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](refresh=True), scratch=scratch)
        verdict, why = gate_verdict(rows)
        held, status_job = self.status_hold(n, full, d['head']) if verdict != 'fail' else (None, None)
        if held:  # tangentswarm #130: a body that says where the PR stands waits for its worker
            verdict, why = 'wait', why + [held]
        age = int(self.now() - d['started'])
        shots = [w for w in why if w.startswith('7a')] if verdict == 'wait' else []
        # Michal 2026-11-15: screenshots are a job for a worker now, not a timeout. Every open
        # job goes in one relay (Codex on PR #133: the revive queue keeps one entry per PR).
        jobs_now = ([('shots', shots_text(n))] if shots else []) + ([('status', status_job)] if held else [])
        if jobs_now:
            self.worker_jobs(n, d['head'], jobs_now, p)
        # Worker jobs (screenshots, status text) never time out: a fail would drop the pull, and
        # the next pass would pull and push it to work again (Codex on PR #133).
        jobs = [w for w in why if w.startswith(WORKER_JOB_ROWS)] if verdict == 'wait' else []
        if verdict == 'wait' and age > PULL_TIMEOUT and len(jobs) < len(why):
            verdict, why = 'fail', [f"still waiting after {age // 60} min: " + '; '.join(why)]
        elif verdict == 'wait' and age > PULL_TIMEOUT:
            if shots:
                self.note(f"SCREENSHOTS_NEEDED #{n}: still no screenshots after {age // 60} min; the pull stays "
                          "open (never demoted into a column with no worker)")
            if held:
                self.note(f"STATUS_TEXT #{n}: the body still states where the PR stands after {age // 60} min; "
                          "the pull stays open")
        if verdict == 'wait':
            self.note(f"#{n} pull waiting ({age // 60} min): " + '; '.join(w[:120] for w in why))
            return
        if verdict == 'fail':
            bad = gate_conflicts(rows)
            if bad:  # tangentswarm #83: only a real conflict goes to In Progress
                reason = '; '.join(bad)
                self.fail(p, reason, relay='merge-main', text=merge_main_text(n, reason))
            else:
                self.leave(p, '; '.join(why))
            return
        if self.dry:
            self.change(f"#{n} hr-gate --apply {n} (every condition passes)")
            return
        rc, out = self.tool([BIN / 'hr-gate', '--apply', str(n)])
        msg = [l.strip() for l in out.splitlines() if l.strip().startswith(('moved', 'refused', 'stayed', 'stays'))]
        self.change(f"#{n} hr-gate --apply: " + (msg[-1] if msg else f'rc={rc}'))
        moved = [m for m in msg if m.startswith('moved')]
        if moved:
            from . import moves
            to = 'Human Review' if 'Human Review' in moved[-1] else 'Staging'
            moves.log_move(n, p['col'] if p['col'] != board.STAGING else 'Staging', to, d['head'], note='hr-gate --apply')
        if rc == 0 or moved:
            drop_pull(n)

    @staticmethod
    def identity_problem(d, full):
        """Codex on PR #138: the cache may lag a push or a draft conversion, so the fast move
        needs the FRESH PR to be open, not a draft, and at the pulled head. Why not, or None."""
        if (full.get('state') or 'OPEN') != 'OPEN':
            return f"the PR is {full.get('state')}"
        if full.get('isDraft'):
            return 'the PR is a draft'
        if full.get('headRefOid') != d['head']:
            return f"the head is {(full.get('headRefOid') or '-')[:8]}, not the pulled {d['head'][:8]}"
        return None

    def evidence_problem(self, full):
        """Codex on PR #138: the Human Review conditions that Ready does NOT prove (the MinBot
        pass checks rows 1, 2, 3 and 5 only): 7a screenshots (every image an absolute https URL
        that renders, when the PR needs them), 7b the where-to-see line, and 8 no later human
        request; and the tangentswarm #130 hold (a body that states where the PR stands). Why
        one fails, or None. The other rows: 1 is identity_problem; 2 and 9 the
        pre-merge onto future main; 3, 4 and 5 the MinBot pass at this head (Ready); 6 the
        work health (work_health) and the served check in stage(). No hr-gate run; hr-gate's
        own 7a check when it is loaded."""
        from . import gatechecks
        from .brief import strip_brief
        body = strip_brief(full.get('body') or '')
        paths = [f.get('path') or '' for f in (full.get('files') or {}).get('nodes') or []]
        if gatechecks.needs_screenshots(paths, body):
            if not gatechecks.image_urls(body):
                return '7a: no image in body'
            if 'shots_exists' in self.g:
                ok, why = gatechecks.check_images(body, REPO, self.g['shots_exists'])
                if not ok:
                    return f'7a: {why}'
        if not gatechecks.WHERE_RE.search(body):
            return '7b: no where-to-see line in body'
        if gatechecks.status_lines(full.get('body') or ''):  # the full path opens the incident and gives the job
            return 'the body states where the PR stands (tangentswarm #130)'
        later = gatechecks.later_requests(full)
        if later is None:
            return '8: no head commit date in the PR read'
        if later:
            return f"8: {len(later)} later {gatechecks.HUMAN} {later[0][0]}(s) after the head"
        return None

    def work_health(self, full):
        """Codex on PR #138: condition 6's entry safeguard (gatechecks.work_problem: conflict
        markers in origin/work, or a failed latest work Redeploy run). A served .deploy-sha can
        contain the head while work is broken. Checked for a PR with served files only, as
        hr-gate does, with hr-gate's own work_broken (one answer per process). Why, or None."""
        from . import gatechecks
        paths = [f.get('path') or '' for f in (full.get('files') or {}).get('nodes') or []]
        if not any(gatechecks.SERVED_RE.search(p) for p in paths):
            return None
        if 'work_broken' not in self.g:
            return '6: no work health check loaded'
        try:
            bad = self.g['work_broken'](gatechecks)
        except Exception as e:
            return f"6: work health check failed: {type(e).__name__}: {str(e)[:120]}"
        return f"6: {bad}" if bad else None

    def unserved(self, d, full=None):
        """'' when the priority staging site serves pull d (or the PR changes no served file),
        else the sites that lag. File reads and one cached PR read (the changed paths)."""
        from . import advance, gatechecks
        files = ((full or self.g['fetch'](d['pr'])).get('files') or {}).get('nodes') or []
        if not any(gatechecks.SERVED_RE.search(f.get('path') or '') for f in files):
            return ''
        lag = advance.lagging(self.g, d, now=self.now)
        return '; '.join(f"{h} ({(x or '-')[:8]})" for _, h, x, _ in lag)

    def stage(self, d, p, served=False, full=None):
        """tangentswarm #136 (Michal, #116: "This is what ready means"): a Ready PR passed every
        Human Review check before it entered Ready, so Ready -> Staging is only the pre-merged
        tip in work (the pull pushed it) and the card move. No hr-gate run, no board read, no
        GitHub check beyond one PR read. advance() confirmed the card is in Ready at the pulled
        head and that its body has the evidence Ready does not prove (evidence_problem: 7a,
        7b; Codex on PR #138); the only check left is that the priority staging site serves it
        (served: the caller, such as
        Cascade.gate_pulls, already did; else unserved() does it here). The gate pass
        line for the head is written here, so the promote fast path can move it on."""
        from . import moves
        n, head = d['pr'], d['head']
        lag = '' if served else self.unserved(d, full)
        if lag:
            self.note(f"#{n} pull waiting: {head[:8]} is not served yet by {lag}")
            return
        if self.dry:
            self.change(f"#{n} Ready -> Staging (pre-merged tip in {WORK}; no gate re-run)")
            return
        board.move_card(self.g, p['item'], board.STAGING, n=n)
        self.g['log']('apply', n, head[:8], 'PASS', [])  # the gate pass the promote fast path reads
        moves.log_move(n, 'Ready', 'Staging', head, note='pre-merged Ready PR: no gate re-run (#136)')
        self.change(f"#{n} Ready -> Staging (pre-merged tip in {WORK}; no gate re-run)")
        drop_pull(n)

    def status_hold(self, n, full, head):
        """tangentswarm #130 (option A, Michal via MethBot on PR #133): a body that says where
        the PR stands (merged into work, on staging, the sha a site serves) holds hr-gate
        --apply, and an incident is opened. The swarm never edits the body. Returns (the wait
        reason, the worker's job text), or (None, None)."""
        from .gatechecks import check_status, status_lines, status_text
        body = (full or {}).get('body') or ''
        ok, why = check_status(n, body, REPO, head=head, dry=self.dry)
        if ok:
            return None, None
        return STATUS_ROW + why, status_text(n, status_lines(body))

    def screenshot_job(self, n, head, full=None, p=None):
        """A UI PR without screenshots: give its worker the screenshot job now, once per
        head (wake its running Claude, or resume its parked one in a rework slot)."""
        self.worker_jobs(n, head, [('shots', shots_text(n))], p)

    def worker_jobs(self, n, head, jobs, p=None):
        """Give PR n's worker its open jobs [(kind, text)] while its pull waits. Each kind is
        sent once per head. When one is new, ALL open jobs go in one relay, saved to
        relay-<n>-jobs.md (Codex on PR #133): the revive queue keeps one entry per PR and a
        queued worker start keeps its first brief path, so a message that held only the
        newest job would drop the others. The relay goes through to_worker: a Claude that
        cannot take it now gets a queued revive, and a gone worktree gets a worker start
        with the relay as its brief. A refusal never just repeats every pass."""
        from . import tick
        state = tick.State(tick.STATE_DIR / 'state.json')
        keys = [f"{kind}-job:{n}@{head}" for kind, _ in jobs]  # shots-job:N@H, status-job:N@H
        if all(state.done(k) for k in keys):
            return
        what = ' and '.join({'shots': 'screenshot job', 'status': 'status-text job'}.get(k, k) for k, _ in jobs)
        if self.dry:
            self.change(f"#{n} would give its worker the {what}")
            return
        text = '\n\n'.join(t for _, t in jobs)
        relay_path = RELAY_DIR / f"relay-{n}-jobs.md"
        try:
            relay_path.write_text(text + '\n')
        except OSError:
            pass
        waiting, said = self.to_worker(p or {'number': n}, text, relay_path)
        for k in keys:
            state.mark(k)
        self.change(f"#{n} {what} " + (said if waiting else f"sent to its worker ({said})"))

    def start_worker(self, p, relay_path, why):
        """The PR has no worktree or window to resume: queue a worker start through the
        launcher (the spool op 'start': swarm-start-scoped makes the worktree and window, and
        the new Claude is told to read the relay file). Within the same limits. Returns a note."""
        from . import slots, spool
        n = p['number']
        ok, held = slots.conflict_capacity()
        if not ok:
            return f"no worker to resume ({why}) and no start: {held}"
        if any(int(r.get('pr') or 0) == n for r in spool.in_flight(('start',))) or str(n) in slots.load()['unstarted']:
            return f"no worker to resume ({why}); its worker start is already queued"
        if not slots.start_later(n, pr_branch(p), relay_path):  # Codex on PR #109: never an empty branch
            return f"no worker to resume ({why}); its branch is unknown, so no start"
        spool.submit('start', '', pr=n)
        return f"no worker to resume ({why}); queued a worker start with the relay {relay_path.name}"

    def hand_off(self, p, text, relay_path):
        """(ok, note) for mainmerge.recheck: the relay reaches the PR's Claude now, or it
        waits for a Claude (to_worker). ok is True in both cases: the card goes to In
        Progress (tangentswarm #101). Codex on PR #26: never give_worker's rc."""
        waiting, said = self.to_worker(p, text, relay_path)
        return True, said

    def to_worker(self, p, text, relay_path):
        """(waiting, note): tangentswarm #101 (Michal 2026-11-17 8:49 AM ET: drop parking).
        Give a conflicting PR's Claude its relay now. If no Claude can take it now, the
        relay is saved and waits for a Claude, and the card still goes to In Progress:
          - no rework slot or memory (revive rc 3): the revive is queued with the text, and
            the cascade runs the queue in pick order when capacity frees (slots.run_queued);
          - no worktree or window to resume: an unstarted record with the relay file as its
            brief, and the cascade's start retry starts its worker (Advance.retry_starts);
          - any other refusal: the revive stays queued, and every pass tries it again
            (Codex on PR #109: a refusal never drops the saved feedback).
        The queued record keeps the branch and the relay path, so a later "worktree is
        gone" refusal turns it into a worker start (slots.run_queued).
        waiting is False when its Claude has the relay now."""
        from . import slots
        n = p['number']
        rc, said = self.give_worker(n, text, queue=True)
        if rc == 0:
            return False, said
        branch = pr_branch(p)
        if no_worker(said):
            if slots.start_later(n, branch, relay_path):  # the start, not a revive, gives its Claude the relay
                return True, f"waiting for a Claude: a worker start with the relay {relay_path.name} is queued ({said})"
            # Codex on PR #109: no branch, so no start can be recorded: keep the revive queued;
            # run_queued tries the start again once the branch can be read
        slots.queue_revive(n, text, why=said, branch=branch, relay=str(relay_path))
        if rc == 3:
            return True, f"waiting for a Claude: the revive is queued ({said})"
        return True, f"waiting for a Claude: the revive was refused ({said}); it stays queued and is retried"

    def give_worker(self, n, text, priority=False, queue=False):
        """(rc, last line) of slots.revive: 0 = its Claude runs and got the text. priority: a
        conflict fix for fill (tangentswarm #65): no rework-slot wait, only the hard limits.
        queue: a held revive (rc 3) is queued (slots.queue_revive)."""
        from . import slots
        lines = []
        kw = dict({'priority': True} if priority else {}, **({'queue': True} if queue else {}))
        try:
            rc = slots.revive(n, text, out=lines.append, **kw)
        except Exception as e:
            return 1, f"{type(e).__name__}: {str(e)[:160]}"
        return rc, (lines[-1] if lines else f'rc={rc}')

    def leave(self, p, reason, tell=True):
        """tangentswarm #83 (Michal 2026-11-10): a pulled PR leaves the Staging pull with no
        conflict. The pull is dropped and the card stays in Ready, at its place in the pick
        order; it never goes to In Progress. A card that is not in Ready any more (a new head
        moved it to Auto Review) is not moved. tell: give its worker the reason, once per head
        (a timeout reason holds its age, so the key is the head only), so a body or CI fix gets
        done; the card does not wait for it."""
        n, head, col = p['number'], p.get('head') or '', p.get('col')
        where = 'stays in Ready' if col == board.READY else f"is in {col or '?'}: not moved"
        if self.dry:
            self.change(f"#{n} would leave the Staging pull, no conflict; the card {where}: {reason[:200]}")
            return
        drop_pull(n)
        self.change(f"#{n} left the Staging pull, no conflict; the card {where}: {reason[:200]}")
        if not tell or col != board.READY:
            return
        from . import tick
        state = tick.State(tick.STATE_DIR / 'state.json')
        key = f"left-pull:{n}@{head}"
        if state.done(key):
            return
        text = (f"Coordinator: #{n} left the Staging pull: {reason}. There is no conflict, so the card stays "
                "in Ready at its place in the queue. If this needs a change in the PR (body, screenshots, CI), "
                "fix the root cause and push. If not, do nothing.")
        rc, said = self.give_worker(n, text)
        if rc == 0:
            state.mark(key)
            self.change(f"#{n} its worker has the reason ({said})")
        else:
            self.note(f"#{n} left the Staging pull; no worker took the reason ({said}); the card stays in Ready")

    def fail(self, p, reason, relay=None, text=None):
        """Send a pulled PR back to its worker. tangentswarm #83: only for a real conflict
        (a PR that does not merge onto main); any other way out of the pull is leave().
        tangentswarm #101 (Michal 2026-11-17 8:49 AM ET: drop parking; this replaces the Nov
        15 rule never to move a card into a column with no worker): the card always goes to
        In Progress. Its Claude gets the relay now, or the relay waits for a Claude
        (to_worker) and the move note says so. Nothing is held, so nothing behind it in Ready
        waits. Returns True when the card moved."""
        n = p['number']
        text = text or (f"Coordinator: #{n} left the Staging pull: {reason}. Fix the root cause, push, and "
                        "report the head commit and test counts.")
        if self.dry:
            self.change(f"#{n} -> In Progress (its worker gets the relay, or it waits for a Claude): {reason[:200]}")
            return False
        drop_pull(n)
        relay_path = RELAY_DIR / f"relay-{n}-{relay or 'gate'}.md"
        try:
            relay_path.write_text(text + '\n')
        except OSError:
            pass
        waiting, said = self.to_worker(p, text, relay_path)
        board.move_card(self.g, p['item'], board.IN_PROGRESS, n=n, repo=REPO)
        from . import moves
        moves.log_move(n, p.get('col') or '?', 'In Progress', p.get('head', ''),
                       note=(('waiting for a Claude: ' if waiting else '') + reason)[:120])
        if waiting:
            self.change(f"#{n} -> In Progress, WAITING_FOR_CLAUDE: {reason[:200]} ({said})")
        else:
            self.change(f"#{n} -> In Progress: {reason[:200]} (its worker has the relay: {said})")
        return True

    # -- the step ---------------------------------------------------------------
    def step(self, promote=True):
        """One step. promote=False skips step 1: the swarm-promoted daemon refills Human
        Review itself (cheap check), so it only needs the pull and its advance."""
        prs, cols = self.columns()
        hr, staging = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if len(hr) > HR_TARGET:
            self.note(f"Human Review holds {len(hr)} PRs (more than {HR_TARGET}): judgment needed")
        if len(staging) > STAGING_MAX:
            self.note(f"Staging holds {len(staging)} PRs (more than {STAGING_MAX})")
        if promote and self.promote(cols):
            prs, cols = self.columns() if not self.dry else (prs, cols)
        with self.g['ScratchRepo']() as scratch:
            d = load_pull()
            if d:
                self.advance(d, prs, scratch)
            running = d if self.dry else load_pull()
            if not running:
                self.pull(prs, cols, scratch)
        for line in self.did + self.notes:
            self.out(line)
        return 0

    def status(self):
        prs, cols = self.columns()
        for key in (board.HUMAN_REVIEW, board.STAGING, board.READY):
            ps = cols.get(key, [])
            label = 'Staging' if key == board.STAGING else key
            self.out(f"{label} {len(ps)}: " + (', '.join(f"#{p['number']}" for p in ps) or '-'))
        pulls = load_pulls()
        for d in pulls:
            self.out(f"pull: #{d['pr']} head {d['head'][:8]} in work {str(d.get('work'))[:8]} "
                     f"since {int((self.now() - d['started']) // 60)} min")
        if not pulls:
            self.out('pull: none')
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-flow', description=__doc__.split('\n', 1)[0])
    ap.add_argument('cmd', choices=['step', 'event', 'status', 'merge-work'])
    ap.add_argument('name', nargs='?', help='webhook event name (event); PR number (merge-work)')
    ap.add_argument('tip', nargs='?', help='merge-work: the commit to merge into work (a -vN tip)')
    ap.add_argument('--label', default='', help='merge-work: branch name for the message')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'status':
            return Flow().status()
        if a.cmd == 'merge-work':
            if not (a.name or '').isdigit() or not a.tip:
                ap.error('merge-work needs a PR number and a tip commit')
            if a.dry_run:
                return Flow(dry_run=True).merge_work(int(a.name), a.tip, a.label or 'tip')
            with gatelock.gate_lock(f'swarm-flow merge-work #{a.name}'):
                return Flow().merge_work(int(a.name), a.tip, a.label or 'tip')
        if a.cmd == 'event':
            payload = json.load(sys.stdin)
            if not event_wants_step(a.name or '', payload):
                print(f"skip: {a.name} {payload.get('action')} does not change the flow")
                return 0
        if a.dry_run:
            return Flow(dry_run=True).step()
        with gatelock.gate_lock('swarm-flow step'):
            return Flow().step()
    except gatelock.Busy as e:
        print(f"busy: {e}")
        return gatelock.EXIT_BUSY
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"swarm-flow error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
