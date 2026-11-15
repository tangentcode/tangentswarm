"""swarm-advance: ONE step from the end of the line, then exit. No LLM in the path.

The swarmbot loop ran flow-status, then decided by hand which card to move. This does
the first useful step, nearest Human Review first:

  1. promote   Human Review has a free slot and a Staging PR passes the cheap check
               (tangentswarm.promoted fast path: gate pass at this head, open, not a
               draft, not stacked, no later human comment, staging serves the head,
               clean merge with main and each Human Review head): move it.
  2. pull      a pull is running, or Staging is short and Ready has a PR: the swarm-flow
               pull step (one work merge, then hr-gate --apply on the pulled head, which
               checks the image URLs and the static .deploy-sha) or its advance.
  3. minbot    an Auto Review PR is Codex-clean on its exact head (receipt, no findings,
               no open Codex threads), not a draft, merges onto main and has green Linux
               CI (hr-gate conditions 1, 2, 3, 5): post the MinBot pass comment that
               names the head (hr-gate condition 4 reads it; the MinBot-passed label
               is retired), and move the card to Ready.

Nothing to do: exit 0 with one summary line.

    swarm-advance [--dry-run]
Exit 0 = a step ran or nothing to do, 75 = busy (queued: the swarm-promoted daemon runs it
under the lock), 2 = error.

    swarm-advance --cascade [--dry-run]
The merge or reject event runs this: the whole just-in-time cascade (see Cascade) in one
serial run under ~/.cache/swarm-serial.lock. A caller that finds the lock busy writes the
rerun flag (~/.cache/swarm-serial.rerun), queues `swarm-advance --cascade --if-flagged` for
the daemon, and exits 0. The holder runs the cascade again until no flag is left, then
releases the lock. Lines for swarmbot: START_CLAUDE n=K ... and CORNERBOT_REQUEST: ...
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import board, gatelock, proc

REPO = 'tangentcode/platform'
CODEX = 'chatgpt-codex-connector'
MINBOT = os.environ.get('SWARM_MINBOT', 'minbot')
MINBOT_ROWS = ('1 ', '2 ', '3 ', '5 ')  # not draft, mergeable, Codex clean, Linux CI

# --cascade (Michal 2026-11-14)
FLOOR = int(os.environ.get('SWARM_STAGING_FLOOR', '2'))  # pull while Staging has fewer (--floor; compare 1 and 2)
READY_TARGET = int(os.environ.get('SWARM_READY_TARGET', '6'))      # START_CLAUDE while Ready is below this
WORKING_TARGET = int(os.environ.get('SWARM_WORKING_TARGET', '2'))  # aim: about this many coders working
DEPLOY_WAIT = int(os.environ.get('SWARM_DEPLOY_WAIT', '600'))      # seconds to wait for staging to serve a pull
PTW_WAIT = int(os.environ.get('SWARM_PTW_WAIT', '1800'))           # seconds to wait for a cornerbot redeploy
DEPLOY_POLL = 15
BUDGET = int(os.environ.get('SWARM_CASCADE_BUDGET', '780'))   # whole run; the tick kills it at 900 s
STEP_TIMEOUT = int(os.environ.get('SWARM_CASCADE_STEP_TIMEOUT', '300'))  # one hr-gate --apply run
GATE_RESERVE = STEP_TIMEOUT + 30  # budget kept for the gate after a redeploy wait
EXIT_TIMEOUT = 124
DEADLINE = None  # time.monotonic() deadline of this process (watchdog)
CASCADE_LOG = Path(os.environ.get('SWARM_CASCADE_LOG', str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'cascade.log')))
CORNERBOT_FILE = Path(os.environ.get('SWARM_CORNERBOT_REQUEST', str(Path.home() / '.cache' / 'swarm-cornerbot-request')))
MAX_PASSES = int(os.environ.get('SWARM_CASCADE_MAX_PASSES', '10'))  # cascade passes per lock hold
RERUN = Path(os.environ.get('SWARM_CASCADE_RERUN', str(gatelock.LOCK.with_name('swarm-serial.rerun'))))
CORNERBOT_SITES = ('ptw',)  # staging for these sites redeploys only through a cornerbot request

CODEX_FIELDS = """number isDraft headRefOid mergeable
 comments(last:40){nodes{author{login} body}}
 reviews(last:30){nodes{author{login} commit{oid} comments{totalCount}}}
 reviewThreads(last:60){nodes{isResolved isOutdated comments(first:1){nodes{author{login}}}}}"""


def _codex(node):
    return ((node or {}).get('author') or {}).get('login', '').startswith(CODEX)


def codex_clean(pr):
    """hr-gate condition 3 on the exact head: a clean receipt (or review-state JSON) for the
    head, no Codex review with findings on the head, and no open Codex thread."""
    head = pr.get('headRefOid') or ''
    if not head:
        return False
    clean = False
    for c in (pr.get('comments') or {}).get('nodes') or []:
        if not _codex(c):
            continue
        body = c.get('body') or ''
        if 'Reviewed commit' in body and head[:8] in body and "Didn't find any major issues" in body:
            clean = True
        m = re.search(r'codex-review-state:v1\s*(\{.*?\})\s*-->', body, re.S)
        if m:
            try:
                st = json.loads(m.group(1)).get('code_review', {})
            except ValueError:
                continue
            if st.get('commit_sha') == head and not st.get('has_any_findings') \
                    and not st.get('has_unresolved_findings'):
                clean = True
    findings = [r for r in (pr.get('reviews') or {}).get('nodes') or []
                if _codex(r) and (r.get('commit') or {}).get('oid') == head
                and ((r.get('comments') or {}).get('totalCount') or 0) > 0]
    open_threads = [t for t in (pr.get('reviewThreads') or {}).get('nodes') or []
                    if not t.get('isResolved') and not t.get('isOutdated')
                    and _codex(((t.get('comments') or {}).get('nodes') or [{}])[0])]
    return clean and not findings and not open_threads


def minbot_order(prs, active=None):
    """Auto Review PRs to try: the active fast-track first, then the lowest number."""
    def key(p):
        ft = active is not None and (p['number'] == active or active in (p.get('closes') or ()))
        return (0 if ft else 1, p['number'])
    return sorted(prs, key=key)


def pass_text(head):
    return (f"Exact-head pass on {head[:10]} (swarm-advance, mechanical): not a draft; merges cleanly "
            "onto main; Codex clean on this head with no open Codex threads; Linux CI green.")


class Advance:
    def __init__(self, g=None, dry_run=False, out=print, minbot=None):
        import runpy
        from .promoted import BIN
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_advance')
        self.dry, self.out = dry_run, out
        self.minbot = minbot or self._minbot
        self.why = []  # why each step had nothing to do, for the summary line
        self.moved = []  # PRs the promote step moved (or would move) to Human Review

    def _minbot(self, *args):
        argv = [MINBOT, *(['--apply'] if not self.dry else []), *map(str, args)]
        r = proc.sh(*argv, check=False, who='swarm-advance')
        if r.returncode:
            raise RuntimeError(f"minbot {' '.join(map(str, args[:2]))} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    # 1 ---------------------------------------------------------------------
    def promote(self, cols):
        from . import promoted
        hr, st = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if not promoted.needs_promotion(cols):
            self.why.append(f"Human Review {len(hr)}/{promoted.HR_TARGET}"
                            + ('' if len(hr) >= promoted.HR_TARGET else ', Staging empty'))
            return False
        lines = []
        p = promoted.Promoter(g=self.g, dry_run=self.dry)
        old, promoted.say = promoted.say, lines.append
        try:
            moved = p.fast_path(cols)
        finally:
            promoted.say = old
        self.moved = list(moved or [])
        if moved:
            for line in lines:
                if 'Human Review' in line and ('moved' in line or 'would move' in line):
                    self.out(line)
            return True
        self.why.append(f"Human Review {len(hr)}/{promoted.HR_TARGET} but no Staging PR passes the cheap check ("
                        + '; '.join(f"#{n}: {w}" for n, _, w in p.left)[:300] + ')')
        return False

    # 2 ---------------------------------------------------------------------
    def pull(self, cols):
        from . import flow
        st, ready = cols.get(board.STAGING, []), cols.get(board.READY, [])
        running = flow.load_pull()
        if not running and (len(st) >= flow.STAGING_MIN or not ready):
            self.why.append(f"Staging {len(st)}/{flow.STAGING_MIN}, Ready {len(ready)}")
            return False
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        f.step(promote=False)
        for line in f.did:
            self.out('pull: ' + line)
        if f.did:
            return True
        self.why.append('pull: ' + ('; '.join(f.notes)[:300] or 'nothing moved'))
        return False

    # 3 ---------------------------------------------------------------------
    def minbot_pass(self, cols):
        ar = cols.get(board.AUTO_REVIEW, [])
        if not ar:
            self.why.append('Auto Review empty')
            return False
        owner, name = REPO.split('/')
        parts = ' '.join(f'p{p["number"]}: pullRequest(number:{int(p["number"])}){{{CODEX_FIELDS}}}' for p in ar)
        data = self.g['gql'](f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
        details = {int(k[1:]): v for k, v in data.items() if v}
        skipped = []
        for p in minbot_order(ar, board.active_fast_track()):
            n, d = p['number'], details.get(p['number']) or {}
            if d.get('headRefOid') != p['head']:
                skipped.append(f"#{n} head moved")
                continue
            if d.get('isDraft'):
                skipped.append(f"#{n} draft")
                continue
            if d.get('mergeable') == 'CONFLICTING':  # cheap: no full hr-gate read for it
                skipped.append(f"#{n} conflicts with main (GitHub mergeable)")
                continue
            if not codex_clean(d):
                skipped.append(f"#{n} not Codex-clean on {p['head'][:8]}")
                continue
            why = self.full_check(n)
            if why:
                skipped.append(f"#{n} {why}")
                continue
            self.apply_pass(p)
            return True
        self.why.append('Auto Review: ' + ', '.join(skipped))
        return False

    def full_check(self, n):
        """None if hr-gate conditions 1, 2, 3 and 5 pass on the head, else the failed rows."""
        full = self.g['fetch'](n, refresh=True)
        with self.g['ScratchRepo']() as scratch:
            rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](), scratch=scratch)
        bad = [f"{name.strip()}: {why[:80]}" for name, ok, why in rows if name.startswith(MINBOT_ROWS) and not ok]
        return '; '.join(bad) if bad else None

    def apply_pass(self, p):
        n, head = p['number'], p['head']
        self.minbot('evidence', n, '--repo', REPO, '--title', 'MinBot pass', '--commit', head,
                    '--body', pass_text(head))
        if not self.dry:
            self.g['move'](p['item'], board.READY)
            from . import moves
            moves.log_move(n, 'Auto Review', 'Ready', head, note='swarm-advance MinBot pass')
        self.out(f"{'would ' if self.dry else ''}MinBot-pass #{n} at {head[:8]}: pass comment, "
                 "Auto Review -> Ready")

    # the step ----------------------------------------------------------------
    def step(self):
        from .promoted import read_columns
        cols = read_columns(self.g['gql'])
        for fn in (self.promote, self.pull, self.minbot_pass):
            if fn(cols):
                return 0
        self.out('nothing to do: ' + '; '.join(self.why))
        return 0


class Cascade(Advance):
    """swarm-advance --cascade: the whole just-in-time cascade in one serial run.

      1. promote   Human Review has a free slot: the fast path moves the next Staging PR.
      2. fill      (Michal 2026-11-14 7:16 PM ET) no fixed cap. Any Ready PR that merges
                   cleanly (git merge-tree) with main, work and every Human Review and
                   Staging PR may enter Staging (Flow.pull strict). A pull happens only
                   while Staging has fewer than FLOOR PRs (default 2; --floor or
                   SWARM_STAGING_FLOOR, to compare 1 and 2). Nothing is rebased, reworked
                   or sent back just to fill Staging. The expensive steps run only at pull
                   time: work merge (refused on a conflict or a marker), staging redeploy
                   (ptw: CORNERBOT_REQUEST, also in CORNERBOT_FILE), a poll until the
                   .deploy-sha of EVERY staging site contains it, then hr-gate --apply.
                   A pull that still waits after the timeout (or for CI) stops the fill;
                   the next cascade continues it. Each move goes to the move log
                   (tangentswarm.moves).
      3. signal    Ready has fewer than READY_TARGET PRs: print one START_CLAUDE line for
                   swarmbot, so that about WORKING_TARGET coders work.
    """

    def __init__(self, g=None, dry_run=False, out=print, sleep=time.sleep, clock=time.monotonic, **kw):
        super().__init__(g=g, dry_run=dry_run, out=out, **kw)
        from . import flow
        self.sleep, self.clock = sleep, clock
        self.f = flow.Flow(dry_run=dry_run, out=lambda line: None, g=self.g)
        self.f.tool_timeout = STEP_TIMEOUT
        self._seen = [0, 0]
        # one deadline per PROCESS (watchdog sets it): a rerun pass must not start a fresh budget
        self.deadline = DEADLINE if DEADLINE is not None and clock is time.monotonic else self.clock() + BUDGET

    def flush(self):
        """Print the Flow lines (did/would, then notes) not printed yet."""
        for i, (tag, lines) in enumerate((('pull: ', self.f.did), ('note: ', self.f.notes))):
            for line in lines[self._seen[i]:]:
                self.out(tag + line)
            self._seen[i] = len(lines)

    # -- served check -----------------------------------------------------------
    def touches_served(self, n):
        """True if PR n changes served files (web/, app/, api/): every staging site serves them."""
        from . import gatechecks
        full = self.g['fetch'](n)
        return any(gatechecks.SERVED_RE.search(f.get('path') or '')
                   for f in ((full.get('files') or {}).get('nodes') or []))

    def cornerbot_request(self, n, branch, head, work, host):
        line = (f"CORNERBOT_REQUEST{' (dry run)' if self.dry else ''}: redeploy {host} from platform work "
                f"{work[:8] if work else '(after the merge)'} (#{n} {branch} {head[:8]} is in work)")
        if not self.dry:  # for swarmbot while this run still waits (stdout may be read only at exit)
            try:
                CORNERBOT_FILE.parent.mkdir(parents=True, exist_ok=True)
                CORNERBOT_FILE.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
            except OSError as e:
                self.out(f"cornerbot request file not written: {e}")
        return line

    def lagging(self, d):
        """[(site key, host, static sha, err)] for each staging site that does not serve the
        pulled head yet. A site serves it when its .deploy-sha is the pull's work commit or
        contains the head (platform #493: ptw lagged while the other two served it)."""
        if isinstance(self.g.get('_fetched'), list):
            self.g['_fetched'].clear()  # fetch work again: a served commit may be new
        return [(k, host, sha, err) for k, host, sha, err in self.g['served_all']()
                if not (sha and (sha == d.get('work') or self.g['contains'](d['head'], sha)))]

    def served(self, d):
        """True once EVERY staging site serves the pulled head. Polls each .deploy-sha up to
        DEPLOY_WAIT s, or PTW_WAIT s while a cornerbot site (ptw) lags; that site gets one
        CORNERBOT_REQUEST per pull."""
        from . import flow
        start, asked, said = self.clock(), bool(d.get('asked')), self.clock()
        self.out(f"redeploy: polling every staging .deploy-sha for #{d['pr']} {d['head'][:8]}"
                 + (" (cornerbot was asked before: one check, no wait)" if asked else ''))
        while True:
            lag = self.lagging(d)
            if not lag:
                self.out(f"served: every staging site serves #{d['pr']} {d['head'][:8]}")
                return True
            corner = [(k, h) for k, h, _, _ in lag if k in CORNERBOT_SITES]
            if corner and not asked:
                for _, host in corner:
                    self.out(self.cornerbot_request(d['pr'], d.get('branch', ''), d['head'], d.get('work'), host))
                asked = True
                if not self.dry:
                    flow.save_pull(dict(d, asked=True))
            # the wait limit, cut so the gate still fits in the run budget. A cornerbot site that
            # was asked in an EARLIER run or pass gets one check only: waiting again would hold the
            # serial lock for nothing (7:32 PM ET: #480 held it pass after pass).
            limit = min(PTW_WAIT if corner else DEPLOY_WAIT, self.deadline - start - GATE_RESERVE)
            if corner and d.get('asked'):
                limit = 0
            waited = self.clock() - start
            if waited >= limit:
                self.out(f"waiting: #{d['pr']} {d['head'][:8]} is not served yet by "
                         + '; '.join(f"{h} ({s[:8] if s else (e or 'empty')[:60]})" for _, h, s, e in lag)
                         + f" after {int(waited)}s; the pull stays open for the next cascade")
                return False
            if self.clock() - said >= 60:
                said = self.clock()
                self.out(f"redeploy: still waiting for {', '.join(h for _, h, _, _ in lag)} "
                         f"({int(waited)}s of {int(limit)}s)")
            self.sleep(DEPLOY_POLL)

    def finish(self, d, prs, scratch):
        """Bring the running pull d into Staging: wait until every staging site serves it, then
        hr-gate --apply. True when the pull ended (moved or failed), False when it still waits
        (redeploy timeout, CI): the fill stops there and the next cascade continues it."""
        from . import flow
        if self.touches_served(d['pr']) and not self.served(d):
            return False
        before = len(self.f.did)
        self.out(f"gate: hr-gate on #{d['pr']} {d['head'][:8]} (timeout {STEP_TIMEOUT}s)")
        self.f.advance(d, prs, scratch)
        self.flush()
        if self.dry:  # nothing is saved: a would-line means the pull ends, a note means it waits
            return len(self.f.did) > before
        return flow.load_pull() is None

    # -- 2. fill ------------------------------------------------------------------
    def fill(self):
        from . import flow
        prs, cols = self.f.columns()
        if self.dry and self.moved:  # the promote step only said "would move"
            up = [p for p in cols.get(board.STAGING, []) if p['number'] in self.moved]
            cols[board.STAGING] = [p for p in cols.get(board.STAGING, []) if p not in up]
            cols[board.HUMAN_REVIEW] = cols.get(board.HUMAN_REVIEW, []) + up
        with self.g['ScratchRepo']() as scratch:
            ended = None  # dry run: the running pull that would end (pull.json is not changed)
            for _ in range(len(cols.get(board.READY, [])) + 2):  # each turn ends the pull, adds one PR, or stops
                d = flow.load_pull()
                if d and d.get('pr') != ended:
                    if not self.finish(d, prs, scratch):
                        return cols
                    if self.dry:  # count it where the live run would see it: out of Ready, into Staging
                        ended = d.get('pr')
                        if any(f"#{ended} hr-gate --apply" in line for line in self.f.did):
                            done = [q for q in cols.get(board.READY, []) if q['number'] == ended]
                            cols = dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q not in done],
                                                 board.STAGING: cols.get(board.STAGING, []) + done})
                    else:
                        prs, cols = self.f.columns()
                    continue
                st = cols.get(board.STAGING, [])
                ready = [p for p in cols.get(board.READY, []) if not p.get('draft')]
                if not ready:
                    self.out(f"fill: Staging {len(st)}, Ready 0: Ready is empty")
                    return cols
                if len(st) >= FLOOR:
                    self.out(f"fill: Staging {len(st)}, floor {FLOOR}: no pull needed")
                    return cols
                p = self.f.pull(prs, cols, scratch, limit=FLOOR, strict=True)
                self.flush()
                if p is None:
                    self.out(f"fill: Staging {len(st)}: no {'extra ' if st else ''}Ready PR to pull without rework")
                    return cols
                self.out(f"fill: #{p['number']} is the {'extra' if st else 'first'} Staging pull")
                if self.dry:  # nothing was pushed: show the rest of the chain, then count it in Staging
                    if self.touches_served(p['number']):
                        d = dict(pr=p['number'], head=p['head'], work=getattr(self.f, 'last_work', None))
                        lag = self.lagging(d)
                        for k, host, _, _ in lag:
                            if k in CORNERBOT_SITES:
                                self.out(self.cornerbot_request(p['number'], p.get('branch', ''), p['head'],
                                                                d['work'], host))
                        corner = any(k in CORNERBOT_SITES for k, *_ in lag)
                        self.out(f"would wait up to {PTW_WAIT if corner else DEPLOY_WAIT}s until every staging "
                                 f"site serves {p['head'][:8]}"
                                 + (f" (now lagging: {', '.join(h for _, h, _, _ in lag)})" if lag else ''))
                    self.out(f"would run hr-gate --apply {p['number']} (Ready -> Staging when it passes)")
                    cols = dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q is not p],
                                         board.STAGING: st + [p]})
                    continue
                if not self.finish(flow.load_pull(), prs, scratch):
                    return cols
                prs, cols = self.f.columns()
        return cols

    # -- 3. signal ----------------------------------------------------------------
    def signal(self, cols):
        ready = len([p for p in cols.get(board.READY, []) if not p.get('draft')])
        working = len(cols.get(board.IN_PROGRESS, []))
        if ready >= READY_TARGET:
            self.out(f"signal: Ready {ready}/{READY_TARGET}: no new coder")
            return
        start = max(0, WORKING_TARGET - working)
        if start:
            self.out(f"START_CLAUDE n={start} ready={ready}/{READY_TARGET} working={working}/{WORKING_TARGET}")
        else:
            self.out(f"signal: Ready {ready}/{READY_TARGET}, {working} PR(s) In Progress "
                     f"(aim {WORKING_TARGET}): no START_CLAUDE")

    def run(self):
        from .promoted import read_columns
        t0 = self.clock()
        self.out(f"cascade: start{' (dry run)' if self.dry else ''}; budget {BUDGET}s")
        self.out("promote: reading the board")
        if not self.promote(read_columns(self.g['gql'])):
            self.out('promote: ' + self.why[-1])
        self.out("fill: reading the board")
        self.signal(self.fill())
        self.out(f"cascade: done in {int(self.clock() - t0)}s")
        return 0


