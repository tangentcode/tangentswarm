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
releases the lock. Lines for swarmbot: START_CLAUDE n=K issues=#A,#B ... and CORNERBOT_REQUEST: ...
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import board, gatelock, gqlmeter, proc

REPO = 'tangentcode/platform'
CODEX = 'chatgpt-codex-connector'
MINBOT = os.environ.get('SWARM_MINBOT', 'minbot')
MINBOT_ROWS = ('1 ', '2 ', '3 ', '5 ')  # not draft, mergeable, Codex clean, Linux CI

# --cascade (Michal 2026-11-14)
FLOOR = int(os.environ.get('SWARM_STAGING_FLOOR', '2'))  # pull while Staging has fewer (--floor; compare 1 and 2)
READY_TARGET = int(os.environ.get("SWARM_READY_TARGET", "6"))      # Ready short: one more new-work slot (slots.NEW_SLOTS_SHORT)
BUDGET = int(os.environ.get('SWARM_CASCADE_BUDGET', '780'))   # whole run; the tick kills it at 900 s
STEP_TIMEOUT = int(os.environ.get('SWARM_CASCADE_STEP_TIMEOUT', '300'))  # one hr-gate --apply run
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
        self.g = g if g is not None else hr_gate()
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
      2. fill      Staging stocking (Michal 2026-11-15; before: 2026-11-14 7:16 PM ET).
                   (A) Each pull in flight gets ONE served check, never a wait: when EVERY
                   staging site's .deploy-sha contains its head, hr-gate --apply moves it
                   into Staging. The serial lock is never held for a redeploy, so a promote
                   never waits on one. The swarm-promoted daemon queues the next cascade
                   when a staging .deploy-sha changes. (B) While Staging + pulls in flight
                   is below FLOOR (the target, default 2; --floor or SWARM_STAGING_FLOOR),
                   EVERY Ready PR that merges cleanly (git merge-tree) with main, work, each
                   Human Review and Staging PR and the batch so far is merged into work in
                   ONE push (Flow.pull_batch, up to flow.STAGING_MAX in Staging), so one
                   redeploy (one CORNERBOT_REQUEST for ptw) covers all of them. Nothing is
                   rebased, reworked or sent back just to fill Staging. The promote ran
                   first in the pass, so a promote frees a slot and the next pull starts in
                   the same pass. A fast-track PR is pulled also at the target (Michal
                   2026-11-14 8:43 PM). Each move goes to the move log (tangentswarm.moves).
      park         (tangentswarm.slots) the Claude of each PR in Ready exits; its tmux
                   window, branch and worktree stay, and its session id is kept so that
                   swarm-revive PR FEEDBACK can resume it in a rework slot.
      3. signal    (tangentswarm.slots) fewer than NEW_SLOTS new-work Claudes run and Active
                   issues without a PR exist: print one START_CLAUDE line for swarmbot that
                   names the issues to start, in Ready order.
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

    def cornerbot_request(self, n, branch, head, work, host, also=()):
        line = request_line(n, branch, head, work, host, self.dry, also)
        if not self.dry:  # for swarmbot while this run still waits (stdout may be read only at exit)
            try:
                CORNERBOT_FILE.parent.mkdir(parents=True, exist_ok=True)
                CORNERBOT_FILE.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
            except OSError as e:
                self.out(f"cornerbot request file not written: {e}")
        return line

    def lagging(self, d):
        return lagging(self.g, d)

    # -- the pulls in flight: one check, never a wait (Michal 2026-11-15) ---------------------
    def gate_pulls(self, prs, scratch, only=None):
        """ONE served check for each pull in flight, no wait: the serial lock is never held for
        a redeploy, so a promote never waits on one (Human Review sat empty about 15 min twice on
        2026-11-14 while pulls of #495 and #514 waited on a PTW redeploy). A pull whose head
        every staging site serves gets hr-gate --apply. A batch (one work commit) gets ONE
        CORNERBOT_REQUEST. The swarm-promoted daemon queues the next cascade when a staging
        .deploy-sha changes. Returns the PRs whose pull ended (moved or failed)."""
        from . import flow
        every = flow.load_pulls()
        pulls = [d for d in every if only is None or d['pr'] in only]
        ended, asked = [], {d.get('work') for d in every if d.get('asked')}
        for d in pulls:
            n, head = d['pr'], d['head']
            if self.touches_served(n):
                lag = self.lagging(d)
                if lag:
                    corner = [h for k, h, _, _ in lag if k in CORNERBOT_SITES]
                    if corner and d.get('work') not in asked:
                        group = [q for q in flow.load_pulls() if q.get('work') == d.get('work')]
                        also = [q['pr'] for q in group if q['pr'] != n]
                        for host in corner:
                            self.out(self.cornerbot_request(n, d.get('branch', ''), head, d.get('work'), host, also))
                        asked.add(d.get('work'))
                        if not self.dry:
                            for q in group:
                                flow.save_pull(dict(q, asked=True))
                    age = int(max(0, time.time() - float(d.get('started') or 0)))
                    self.out(f"waiting: #{n} {head[:8]} is not served yet by "
                             + '; '.join(f"{h} ({x[:8] if x else (e or 'empty')[:60]})" for _, h, x, e in lag)
                             + f"; pulled {_age(age)} ago; no wait: the pull stays open, and a .deploy-sha "
                               "change queues the next cascade")
                    continue
                self.out(f"served: every staging site serves #{n} {head[:8]}")
                if d.get('work') in asked and not self.dry and CORNERBOT_FILE.exists():
                    CORNERBOT_FILE.unlink(missing_ok=True)
                    self.out(f"CORNERBOT_DONE: every cornerbot site serves #{n} {head[:8]}; request cleared")
            before = len(self.f.did)
            self.out(f"gate: hr-gate on #{n} {head[:8]} (timeout {STEP_TIMEOUT}s)")
            self.f.advance(d, prs, scratch)
            self.flush()
            if (len(self.f.did) > before) if self.dry else n not in flow.pulled_prs():
                ended.append(n)
        return ended

    def _staged(self, cols, nums):
        """Dry run: count the PRs a gate would move (from Ready) into Staging."""
        done = [q for q in cols.get(board.READY, []) if q['number'] in nums]
        return dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q not in done],
                             board.STAGING: cols.get(board.STAGING, []) + done})

    # -- 2. fill ------------------------------------------------------------------
    def fill(self):
        """Staging stocking (Michal 2026-11-15). (A) gate the pulls in flight that staging
        serves (no wait). (B) While Staging + pulls in flight < FLOOR (the target, default 2),
        pull EVERY clean Ready PR (up to flow.STAGING_MAX in Staging) in ONE work push, so one
        redeploy covers them. A fast-track PR is pulled also at the target. The promote ran
        first in this pass, so a promote frees a slot and the next pull starts in the same
        pass; the pull's redeploy never holds the lock."""
        from . import flow
        prs, cols = self.columns()
        cols = self.main_check(cols)
        if self.dry and self.moved:  # the promote step only said "would move"
            up = [p for p in cols.get(board.STAGING, []) if p['number'] in self.moved]
            cols[board.STAGING] = [p for p in cols.get(board.STAGING, []) if p not in up]
            cols[board.HUMAN_REVIEW] = cols.get(board.HUMAN_REVIEW, []) + up
        with self.g['ScratchRepo']() as scratch:
            ended = self.gate_pulls(prs, scratch) if flow.load_pulls() else []
            if ended and self.dry:
                gated = [n for n in ended if any(f"#{n} hr-gate --apply" in line for line in self.f.did)]
                cols = self._staged(cols, gated)
            elif ended:
                prs, cols = self.columns()
            st = cols.get(board.STAGING, [])
            flying = ({d['pr'] for d in flow.load_pulls()} - (set(ended) if self.dry else set())
                      - {p['number'] for p in st})
            fl = f" + {len(flying)} in flight ({', '.join('#' + str(n) for n in sorted(flying))})" if flying else ''
            ready = [p for p in cols.get(board.READY, []) if not p.get('draft') and p['number'] not in flying]
            have = len(st) + len(flying)
            # (d) Michal 2026-11-14 8:43 PM ET: a PR Michal marks urgent (the active fast-track)
            # is pulled also when Staging is at the target; strict still needs a clean merge.
            urgent = [p for p in ready if p.get('rank') == board.FAST_TRACK]
            if have >= FLOOR and not urgent:
                self.out(f"fill: Staging {len(st)}{fl}, target {FLOOR}: no pull needed")
                return cols
            if not ready:
                self.out(f"fill: Staging {len(st)}{fl}, Ready 0: Ready is empty")
                return cols
            if have >= FLOOR:
                pick, room = dict(cols, **{board.READY: urgent}), len(urgent)
                self.out(f"fill: Staging {len(st)}{fl}, target {FLOOR}: fast-track "
                         f"{', '.join('#' + str(p['number']) for p in urgent)} is pulled past the target")
            else:
                pick, room = dict(cols, **{board.READY: ready}), max(1, flow.STAGING_MAX - have)
            batch = self.f.pull_batch(prs, pick, scratch, room)
            self.flush()
            if not batch:
                self.out(f"fill: Staging {len(st)}{fl}: no {'extra ' if have else ''}Ready PR to pull without rework")
                return cols
            nums = [p['number'] for p in batch]
            getattr(self, 'pulled', []).extend(nums)
            ft = ' (fast-track)' if any(p.get('rank') == board.FAST_TRACK for p in batch) else ''
            self.out(f"fill: {', '.join('#' + str(n) for n in nums)} pulled in one work merge{ft}: "
                     f"Staging {len(st)}{fl} + {len(batch)}, target {FLOOR}")
            if self.dry:  # nothing was pushed: show the rest of the chain, then count them in Staging
                work = getattr(self.f, 'last_work', None)
                served = [p for p in batch if self.touches_served(p['number'])]
                if served:
                    p = served[0]
                    lag = self.lagging(dict(pr=p['number'], head=p['head'], work=work))
                    for k, host, _, _ in lag:
                        if k in CORNERBOT_SITES:
                            self.out(self.cornerbot_request(p['number'], p.get('branch', ''), p['head'], work, host,
                                                            [q['number'] for q in batch if q is not p]))
                    self.out("would gate each one when every staging site serves the batch (no wait; the "
                             "daemon queues a cascade on a .deploy-sha change)"
                             + (f" (now lagging: {', '.join(h for _, h, _, _ in lag)})" if lag else ''))
                for n in nums:
                    self.out(f"would run hr-gate --apply {n} (Ready -> Staging when it passes)")
                return dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q not in batch],
                                     board.STAGING: st + batch})
            # one check now, no wait: the PTW request goes out at once
            if self.gate_pulls(prs, scratch, only=set(nums)):
                prs, cols = self.columns()
        return cols

    # -- rework: Ready PRs that cannot be pulled (Michal 2026-11-14 9:10 PM ET deadlock) ------
    def main_check(self, cols):
        """(1) Each Ready PR against origin/main with git merge-tree (local, kept per SHA pair:
        no GraphQL). A conflict: card to In Progress and a rework relay that lists the files.
        Returns cols without those PRs in Ready."""
        ready = [p for p in cols.get(board.READY, []) if not p.get('draft')]
        if not ready:
            return cols
        bad = []
        with self.g['ScratchRepo']() as scratch:
            scratch.ensure_main()
            main = self.f.sha_of(scratch, scratch.main)
            scratch.ensure(*[p['head'] for p in ready])
            for p in ready:
                if self.f.clean(scratch, main, p['head']):
                    continue
                self.rework(p, [('origin/main', self.f.files(scratch, main, p['head']))])
                bad.append(p['number'])
        self.f.cmap.save()
        if bad:
            self.out(f"main check: {', '.join('#' + str(n) for n in bad)} conflict with origin/main: sent to rework")
        return dict(cols, **{board.READY: [p for p in cols.get(board.READY, []) if p['number'] not in bad]})

    def unblock(self, cols, pulled):
        """(2) Human Review or Staging is empty and no Ready PR could be pulled: never end
        quietly. The Ready PR with the fewest conflicts (against origin/main, work and each
        Human Review and Staging head) gets a rework relay and a NEEDS_REWORK line."""
        from . import flow
        hr, st = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        ready = [p for p in cols.get(board.READY, []) if not p.get('draft')]
        if pulled or flow.load_pulls() or (hr and st) or len(st) >= FLOOR:  # at the floor: no pull was wanted
            return
        if not ready:
            self.out(f"STALLED: Human Review {len(hr)}, Staging {len(st)}, Ready empty: new work must reach Ready")
            return
        with self.g['ScratchRepo']() as scratch:
            scratch.ensure_main()
            main = self.f.sha_of(scratch, scratch.main)
            bases = [('origin/main', main), (flow.WORK, self.f.fetch_work(scratch))]
            bases += [(f"#{q['number']}", q['head']) for q in hr + st]
            scratch.ensure(*[sha for _, sha in bases], *[p['head'] for p in ready])
            found = []
            for p in ready:
                conf = [(name, self.f.files(scratch, sha, p['head'])) for name, sha in bases
                        if not self.f.clean(scratch, sha, p['head'])]
                found.append((len(conf), p, conf))
        self.f.cmap.save()
        order = {q['number']: i for i, q in enumerate(flow.ready_order(ready, {c[1]['number']: c[0] for c in found}))}
        n_conf, p, conf = min(found, key=lambda t: (t[0], order[t[1]['number']]))
        self.out(f"STALLED: Human Review {len(hr)}, Staging {len(st)}, no Ready PR can be pulled; "
                 f"best candidate #{p['number']} ({n_conf} conflict(s))")
        if conf:
            self.rework(p, conf)
        else:
            self.out(f"NEEDS_REWORK #{p['number']} (no merge conflict: it fails the stack check; see the notes above)")

    def rework(self, p, conflicts):
        """Card to In Progress, NEEDS_REWORK line, and the relay: swarm-revive in a rework slot,
        else a swarm-resume relay line. One relay per head."""
        from . import slots
        n, head = p['number'], p.get('head') or ''
        files = sorted({f for _, fs in conflicts for f in fs})
        self.out(f"NEEDS_REWORK #{n} {' '.join(files) or '(files unknown)'}")
        where = '; '.join(f"{name}: {', '.join(fs) or 'files unknown'}" for name, fs in conflicts)
        others = [name for name, _ in conflicts if name != 'origin/main']
        msg = (f"Coordinator: #{n} cannot enter Staging: it conflicts with {where}. Merge origin/main into "
               f"your branch (git fetch origin; git merge origin/main). Do not rebase and do not force-push. "
               + (f"Then make sure it also merges cleanly with {', '.join(others)} (resolve the same files). "
                  if others else '')
               + "Resolve the conflicts, run the tests, push, and report the new head and the test counts.")
        st = slots.load()
        if st['relayed'].get(str(n)) == head and head:
            self.out(f"rework: #{n} already has a relay for {head[:8]}: not sent again")
            return
        if self.dry:
            self.out(f"would move #{n} Ready -> In Progress and send: {msg[:160]}")
            return
        self.f.fail(p, f"conflicts with {where}"[:200], relay='merge-main')
        relay = Path(f"/tmp/relay-{n}-merge-main.md")
        try:
            relay.write_text(msg + '\n')
        except OSError as e:
            self.out(f"relay file not written: {e}")
        try:
            rc = slots.revive(n, msg, out=lambda line: self.out('revive: ' + line))
        except Exception as e:  # the relay line below still goes out
            self.out(f"revive: {type(e).__name__}: {str(e)[:160]}")
            rc = 1
        if rc:
            self.out(f"RELAY: swarm-resume {n} {relay}")
        st = slots.load()
        st['relayed'][str(n)] = head
        slots.save(st)

    def columns(self):
        """Flow.columns, and keep the PRs for the park and signal steps."""
        self.prs, cols = self.f.columns()
        return self.prs, cols

    # -- park --------------------------------------------------------------------
    def park(self, cols):
        """(a) Michal 2026-11-14 8:43 PM ET: the Claude of a PR in Ready exits (its window,
        branch and worktree stay; its session id is kept for swarm-revive)."""
        from . import slots
        try:
            slots.park_ready(getattr(self, 'prs', None) or {}, cols, dry=self.dry, out=self.out)
        except Exception as e:  # parking must not stop the signal step
            self.out(f"park: skipped: {type(e).__name__}: {str(e)[:200]}")

    # -- 3. signal ----------------------------------------------------------------
    def signal(self, cols):
        """(c) Michal 2026-11-14 8:43 PM ET: START_CLAUDE whenever fewer than slots.NEW_SLOTS
        new-work Claudes run and Active issues without a PR exist (slots.NEW_SLOTS_SHORT while
        Ready is short). The line names the issues to start, in Ready order."""
        from . import slots
        prs = getattr(self, 'prs', None) or {}
        ready = len([p for p in cols.get(board.READY, []) if not p.get('draft')])
        k = slots.kinds(slots.attach_prs(slots.running(), prs), slots.load())
        from . import boardcache
        issues = slots.active_issues(boardcache.snapshot(self.g['gql']), board.active_fast_track(), prs)
        n, target = slots.start_plan(len(k['new']), issues, ready, READY_TARGET)
        facts = (f"new={len(k['new'])}/{target} rework={len(k['rework'])}/{slots.REWORK_SLOTS} "
                 f"ready={ready}/{READY_TARGET} active_without_pr={len(issues)}")
        if n:
            self.out(f"START_CLAUDE n={n} issues={','.join('#' + str(i['number']) for i in issues[:n])} {facts}")
        elif not issues:
            self.out(f"signal: no Active issue without a PR ({facts}): no START_CLAUDE")
        else:
            self.out(f"signal: the new-work slots are full ({facts}): no START_CLAUDE")

    def run(self):
        from .promoted import read_columns
        t0 = self.clock()
        os.environ[gqlmeter.PASS_ENV] = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"  # hr-gate children log it too
        mark = dict(gqlmeter.TALLY)
        self.out(f"cascade: start{' (dry run)' if self.dry else ''}; budget {BUDGET}s; "
                 f"pass {os.environ[gqlmeter.PASS_ENV]}")
        try:
            return self._run(t0)
        finally:
            calls, pts = gqlmeter.since_mark(mark)
            self.out(f"graphql: {pts} points in {calls} queries in this process "
                     f"(remaining {gqlmeter.TALLY['remaining']}); per pass with hr-gate: "
                     f"python -m tangentswarm.gqlmeter --passes")

    def _run(self, t0):
        from .promoted import read_columns
        self.out("promote: reading the board")
        if not self.promote(read_columns(self.g['gql'], max_age=0)):  # the one board read of this pass
            self.out('promote: ' + self.why[-1])
        self.out("fill: reading the board")
        self.pulled = []
        cols = self.fill()
        self.unblock(cols, self.pulled)
        self.park(cols)
        self.signal(cols)
        self.out(f"cascade: done in {int(self.clock() - t0)}s")
        return 0


