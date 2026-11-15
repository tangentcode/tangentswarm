"""swarm-flow: the lean just-in-time Human Review flow (methodology#56), one step at a time.

Columns (matched by option id, see tangentswarm.board):
  Ready         finished PRs (Claude done, Codex clean on head, exact-head MinBot pass).
                They get no merge, no gate and no GitHub check. They wait in order:
                fast-track (methodology#60), priority milestone, fewest conflicts, oldest.
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
     `hr-gate --apply N` puts it into Staging. A hard failure sends it back to In
     Progress with the reason. Waiting reasons (CI running, screenshots or the
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
HR_TARGET = 2
STAGING_MIN = int(os.environ.get('SWARM_STAGING_MIN', '3'))
STAGING_MAX = int(os.environ.get('SWARM_STAGING_MAX', '5'))
PULL_TIMEOUT = int(os.environ.get('SWARM_PULL_TIMEOUT', str(90 * 60)))
WORK = 'work'
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


HELD = STATE.with_name('held.json')
RELAY_DIR = Path(os.environ.get('SWARM_RELAY_DIR', '/tmp'))  # relay-<N>-<kind>.md for swarm-resume


def shots_text(n):
    from .gatechecks import SHOTS_RULE
    return (f"Coordinator: #{n} needs screenshots before Human Review. Take them from staging (it serves "
            f"work with your head) or from a local run of the real app, never an isolated component page. "
            f"{SHOTS_RULE} Do not change code; push only the PR body edit.")


def held():
    """{PR number: {head, why}}: PRs whose failed pull found no worker (not pulled again at
    that head)."""
    try:
        return {int(k): v for k, v in json.loads(HELD.read_text()).items()}
    except (OSError, ValueError):
        return {}


def hold(n, head, why):
    d = held()
    d[n] = {'head': head, 'why': why[:200]}
    HELD.parent.mkdir(parents=True, exist_ok=True)
    HELD.write_text(json.dumps({str(k): v for k, v in d.items()}))


def pulled_pr():
    """PR number of the oldest pull in flight, or None."""
    d = load_pull()
    return d['pr'] if d else None


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


def ready_order(ready, conflicts):
    """Ready PRs in pull order: milestone rank, fewest conflicts, then oldest (entry time when
    known, else the lower PR number)."""
    return sorted(ready, key=lambda p: (p['rank'], conflicts.get(p['number'], 0), p.get('entered') or 0, p['number']))


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


def conflict_files(scratch, a, b):
    """The paths that conflict when b merges into a (git merge-tree --name-only; local)."""
    r = git(scratch, 'merge-tree', '--write-tree', '--name-only', '--no-messages', a, b, check=False)
    if r.returncode == 0:
        return []
    lines = [l.strip() for l in (r.stdout or '').splitlines()[1:] if l.strip()]
    return sorted(set(lines))


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
        self.tool_timeout = 1800  # one hr-gate / pending-promote run; the cascade sets less
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
        prs = tick.board_prs_open({'gql': self.g['gql'], 'PARKED': self.g.get('PARKED', set())})
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
        strict (the swarm-advance cascade): no rework to fill Staging. A Ready PR is pulled
        only if it merges cleanly with main, with work, and with each Human Review and Staging
        head (one pair at a time), and stacks cleanly on all of them. Any other PR is skipped
        with a note: it is not moved, and no relay is written."""
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
        work = self.fetch_work(scratch) if strict else None
        for p in ready_order(ready, conflicts):
            n = p['number']
            if strict:
                bad = [label for label, sha in [('main', main), (WORK, work)] + bases
                       if not self.clean(scratch, sha, p['head'])]
                if bad:
                    self.note(f"#{n} skipped: does not merge cleanly with {', '.join(bad)} (no rework to fill Staging)")
                    continue
            elif not self.clean(scratch, main, p['head']):
                self.fail(p, 'does not merge cleanly onto main: merge origin/main into the branch (no rebase)',
                          relay='merge-main')
                continue
            # A fast-track PR takes the FIRST Staging slot (methodology#60): it stacks on main +
            # Human Review only; the Staging PRs behind it are re-stacked at their own promotion.
            # Strict: no re-stack, so it must stack on every Staging PR too.
            mine = bases[:len(hr)] if p['rank'] == board.FAST_TRACK and not strict else bases
            bad = self.cmap.stack(main, mine, p['head'], lambda: stack_conflict(scratch, main, mine, p['head']))
            if bad:
                self.note(f"#{n} skipped this step: conflicts with {bad} (Ready order kept)")
                continue
            return p if self.start_pull(p, scratch) else None
        self.note('no Ready PR merges cleanly with main, work and every Human Review and Staging PR'
                  if strict else
                  'no Ready PR stacks cleanly on main + Human Review + Staging: one speculative rebase needed')

    def staging_order(self, staging):
        try:
            order = [r['pr'] for r in self.g['queue_from_board_or_file']()]
        except Exception:
            order = []
        pos = {n: i for i, n in enumerate(order)}
        return sorted(staging, key=lambda p: (pos.get(p['number'], len(pos)), p['number']))

    def fetch_work(self, scratch):
        """Fetch origin work into the scratch repo; return its commit."""
        git(scratch, 'fetch', '--no-tags', '--quiet', self.g['origin_url'](), f'{WORK}:refs/heads/{WORK}')
        return git(scratch, 'rev-parse', f'refs/heads/{WORK}').stdout.strip()

    def merge_into_work(self, scratch, n, tip, label):
        """Merge tip into origin work and push it (a fast-forward; never with --dry-run).
        Returns (work commit, None), (work commit, 'already in work') or (None, why). The merge is refused unless it is clean
        (merge_problem_text): a conflict never reaches work."""
        work = self.fetch_work(scratch)
        if git(scratch, 'merge-base', '--is-ancestor', tip, work, check=False).returncode == 0:
            return work, 'already in work'
        why = []
        merged = merge_commit(scratch, work, tip, f"Merge #{n} ({label}) into {WORK} for Staging\n\n"
                                                  f"swarm-flow, tip {tip}.", why=why)
        if merged is None:
            return None, f"{label} {tip[:8]} does not merge cleanly into {WORK} {work[:8]}: {why[0]}"
        if not self.dry:
            git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{merged}:refs/heads/{WORK}')
        return merged, None

    def start_pull(self, p, scratch):
        n, head = p['number'], p['head']
        merged, why = self.merge_into_work(scratch, n, head, p['branch'])
        if merged is None:
            self.note(f"#{n} refused, work NOT pushed: {why}")
            return False
        self.last_work = merged  # the work commit (also for a dry run, where it is not pushed)
        if not self.dry:
            save_pull(dict(pr=n, head=head, branch=p['branch'], item=p['item'], work=merged, started=self.now()))
        self.change(f"#{n} pulled: merged head {head[:8]} into {WORK} ({merged[:8]}); staging redeploys")
        return True

    def pull_batch(self, prs, cols, scratch, room):
        """Staging stocking (Michal 2026-11-15): pull EVERY clean Ready PR, up to room, in ONE
        work push, so one staging redeploy (one PTW request) covers all of them. A PR enters
        the batch only if it merges cleanly with main, work and each Human Review and Staging
        head, stacks on them, and merges cleanly onto the batch so far (one merge commit per
        PR on top of work). Nothing is reworked to fill Staging. Returns the pulled PRs."""
        try:
            return self._pull_batch(prs, cols, scratch, room)
        finally:
            self.cmap.save()

    def _pull_batch(self, prs, cols, scratch, room):
        staging, hr = cols.get(board.STAGING, []), cols.get(board.HUMAN_REVIEW, [])
        hd = held()
        ready = []
        for p in cols.get(board.READY, []):
            if p['draft']:
                continue
            if (hd.get(p['number']) or {}).get('head') == p['head']:
                self.note(f"#{p['number']} held: its last pull failed with no worker "
                          f"({hd[p['number']]['why'][:100]}); a new head releases it")
                continue
            ready.append(p)
        if not ready or room < 1:
            return []
        scratch.ensure_main()
        main = self.sha_of(scratch, scratch.main)
        bases = [(f"#{p['number']}", p['head']) for p in hr + self.staging_order(staging)]
        scratch.ensure(*[sha for _, sha in bases], *[p['head'] for p in ready])
        conflicts = {p['number']: sum(not self.clean(scratch, sha, p['head']) for sha in [main] + [s for _, s in bases])
                     for p in ready}
        work = self.fetch_work(scratch)
        tip, batch = work, []
        for p in ready_order(ready, conflicts):
            if len(batch) >= room:
                break
            n, head = p['number'], p['head']
            bad = [label for label, sha in [('main', main), (WORK, work)] + bases if not self.clean(scratch, sha, head)]
            if bad:
                self.note(f"#{n} skipped: does not merge cleanly with {', '.join(bad)} (no rework to fill Staging)")
                continue
            bad = self.cmap.stack(main, bases, head, lambda: stack_conflict(scratch, main, bases, head))
            if bad:
                self.note(f"#{n} skipped this step: conflicts with {bad} (Ready order kept)")
                continue
            if git(scratch, 'merge-base', '--is-ancestor', head, tip, check=False).returncode != 0:
                why = []
                merged = merge_commit(scratch, tip, head, f"Merge #{n} ({p['branch']}) into {WORK} for Staging\n\n"
                                                          f"swarm-flow batch, head {head}.", why=why)
                if merged is None:
                    self.note(f"#{n} skipped: does not merge cleanly onto the batch "
                              f"({', '.join('#' + str(q['number']) for q in batch)}): {why[0]}")
                    continue
                tip = merged
            batch.append(p)
        if not batch:
            self.note('no Ready PR merges cleanly with main, work and every Human Review and Staging PR')
            return []
        if tip != work and not self.dry:
            git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{tip}:refs/heads/{WORK}')
        self.last_work = tip
        if not self.dry:
            now = self.now()
            save_pulls(load_pulls() + [dict(pr=p['number'], head=p['head'], branch=p['branch'], item=p['item'],
                                            work=tip, started=now) for p in batch])
        names = ', '.join(f"#{p['number']} {p['head'][:8]}" for p in batch)
        self.change(f"{names} pulled: {len(batch)} head(s) merged into {WORK} ({tip[:8]}) in one push; "
                    "staging redeploys once")
        return batch

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
    def advance(self, d, prs, scratch):
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
        if p['head'] != d['head']:
            self.fail(p, f"head moved during the pull ({d['head'][:8]} -> {p['head'][:8]}): needs Codex and MinBot again")
            return
        full = self.g['fetch'](n, refresh=True)
        rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](refresh=True), scratch=scratch)
        verdict, why = gate_verdict(rows)
        age = int(self.now() - d['started'])
        shots = [w for w in why if w.startswith('7a')] if verdict == 'wait' else []
        if shots:  # Michal 2026-11-15: screenshots are a job for a worker now, not a timeout
            self.screenshot_job(n, d['head'], full)
        if verdict == 'wait' and age > PULL_TIMEOUT and len(shots) < len(why):
            verdict, why = 'fail', [f"still waiting after {age // 60} min: " + '; '.join(why)]
        elif verdict == 'wait' and age > PULL_TIMEOUT:
            self.note(f"SCREENSHOTS_NEEDED #{n}: still no screenshots after {age // 60} min; the pull stays "
                      "open (never demoted into a column with no worker)")
        if verdict == 'wait':
            self.note(f"#{n} pull waiting ({age // 60} min): " + '; '.join(w[:120] for w in why))
            return
        if verdict == 'fail':
            self.fail(p, '; '.join(why))
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

    def screenshot_job(self, n, head, full=None):
        """A UI PR without screenshots: give its worker the screenshot job now, once per
        head (wake its running Claude, or resume its parked one in a rework slot)."""
        from . import tick
        state = tick.State(tick.STATE_DIR / 'state.json')
        key = f"shots-job:{n}@{head}"
        if state.done(key):
            return
        text = shots_text(n)
        if self.dry:
            self.change(f"#{n} would give its worker the screenshot job")
            return
        rc, said = self.give_worker(n, text)
        if rc == 0:
            state.mark(key)
            self.change(f"#{n} screenshot job sent to its worker ({said})")
        else:
            self.note(f"SCREENSHOTS_NEEDED #{n}: no worker could take the screenshot job ({said})")

    def give_worker(self, n, text):
        """(rc, last line) of slots.revive: 0 = its Claude runs and got the text."""
        from . import slots
        lines = []
        try:
            rc = slots.revive(n, text, out=lines.append)
        except Exception as e:
            return 1, f"{type(e).__name__}: {str(e)[:160]}"
        return rc, (lines[-1] if lines else f'rc={rc}')

    def fail(self, p, reason, relay=None, text=None):
        """Send a pulled PR back to its worker. Michal 2026-11-15: never demote into a
        column with no worker. The card moves to In Progress only once its Claude runs and
        has the relay; else it stays where it is, its head is held from new pulls, and the
        note says NEEDS_WORKER."""
        n = p['number']
        text = text or (f"Coordinator: #{n} left the Staging pull: {reason}. Fix the root cause, push, and "
                        "report the head commit and test counts.")
        if self.dry:
            self.change(f"#{n} -> In Progress (with its worker): {reason[:200]}")
            return False
        drop_pull(n)
        try:
            (RELAY_DIR / f"relay-{n}-{relay or 'gate'}.md").write_text(text + '\n')
        except OSError:
            pass
        rc, said = self.give_worker(n, text)
        if rc != 0:
            hold(n, p.get('head', ''), reason)
            self.note(f"NEEDS_WORKER #{n}: {reason[:160]}; the card stays in {p.get('col') or '?'} because no "
                      f"worker could be started ({said}); its head is held from new pulls")
            return False
        self.g['move'](p['item'], board.IN_PROGRESS)
        from . import moves
        moves.log_move(n, p.get('col') or '?', 'In Progress', p.get('head', ''), note=reason[:120])
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