def queue_cascade(qdir=None):
    """Queue `swarm-advance --cascade --if-flagged` for the swarm-promoted daemon. A busy
    caller does this too: the lock holder may be hr-gate or pending-promote, which never
    read the rerun flag."""
    from .promoted import BIN
    try:
        gatelock.enqueue([str(BIN / 'swarm-advance'), '--cascade', '--if-flagged'],
                         by=f"pid {os.getpid()}", qdir=qdir)
        return 'queued for swarm-promoted'
    except (OSError, ValueError) as e:
        return f'NOT queued ({e})'


def flag_rerun(flag=None, by=''):
    flag = flag or RERUN
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(f"{by or 'pid ' + str(os.getpid())} {time.time():.0f}\n")


def run_locked(body, flag=None, lock=None, out=print, if_flagged=False, max_passes=None,
               env=os.environ, enqueue=queue_cascade):
    """Run body() under the serial lock until no rerun flag is left.

    A caller first writes the flag, then tries the lock. Busy: it exits 0 and the holder
    runs the cascade again. The holder removes the flag before each pass, so a flag
    written during a pass gives one more pass. After it releases the lock it looks again:
    a flag written between its last look and the release makes it take the lock again
    (busy then means a newer caller holds it and will see the flag)."""
    flag = flag or RERUN
    max_passes = MAX_PASSES if max_passes is None else max_passes
    if if_flagged:
        if not flag.exists():
            out('no rerun flag: nothing to do')
            return 0
    else:
        flag_rerun(flag)
    passes = 0
    while flag.exists():
        try:
            with gatelock.gate_lock('swarm-advance --cascade', lock=lock, env=env):
                while flag.exists():
                    if passes >= max_passes:
                        out(f"stopped after {passes} cascade passes; the rerun flag stays for the next run")
                        return 0
                    flag.unlink(missing_ok=True)
                    passes += 1
                    if passes > 1:
                        out(f"rerun flag found: cascade pass {passes}")
                    try:
                        body()
                    except BaseException as e:  # timeout, SIGTERM, error: keep the rerun; the with releases the lock
                        flag_rerun(flag, by='kept after ' + type(e).__name__)
                        out(f"stopped: {type(e).__name__}: {str(e)[:200]}; lock released, rerun flag kept")
                        if isinstance(e, CascadeTimeout):
                            return EXIT_TIMEOUT
                        raise
        except gatelock.Busy as e:
            out(f"busy: {e}; rerun flag set, the lock holder runs the cascade again; {enqueue()}")
            return 0
    return 0