def hr_gate():
    """The live hr-gate namespace (its GraphQL, fetch, served_all, contains helpers)."""
    import runpy
    from .promoted import BIN
    return runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_advance')


def request_line(n, branch, head, work, host, dry=False, also=()):
    return (f"CORNERBOT_REQUEST{' (dry run)' if dry else ''}: redeploy {host} from platform work "
            f"{work[:8] if work else '(after the merge)'} (#{n} {branch} {head[:8]} is in work)"
            + (f" with {', '.join('#' + str(x) for x in also)}" if also else ''))


def lagging(g, d):
    """[(site key, host, static sha, err)] for each staging site that does not serve the
    pulled head yet. A site serves it when its .deploy-sha is the pull's work commit or
    contains the head (platform #493: ptw lagged while the other two served it)."""
    if isinstance(g.get('_fetched'), list):
        g['_fetched'].clear()  # fetch work again: a served commit may be new
    return [(k, host, sha, err) for k, host, sha, err in g['served_all']()
            if not (sha and (sha == d.get('work') or g['contains'](d['head'], sha)))]


def _age(s):
    s = int(max(0, s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _et(t):
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(t, ZoneInfo('America/New_York')).strftime('%H:%M')
    except Exception:
        return time.strftime('%H:%M', time.localtime(t))


def cornerbot_reminder(g, out=print, dry=False, now=time.time):
    """Every `swarm-advance --cascade` run (also a busy run, a run with no rerun flag, and the
    run the daemon drains from its queue) prints the open CORNERBOT_REQUEST again, with its
    age, until every cornerbot site serves the pulled head; then it clears CORNERBOT_FILE.
    Michal 2026-11-15: Human Review and Staging sat empty 10:20-11:11 PM ET because the line
    for #495 4e3928e4 was printed once and lost. No GraphQL: the pull state, the
    .deploy-sha files and git only. Returns the printed line or None."""
    from . import flow
    try:
        at = CORNERBOT_FILE.stat().st_mtime
        text = CORNERBOT_FILE.read_text().strip()
    except OSError:
        return None
    m = re.search(r'\(#(\d+) ', text)
    pulls = flow.load_pulls()
    d = next((q for q in pulls if m and q['pr'] == int(m.group(1))), None) if m else (pulls[0] if pulls else None)

    def clear(line):
        if not dry:
            CORNERBOT_FILE.unlink(missing_ok=True)
        out(line + ('' if not dry else ' (dry run: file kept)'))
        return line
    if not d:
        return clear(f"CORNERBOT_REQUEST cleared: no running pull waits on it ({text[:160]})")
    try:
        corner = [(k, h) for k, h, _, _ in lagging(g, d) if k in CORNERBOT_SITES]
    except Exception as e:  # a site or git read failed: say the request again, decide next run
        corner, why = None, f"{type(e).__name__}: {str(e)[:120]}"
    if corner == []:
        return clear(f"CORNERBOT_DONE: every cornerbot site serves #{d['pr']} {d['head'][:8]} "
                     f"{_age(now() - at)} after the request; request cleared")
    hosts = [h for _, h in corner] if corner else re.findall(r'redeploy (\S+) from', text)[:1] or ['?']
    also = [q['pr'] for q in pulls if q.get('work') == d.get('work') and q['pr'] != d['pr']]
    line = '; '.join(request_line(d['pr'], d.get('branch', ''), d['head'], d.get('work'), h, also=also) for h in hosts)
    line += (f"; open {_age(now() - at)} (asked {_et(at)} ET)"
             + ('; not served yet' if corner else f'; served check failed ({why})'))
    out(line)
    return line


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
                        if rate_limited(e):  # no busy retry: one rerun after the reset
                            out(f"stopped: GraphQL rate limit; it resets at {gqlmeter.when(getattr(e, 'reset', None))}; "
                                f"lock released, rerun flag kept; {schedule(e.reset)}")
                            return EXIT_RATELIMIT
                        out(f"stopped: {type(e).__name__}: {str(e)[:200]}; lock released, rerun flag kept")
                        if isinstance(e, CascadeTimeout):
                            return EXIT_TIMEOUT
                        raise
        except gatelock.Busy as e:
            out(f"busy: {e}; rerun flag set, the lock holder runs the cascade again; {enqueue()}")
            return 0
    return 0


EXIT_RATELIMIT = 75  # EX_TEMPFAIL: the rerun is scheduled
RERUN_UNIT = 'swarm-cascade-after-ratelimit'
RERUN_SLACK = 60  # seconds after the reset


def rate_limited(e):
    return isinstance(e, Exception) and gqlmeter.limited(e)


def schedule(reset, run=subprocess.run, now=time.time):
    """One `swarm-advance --cascade --if-flagged` after the reset (systemd-run --on-active). A
    timer that is already waiting is kept: one rerun, not one per caller."""
    from .promoted import BIN
    delay = int(max(0, (reset or now() + 3600) - now())) + RERUN_SLACK
    try:
        if run(['systemctl', '--user', 'is-active', '--quiet', RERUN_UNIT + '.timer'],
               capture_output=True, timeout=30).returncode == 0:
            return f"a rerun is already scheduled ({RERUN_UNIT}.timer)"
        r = run(['systemd-run', '--user', '--collect', f'--unit={RERUN_UNIT}', f'--on-active={delay}s',
                 str(BIN / 'swarm-advance'), '--cascade', '--if-flagged'],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"rerun NOT scheduled ({e})"
    if r.returncode:
        return f"rerun NOT scheduled ({(r.stderr or r.stdout).strip()[:160]})"
    return f"one rerun scheduled in {delay}s ({RERUN_UNIT}.timer)"


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
            watchdog()
            try:  # before the lock: a busy run and a no-flag run print it too
                cornerbot_reminder(hr_gate(), out=_say, dry=a.dry_run)
            except Exception as e:  # the reminder must never stop the cascade
                _say(f"cornerbot reminder failed: {type(e).__name__}: {str(e)[:160]}")
            if a.dry_run:  # no lock, no flag: a dry run changes nothing
                return Cascade(dry_run=True, out=_say).run()
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
        if rate_limited(e):  # a dry run or a plain step: no lock to keep, no rerun to schedule
            print(f"swarm-advance: GraphQL rate limit; it resets at {gqlmeter.when(getattr(e, 'reset', None))}; no retry",
                  file=sys.stderr)
            return EXIT_RATELIMIT
        print(f"swarm-advance error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
