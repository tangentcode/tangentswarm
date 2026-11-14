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
               CI (hr-gate conditions 1, 2, 3, 5): add MinBot-passed, post the pass
               comment, and move the card to Ready.

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
STAGING_LIMIT = int(os.environ.get('SWARM_CASCADE_STAGING', '5'))  # pull while Staging is below this
READY_TARGET = int(os.environ.get('SWARM_READY_TARGET', '6'))      # START_CLAUDE while Ready is below this
WORKING_TARGET = int(os.environ.get('SWARM_WORKING_TARGET', '2'))  # aim: about this many coders working
DEPLOY_WAIT = int(os.environ.get('SWARM_DEPLOY_WAIT', '600'))      # seconds to wait for staging to serve a pull
DEPLOY_POLL = 15
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
        self.minbot('label', 'add', n, 'MinBot-passed', '--repo', REPO)
        self.minbot('evidence', n, '--repo', REPO, '--title', 'MinBot pass', '--commit', head,
                    '--body', pass_text(head))
        if not self.dry:
            self.g['move'](p['item'], board.READY)
        self.out(f"{'would ' if self.dry else ''}MinBot-pass #{n} at {head[:8]}: label, pass comment, "
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
      2. fill      while Staging has fewer than STAGING_LIMIT PRs and Ready is not empty:
                   pull the next Ready PR (milestone, fewest conflicts, oldest), merge it into
                   work (refused on a conflict or a conflict marker), let staging redeploy
                   (ptw: print the cornerbot request), wait until the served .deploy-sha
                   contains it, then hr-gate --apply. A pull that must wait (ptw redeploy,
                   CI still running) stops the fill; the next cascade continues it.
      3. signal    Ready has fewer than READY_TARGET PRs: print one START_CLAUDE line for
                   swarmbot, so that about WORKING_TARGET coders work.
    """

    def __init__(self, g=None, dry_run=False, out=print, sleep=time.sleep, clock=time.monotonic, **kw):
        super().__init__(g=g, dry_run=dry_run, out=out, **kw)
        from . import flow
        self.sleep, self.clock = sleep, clock
        self.f = flow.Flow(dry_run=dry_run, out=lambda line: None, g=self.g)
        self._seen = [0, 0]

    def flush(self):
        """Print the Flow lines (did/would, then notes) not printed yet."""
        for i, (tag, lines) in enumerate((('pull: ', self.f.did), ('note: ', self.f.notes))):
            for line in lines[self._seen[i]:]:
                self.out(tag + line)
            self._seen[i] = len(lines)

    # -- served check -----------------------------------------------------------
    def site(self, n):
        """(site key, served paths?) for PR n."""
        full = self.g['fetch'](n)
        paths = [f['path'] for f in ((full.get('files') or {}).get('nodes') or []) if f.get('path')]
        from . import gatechecks
        return self.g['site_for'](full, None), any(gatechecks.SERVED_RE.search(x) for x in paths)

    def cornerbot_request(self, n, branch, head, work, site):
        host = self.g['SITES'][site][0] if 'SITES' in self.g else site
        return (f"CORNERBOT_REQUEST{' (dry run)' if self.dry else ''}: redeploy {host} from platform work "
                f"{work[:8] if work else '(after the merge)'} (#{n} {branch} {head[:8]} is in work)")

    def served(self, d, site, wait):
        """True once the static .deploy-sha of site contains the pulled head (polls up to wait s)."""
        end = self.clock() + wait
        while True:
            if isinstance(self.g.get('_fetched'), list):
                self.g['_fetched'].clear()  # fetch work again: the served commit may be new
            host, sha, _, _, err = self.g['served'](site)
            if sha and (sha == d.get('work') or self.g['contains'](d['head'], sha)):
                self.out(f"served: {host} .deploy-sha {sha[:8]} contains #{d['pr']} {d['head'][:8]}")
                return True
            if self.clock() >= end:
                self.out(f"waiting: {host} .deploy-sha {sha[:8] if sha else (err or 'empty')[:60]} does not contain "
                         f"#{d['pr']} {d['head'][:8]} yet")
                return False
            self.sleep(DEPLOY_POLL)

    def finish(self, d, prs, scratch):
        """Bring the running pull d into Staging. True when the pull ended (moved or failed),
        False when it still waits (redeploy, CI): the fill stops there."""
        from . import flow
        site, has_served = self.site(d['pr'])
        if has_served:
            if site in CORNERBOT_SITES:
                if not d.get('asked'):
                    self.out(self.cornerbot_request(d['pr'], d.get('branch', ''), d['head'], d.get('work'), site))
                    if not self.dry:
                        flow.save_pull(dict(d, asked=True))
                ok = self.served(d, site, 0)
            else:
                ok = self.served(d, site, DEPLOY_WAIT)
            if not ok:
                return False
        before = len(self.f.did)
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
            for _ in range(STAGING_LIMIT + 2):  # each turn ends the running pull, adds one PR, or stops
                d = flow.load_pull()
                if d and d.get('pr') != ended:
                    if not self.finish(d, prs, scratch):
                        return cols
                    if self.dry:
                        ended = d.get('pr')
                    else:
                        prs, cols = self.f.columns()
                    continue
                st = cols.get(board.STAGING, [])
                ready = [p for p in cols.get(board.READY, []) if not p.get('draft')]
                if len(st) >= STAGING_LIMIT or not ready:
                    self.out(f"fill: Staging {len(st)}/{STAGING_LIMIT}, Ready {len(ready)}: "
                             + ('Staging is full' if ready else 'Ready is empty'))
                    return cols
                p = self.f.pull(prs, cols, scratch, limit=STAGING_LIMIT)
                self.flush()
                if p is None:
                    return cols
                if self.dry:  # nothing was pushed: show the rest of the chain, then count it in Staging
                    site, has_served = self.site(p['number'])
                    if has_served and site in CORNERBOT_SITES:
                        self.out(self.cornerbot_request(p['number'], p.get('branch', ''), p['head'],
                                                        getattr(self.f, 'last_work', None), site))
                        self.out(f"would stop the fill here: #{p['number']} waits for the cornerbot redeploy; "
                                 "the next cascade gates it")
                        return dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q is not p]})
                    elif has_served:
                        self.out(f"would wait up to {DEPLOY_WAIT}s for the {site} .deploy-sha to contain "
                                 f"{p['head'][:8]}")
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
        if not self.promote(read_columns(self.g['gql'])):
            self.out('promote: ' + self.why[-1])
        self.signal(self.fill())
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
                    body()
        except gatelock.Busy as e:
            out(f"busy: {e}; rerun flag set, the lock holder runs the cascade again; {enqueue()}")
            return 0
    return 0


def queue_self(qdir=None):
    """Queue `swarm-advance` (the ~/bin wrapper) for the daemon. Returns a note."""
    from .promoted import BIN
    try:
        gatelock.enqueue([str(BIN / 'swarm-advance')], by=f"pid {os.getpid()}", qdir=qdir)
        return 'queued for swarm-promoted'
    except (OSError, ValueError) as e:
        return f'NOT queued ({e})'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-advance', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--dry-run', action='store_true', help='print what it would do; change nothing')
    ap.add_argument('--cascade', action='store_true',
                    help='the whole cascade (promote, fill Staging, START_CLAUDE) under the lock, with rerun flag')
    ap.add_argument('--if-flagged', action='store_true', help='--cascade: run only if the rerun flag is set')
    a = ap.parse_args(argv)
    try:
        if a.cascade:
            if a.dry_run:  # no lock, no flag: a dry run changes nothing
                return Cascade(dry_run=True).run()
            return run_locked(lambda: Cascade().run(), if_flagged=a.if_flagged)
        if a.dry_run:
            return Advance(dry_run=True).step()
        with gatelock.gate_lock('swarm-advance'):
            return Advance().step()
    except gatelock.Busy as e:
        # No "busy, give up": the swarm-promoted daemon owns the queue and runs it under the lock.
        print(f"busy: {e}; " + queue_self())
        return gatelock.EXIT_BUSY
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"swarm-advance error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