class CascadeTimeout(Exception):
    """The run budget ran out (SIGALRM) or the caller stopped us (SIGTERM)."""


def watchdog(budget=None):
    """SIGALRM after the budget and SIGTERM both raise CascadeTimeout in the main thread, so
    run_locked keeps the rerun flag and the lock is released (subprocess.run kills its child)."""
    import signal

    def stop(signum, frame):
        raise CascadeTimeout('run budget used up' if signum == signal.SIGALRM else f'signal {signum}')
    global DEADLINE
    budget = BUDGET if budget is None else budget
    signal.signal(signal.SIGALRM, stop)
    signal.signal(signal.SIGTERM, stop)
    DEADLINE = time.monotonic() + budget
    signal.alarm(budget)


def queue_self(qdir=None):
    """Queue `swarm-advance` (the ~/bin wrapper) for the daemon. Returns a note."""
    from .promoted import BIN
    try:
        gatelock.enqueue([str(BIN / 'swarm-advance')], by=f"pid {os.getpid()}", qdir=qdir)
        return 'queued for swarm-promoted'
    except (OSError, ValueError) as e:
        return f'NOT queued ({e})'


def _say(line):
    """A progress line: ET time stamp, printed at once, and kept in CASCADE_LOG (a caller that
    pipes us through tail sees nothing until we exit)."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo('America/New_York'))
    except Exception:
        now = datetime.now()
    line = f"{now.strftime('%H:%M:%S')} {line}"
    print(line, flush=True)
    try:
        CASCADE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CASCADE_LOG, 'a') as f:
            f.write(f"{now.strftime('%Y-%m-%d')} {line}\n")
    except OSError:
        pass


def main(argv=None):
    global FLOOR
    ap = argparse.ArgumentParser(prog='swarm-advance', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--dry-run', action='store_true', help='print what it would do; change nothing')
    ap.add_argument('--cascade', action='store_true',
                    help='the whole cascade (promote, keep one ready PR in Staging, START_CLAUDE) under the lock')
    ap.add_argument('--if-flagged', action='store_true', help='--cascade: run only if the rerun flag is set')
    ap.add_argument('--floor', type=int, help=f'--cascade: Staging floor (default {FLOOR}, env SWARM_STAGING_FLOOR)')
    a = ap.parse_args(argv)
    if a.floor is not None:
        FLOOR = a.floor
        os.environ['SWARM_STAGING_FLOOR'] = str(a.floor)  # the move log records it
    try:
        if a.cascade:
            if a.dry_run:  # no lock, no flag: a dry run changes nothing
                watchdog()
                return Cascade(dry_run=True, out=_say).run()
            watchdog()
            return run_locked(lambda: Cascade(out=_say).run(), if_flagged=a.if_flagged, out=_say)
        if a.dry_run:
            return Advance(dry_run=True).step()
        with gatelock.gate_lock('swarm-advance'):
            return Advance().step()
    except gatelock.Busy as e:
        # No "busy, give up": the swarm-promoted daemon owns the queue and runs it under the lock.
        print(f"busy: {e}; " + queue_self())
        return gatelock.EXIT_BUSY
    except CascadeTimeout as e:  # a dry run (no lock, no flag)
        _say(f"stopped: {e}")
        return EXIT_TIMEOUT
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"swarm-advance error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
