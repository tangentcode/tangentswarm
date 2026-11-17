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
releases the lock. Lines for swarmbot: START_CLAUDE n=K issues=#A,#B ..., NEEDS_REWORK, RELAY,
STALLED and REDEPLOY_LATE.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import board, codexsummary, gatelock, gqlmeter, proc

REPO = 'tangentcode/platform'
CODEX = 'chatgpt-codex-connector'
MINBOT = os.environ.get('SWARM_MINBOT', 'minbot')
MINBOT_ROWS = ('1 ', '2 ', '3 ', '5 ')  # not draft, mergeable, Codex clean, Linux CI

# --cascade (Michal 2026-11-14)
# the Staging WIP target (tangentswarm #65: never pulled past; SWARM_STAGING_TARGET, --floor)
FLOOR = int(os.environ.get('SWARM_STAGING_FLOOR', os.environ.get('SWARM_STAGING_TARGET', '2')))
READY_TARGET = int(os.environ.get("SWARM_READY_TARGET", "6"))      # Ready short: one more new-work slot (slots.NEW_SLOTS_SHORT)
BUDGET = int(os.environ.get('SWARM_CASCADE_BUDGET', '780'))   # whole run; the tick kills it at 900 s
STEP_TIMEOUT = int(os.environ.get('SWARM_CASCADE_STEP_TIMEOUT', '300'))  # one hr-gate --apply run
EXIT_TIMEOUT = 124
DEADLINE = None  # time.monotonic() deadline of this process (watchdog)
CASCADE_LOG = Path(os.environ.get('SWARM_CASCADE_LOG', str(Path.home() / '.local' / 'state' / 'swarm-flow' / 'cascade.log')))
MAX_PASSES = int(os.environ.get('SWARM_CASCADE_MAX_PASSES', '10'))  # cascade passes per lock hold
RERUN = Path(os.environ.get('SWARM_CASCADE_RERUN', str(gatelock.LOCK.with_name('swarm-serial.rerun'))))
REDEPLOY_LATE = int(os.environ.get('SWARM_REDEPLOY_LATE', '600'))  # lag warning only after this (Michal 2026-11-15)
DEPLOY_FALLBACK = int(os.environ.get('SWARM_DEPLOY_FALLBACK', '180'))  # then accept a .deploy-sha that contains the head

CODEX_FIELDS = """number state isDraft headRefOid baseRefName mergeable body
 closingIssuesReferences(first:3){nodes{number comments(last:1){nodes{body}}}}
 comments(last:40){nodes{author{login} body createdAt}}
 commits(last:1){nodes{commit{committedDate}}}
 reviews(last:30){nodes{author{login} commit{oid} comments{totalCount}}}
 reviewThreads(last:60){nodes{id isResolved isOutdated comments(first:1){nodes{author{login} originalCommit{oid}}}}}"""


def _codex(node):
    return ((node or {}).get('author') or {}).get('login', '').startswith(CODEX)


def _thread_commit(t):
    first = ((t.get('comments') or {}).get('nodes') or [{}])[0]
    return (first.get('originalCommit') or {}).get('oid')


def _open_codex_threads(pr):
    return [t for t in (pr.get('reviewThreads') or {}).get('nodes') or []
            if not t.get('isResolved') and not t.get('isOutdated')
            and _codex(((t.get('comments') or {}).get('nodes') or [{}])[0])]


def _review_comments(r):
    return ((r.get('comments') or {}).get('totalCount') or 0)


def _receipt(body, head):
    return 'Reviewed commit' in body and head[:8] in body and "Didn't find any major issues" in body


def codex_decision(pr):
    """('clean' | 'findings' | 'pending', detail) on the exact head, for every reader that acts
    on a Codex result (the MinBot pass, swarm-webhook, its replay).
    Findings are only on the head: an open Codex thread on it (or of unknown commit), a Codex
    review of the head with comments, or a review-state summary of the head with findings.
    Findings win over every clean signal (tangentswarm #75).
    Clean is a clean receipt, a completed summary of the head with both findings flags false,
    or a Codex review of the head with no comment. A running summary, null flags or another
    sha is never clean (codexsummary.result). An open thread from an OLDER head does not
    count once Codex is clean on the head (MethBot 2026-11-15; Codex on PR #26: the webhook
    relayed already-fixed findings from an older head and demoted the PR); the MinBot pass
    resolves it (addressed_threads)."""
    head = pr.get('headRefOid') or ''
    if not head:
        return 'pending', 'no head'
    on_head = [t for t in _open_codex_threads(pr) if _thread_commit(t) in (head, None)]
    if on_head:
        return 'findings', f'{len(on_head)} open Codex thread(s) on {head[:8]}'
    reviews = [r for r in (pr.get('reviews') or {}).get('nodes') or []
               if _codex(r) and (r.get('commit') or {}).get('oid') == head]
    if any(_review_comments(r) for r in reviews):
        return 'findings', f'a Codex review of {head[:8]} has comments'
    comments = (pr.get('comments') or {}).get('nodes') or []
    summary = codexsummary.on_head(comments, head, _codex)
    if summary == codexsummary.FINDINGS:
        return 'findings', f'the Codex summary of {head[:8]} has findings'
    if summary == codexsummary.CLEAN or reviews \
            or any(_codex(c) and _receipt(c.get('body') or '', head) for c in comments):
        return 'clean', f'Codex clean on {head[:8]}'
    return 'pending', f'no Codex result on {head[:8]}'


def codex_clean(pr):
    """hr-gate condition 3 on the exact head: codex_decision() is clean."""
    return codex_decision(pr)[0] == 'clean'


def codex_details(gql, n, repo=REPO):
    """The CODEX_FIELDS of PR n (one GraphQL read), or {}."""
    owner, name = repo.split('/')
    d = gql(f'query{{repository(owner:"{owner}",name:"{name}"){{p: pullRequest(number:{int(n)}){{{CODEX_FIELDS}}}}}}}')
    return ((d or {}).get('repository') or {}).get('p') or {}


def addressed_threads(pr):
    """The ids of open Codex threads from an older head, once Codex is clean on the head."""
    if not codex_clean(pr):
        return []
    head = pr.get('headRefOid') or ''
    return [t['id'] for t in _open_codex_threads(pr) if t.get('id') and _thread_commit(t) not in (head, None)]


OPEN_QUESTION_RE = re.compile(r'(?im)^\s*(?:#+\s*|\*\*)?(?:open\s+)?questions?\s+for\s+michal')


def open_question(pr):
    """platform #541 (2026-11-15): no MinBot pass while the PR has an open question for Michal:
    a "Question(s) for Michal" heading or line in the PR body, or a linked issue whose
    latest comment is the Stop hook's Needs Input question (nobody answered after it)."""
    from .stophook import QUESTION_MARK
    if OPEN_QUESTION_RE.search(pr.get('body') or ''):
        return 'the PR body has an open question for Michal'
    for i in ((pr.get('closingIssuesReferences') or {}).get('nodes') or []):
        last = ((i.get('comments') or {}).get('nodes') or [{}])[-1]
        if QUESTION_MARK in (last.get('body') or ''):
            return f"issue #{i.get('number')} has an unanswered question for Michal"
    return None


def minbot_order(prs, active=None):
    """Auto Review PRs to try, in the shared pick order (board.pick_key; tangentswarm #35)."""
    return sorted(prs, key=lambda p: board.pick_key(p, active))


CI_BAD = ('failure', 'cancelled', 'timed_out', 'action_required', 'startup_failure', 'stale')


def check_runs(head, repo=None):
    """[{name, status, conclusion}] of the latest check runs on the exact head (REST, the
    minbot App quota through the gh shim)."""
    r = proc.sh('gh', 'api', '--paginate', '--slurp',
                f'repos/{repo or REPO}/commits/{head}/check-runs?per_page=100&filter=latest',
                check=False, who='swarm-advance')
    if r.returncode:
        raise RuntimeError(f"check runs of {head[:8]}: {(r.stderr or r.stdout).strip()[:160]}")
    return [c for page in json.loads(r.stdout or '[]') for c in page.get('check_runs', [])]


def ci_problem(runs):
    """tangentswarm #30: why CI on the head is not green, or None. A failed, cancelled or
    timed-out job is never green (platform #516: "Linux mna tests" failed at 10:17 PM ET and
    a MinBot pass at 10:20 PM ET still said "CI green (5 checks)"); a job still running is
    not green yet. Skipped and neutral jobs are fine."""
    bad = [f'"{c.get("name")}" {c.get("conclusion")}' for c in runs
           if c.get('status') == 'completed' and c.get('conclusion') in CI_BAD]
    if bad:
        return 'CI is not green: ' + ', '.join(bad) + ' on this head'
    running = [c.get('name') for c in runs if c.get('status') != 'completed']
    if running:
        return 'CI is still running: ' + ', '.join(f'"{n}"' for n in running)
    return None


def check_text(head, why):
    return f"{why}.\nThe card stays out of Ready (swarm-advance, mechanical)."


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
            q = open_question(d)
            if q:
                skipped.append(f"#{n} {q}")
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
            ci = ci_problem(check_runs(p['head']))  # every job on the exact head, not only Linux
            if ci:
                skipped.append(f"#{n} {ci}")
                self.ci_check_comment(p, ci)
                continue
            self.apply_pass(p, d)
            self.resolve_addressed(n, d)
            return True
        self.why.append('Auto Review: ' + ', '.join(skipped))
        return False

    def minbot_one(self, n):
        """swarm-advance --minbot N (the Stop hook, tangentswarm #28): the MinBot check of one
        Auto Review PR. A pass posts the MinBot pass comment and moves the card to Ready."""
        from .promoted import read_columns
        cols = read_columns(self.g['gql'], max_age=0)
        ar = [p for p in cols.get(board.AUTO_REVIEW, []) if p['number'] == n]
        if not ar:
            self.out(f"#{n} is not in Auto Review: no MinBot check")
            return 0
        from . import eventdb
        if not self.minbot_pass({board.AUTO_REVIEW: ar}):
            self.out(f"#{n} stays in Auto Review: " + (self.why[-1] if self.why else 'no MinBot pass'))
            if not self.dry:
                eventdb.log_event('minbot-check', source='cascade', action='no pass', pr=n, notes=self.why[-1:])
            return 0
        if not self.dry:
            eventdb.log_event('minbot-check', source='cascade', action='pass', pr=n, notes=['Auto Review -> Ready'])
        if not self.dry:  # Codex on PR #26: a new Ready PR may fill Staging now, not at the next tick
            flag_rerun(by=f'MinBot pass #{n}')
            self.out(f"#{n} reached Ready: cascade {queue_cascade()}")
        return 0

    def full_check(self, n):
        """None if hr-gate conditions 1, 2, 3 and 5 pass on the head, else the failed rows."""
        full = self.g['fetch'](n, refresh=True)
        with self.g['ScratchRepo']() as scratch:
            rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](), scratch=scratch)
        bad = [f"{name.strip()}: {why[:80]}" for name, ok, why in rows if name.startswith(MINBOT_ROWS) and not ok]
        return '; '.join(bad) if bad else None

    def resolve_addressed(self, n, d):
        """After the pass: resolve the open Codex threads from older heads (the worker pushed
        the fix; Codex is clean on the new head)."""
        ids = addressed_threads(d)
        if not ids:
            return
        if self.dry:
            self.out(f"#{n} would resolve {len(ids)} addressed Codex thread(s) from an older head")
            return
        done = 0
        for tid in ids:
            try:
                self.g['gql']('mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}',
                              id=tid)
                done += 1
            except Exception as e:
                self.out(f"#{n} thread {tid} not resolved: {type(e).__name__}: {str(e)[:120]}")
        self.out(f"#{n} resolved {done} addressed Codex thread(s) from an older head")

    def ci_check_comment(self, p, why):
        """Post the "MinBot check" comment once per head when a job failed (#30 mockup)."""
        from . import tick
        if not why.startswith('CI is not green'):
            return
        state = tick.State(tick.STATE_DIR / 'state.json')
        key = f"minbot-check:{p['number']}@{p['head']}"
        if state.done(key):
            return
        self.minbot('evidence', p['number'], '--repo', REPO, '--title', 'MinBot check', '--commit', p['head'],
                    '--body', check_text(p['head'], why))
        if not self.dry:
            state.mark(key)

    def apply_pass(self, p, d=None):
        """The pass comment (once per head: a pass whose move failed is not posted again on the
        retry), then the card to Ready."""
        n, head = p['number'], p['head']
        mark = pass_text(head).split(' (')[0]
        if not any(mark in (c.get('body') or '') for c in ((d or {}).get('comments') or {}).get('nodes') or []):
            self.minbot('evidence', n, '--repo', REPO, '--title', 'MinBot pass', '--commit', head,
                        '--body', pass_text(head))
        if not self.dry:
            board.move_card(self.g, p['item'], board.READY, n=n)
            from . import moves
            moves.log_move(n, 'Auto Review', 'Ready', head, note='swarm-advance MinBot pass')
        self.out(f"{'would ' if self.dry else ''}MinBot-pass #{n} at {head[:8]}: pass comment, "
                 "Auto Review -> Ready")
        self.shots_on_ready(n, head)

    def shots_on_ready(self, n, head):
        """Michal 2026-11-15 2:23 AM ET (#519): a UI PR that reaches Ready with no screenshot
        gets the screenshot job now (its worker is woken or resumed), not a demote 90 min
        later. api-only and docs-only PRs need none (gatechecks.needs_screenshots)."""
        from . import flow, gatechecks
        try:
            full = self.g['fetch'](n)
            paths = [f.get('path') or '' for f in ((full.get('files') or {}).get('nodes') or [])]
            body = full.get('body') or ''
        except Exception as e:
            self.out(f"#{n} screenshot check skipped: {type(e).__name__}: {str(e)[:120]}")
            return
        if not gatechecks.needs_screenshots(paths, body) or gatechecks.image_urls(body):
            return
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        f.screenshot_job(n, head, full)
        for line in f.did + f.notes:
            self.out(line)

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
                   when a staging .deploy-sha changes. (B) Every pass pre-merges the top
                   Ready PRs onto future main (main + Human Review + Staging heads, in
                   that order) as a new <branch>-vN (Flow.premerge, tangentswarm #65): a
                   clean one gets the label merge-ready; a real conflict gets pre-merging
                   and a worker resolves it on the -vN only (never on the PR branch).
                   (C) While Staging + pulls in flight is below FLOOR (the WIP target,
                   default 2; --floor, SWARM_STAGING_FLOOR or SWARM_STAGING_TARGET), the
                   Ready PRs are pulled in strict pick order, up to the target, as their
                   -vN tips in ONE work push (Flow.pull_batch), so one redeploy covers all
                   of them. The first PR that is not merge-ready stops the pull: nothing
                   behind it is pulled, and nothing is pulled past the target, also for
                   fast-track. The promote ran first in the pass, so a promote frees a slot
                   and the next pull starts in the same pass (Michal
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

    def lagging(self, d):
        return lagging(self.g, d)

    # -- the pulls in flight: one check, never a wait (Michal 2026-11-15) ---------------------
    def gate_pulls(self, prs, scratch, only=None):
        """ONE served check for each pull in flight, no wait: the serial lock is never held for
        a redeploy, so a promote never waits on one (Human Review sat empty about 15 min twice on
        2026-11-14 while pulls of #495 and #514 waited on a PTW redeploy). A pull whose head
        every staging site serves gets hr-gate --apply. Every staging site redeploys the work
        tip by itself (ptw: a timer, 2 to 4 min), and the swarm-promoted daemon queues the next
        cascade when a staging .deploy-sha changes. A pull not served after REDEPLOY_LATE s
        prints REDEPLOY_LATE. Returns the PRs whose pull ended (moved or failed)."""
        from . import flow
        ended = []
        for d in flow.load_pulls():
            n, head = d['pr'], d['head']
            if only is not None and n not in only:
                continue
            if self.touches_served(n):
                lag = self.lagging(d)
                if lag:
                    age = int(max(0, time.time() - float(d.get('started') or 0)))
                    sites = '; '.join(f"{h} ({x[:8] if x else (e or 'empty')[:60]})" for _, h, x, e in lag)
                    if age < REDEPLOY_LATE:  # a normal redeploy (2 to 4 min): no warning
                        self.out(f"waiting: #{n} {head[:8]} not served yet by {sites}; pulled {_age(age)} ago; "
                                 "no wait: a .deploy-sha change queues the next cascade")
                    else:
                        logs = deploy_logs()
                        see = [logs[k] for k, *_ in lag if k in logs]
                        self.out(f"REDEPLOY_LATE: #{n} {head[:8]} (work {str(d.get('work'))[:8]}) is not served "
                                 f"by {sites} {_age(age)} after the work merge"
                                 + (f"; see {', '.join(see)}" if see else ''))
                    continue
                self.out(f"served: every staging site serves #{n} {head[:8]}")
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
        serves (no wait). (C) While Staging + pulls in flight < FLOOR (the WIP target,
        default 2), pull Ready PRs in strict pick order, up to the target, in ONE work push,
        so one redeploy covers them; the first one that is not merge-ready stops the pull
        (no queue-jumping, never past the target, also for fast-track). (B) Then pre-merge
        the top Ready PRs onto future main (Flow.premerge, every pass; tangentswarm #65), on
        the queue with this pass's pulls in it (tangentswarm #69). The promote ran
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
            cols = self._fill_pull(prs, cols, scratch, st, fl, ready, have)
            # tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): every pass keeps the top Ready
            # PRs pre-merged onto future main, so a freed Staging slot refills at once.
            # tangentswarm #69: AFTER the pull, so the pre-merge uses the queue with the pulled
            # PRs in it, never the queue from before the pull (a -vN that is stale at once).
            try:
                self.f.premerge(cols, scratch)
            finally:
                self.flush()
        return cols

    def _fill_pull(self, prs, cols, scratch, st, fl, ready, have):
        """(C) of fill: the pull. Fixed WIP: never past the target, also for a fast-track PR
        (it goes first in the order). Returns the columns after it."""
        if have >= FLOOR:
            self.out(f"fill: Staging {len(st)}{fl}, target {FLOOR}: no pull needed")
            return cols
        if not ready:
            self.out(f"fill: Staging {len(st)}{fl}, Ready 0: Ready is empty")
            return cols
        pick, room = dict(cols, **{board.READY: ready}), FLOOR - have
        batch = self.f.pull_batch(prs, pick, scratch, room)
        self.flush()
        if not batch:
            self.out(f"fill: Staging {len(st)}{fl}: the next Ready PR is not merge-ready; nothing is pulled "
                     "past it (strict order)")
            return cols
        nums = [p['number'] for p in batch]
        getattr(self, 'pulled', []).extend(nums)
        ft = ' (fast-track)' if any(board.item_fast_track(p, board.active_fast_track()) for p in batch) else ''
        self.out(f"fill: {', '.join('#' + str(n) for n in nums)} pulled in one work merge{ft}: "
                 f"Staging {len(st)}{fl} + {len(batch)}, target {FLOOR}")
        if self.dry:  # nothing was pushed: show the rest of the chain, then count them in Staging
            work = getattr(self.f, 'last_work', None)
            served = [p for p in batch if self.touches_served(p['number'])]
            if served:
                p = served[0]
                lag = self.lagging(dict(pr=p['number'], head=p['head'], work=work))
                self.out("would gate each one when every staging site serves the batch (no wait; the "
                         "daemon queues a cascade on a .deploy-sha change)"
                         + (f" (now lagging: {', '.join(h for _, h, _, _ in lag)})" if lag else ''))
            for n in nums:
                self.out(f"would run hr-gate --apply {n} (Ready -> Staging when it passes)")
            return dict(cols, **{board.READY: [q for q in cols.get(board.READY, []) if q not in batch],
                                 board.STAGING: st + batch})
        # one check now, no wait (a PR without served files is gated at once)
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
        quietly. tangentswarm #65 (Michal 2026-11-16 3:22 PM ET): strict order, so the line
        names the NEXT Ready PR in the pick order and its pre-merge state; its conflict is
        resolved by its worker on a <branch>-vN (Flow.premerge), never on its own branch. A
        conflict with origin/main itself was sent to rework by main_check."""
        from . import flow
        hr, st = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        ready = [p for p in cols.get(board.READY, []) if not p.get('draft')]
        if pulled or flow.load_pulls() or (hr and st) or len(st) >= FLOOR:  # at the floor: no pull was wanted
            return
        if not ready:
            self.out(f"STALLED: Human Review {len(hr)}, Staging {len(st)}, Ready empty: new work must reach Ready")
            return
        p = flow.ready_order(ready)[0]
        rec = flow.load_premerge().get(str(p['number'])) or {}
        if rec.get('head') != p.get('head'):
            how = 'not pre-merged yet'
        elif rec.get('status') == 'ready':
            how = f"merge-ready on {rec.get('vbranch')}; its pull did not go through (see the notes above)"
        else:
            how = (f"pre-merging: its worker resolves the conflict on {rec.get('vbranch')}"
                   + ('' if str(rec.get('relayed') or '').startswith(str(rec.get('vbranch'))) else ' (no worker has the relay yet)'))
        self.out(f"STALLED: Human Review {len(hr)}, Staging {len(st)}, no Ready PR was pulled; "
                 f"next in order #{p['number']}: {how}")

    def rework(self, p, conflicts):
        """Card to In Progress, NEEDS_REWORK line, and the relay: swarm-revive in a rework slot,
        else a swarm-resume relay line. One relay per head."""
        from . import flow, slots
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
        moved = self.f.fail(p, f"conflicts with {where}"[:200], relay='merge-main', text=msg)
        self.flush()
        if not moved:  # no worker could be started: the card stays, and the next pass tries again
            self.out(f"RELAY: swarm-resume {n} {flow.RELAY_DIR / f'relay-{n}-merge-main.md'}")
            return  # Codex on PR #26: never record a relay no worker took
        with slots.update() as st:
            st['relayed'][str(n)] = head

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
        try:  # Michal 2026-11-15 4:40 AM ET: idle non-PR workers too (/exit, the window stays)
            slots.park_idle_others(dry=self.dry, out=self.out)
        except Exception as e:
            self.out(f"park: idle workers skipped: {type(e).__name__}: {str(e)[:200]}")

    # -- 3. signal ----------------------------------------------------------------
    def signal(self, cols):
        """(c) Michal 2026-11-14 8:43 PM ET: START_CLAUDE whenever fewer than slots.NEW_SLOTS
        new-work Claudes run and Backlog issues without a PR exist (slots.NEW_SLOTS_SHORT while
        Ready is short). The line names the issues to start, from slots.backlog_issues (Michal
        2026-11-15: never Parked, Triage or #377). A named issue is kept for
        slots.LAUNCH_GRACE s, so the next pass does not name it again before its PR exists."""
        from . import slots
        prs = getattr(self, 'prs', None) or {}
        ready = len([p for p in cols.get(board.READY, []) if not p.get('draft')])
        k = slots.kinds(slots.attach_prs(slots.running(), prs), slots.load())
        from . import boardcache
        snap = boardcache.snapshot(self.g['gql'])
        if not self.dry:
            dropped = slots.prune_named(slots.taken_issues(snap, prs))
            if dropped:
                self.out(f"signal: dropped stale named entries (no draft PR): "
                         f"{', '.join('#' + str(x) for x in dropped)}")
        st = slots.load()
        issues = slots.backlog_issues(snap, board.active_fast_track(), prs,
                                      named=st['named'], launching=st['launching'])
        # tangentswarm #44: capacity from live state (running Claudes, launch and start
        # requests in the spool, worker memory), never from reservations or held lists
        from . import spool
        live = slots.live_claudes()
        flying = [r for r in spool.in_flight(('launch',))]
        cap, why_none = slots.capacity()
        unstarted = st['unstarted']
        cap = self.retry_starts(unstarted, cap)  # launched PRs with no Claude first
        n, target = slots.start_plan(len(k['new']) + len(unstarted) + len(flying), issues, ready, READY_TARGET)
        facts = (f"new={len(k['new'])}/{target} rework={len(k['rework'])}/{slots.REWORK_SLOTS} "
                 f"ready={ready}/{READY_TARGET} backlog_without_pr={len(issues)} "
                 f"claudes={live}/{slots.MAX_CLAUDES} launching={len(flying)} "
                 f"unstarted={len(unstarted)}")
        if n and not cap:
            self.out(f"signal: {n} new-work slot(s) free, but no capacity: {why_none or 'cap used'} "
                     f"({facts})")
            return
        n = min(n, cap)
        if n:
            pick = issues[:n]
            self.out(f"START_CLAUDE n={n} issues={','.join('#' + str(i['number']) for i in pick)} {facts} "
                     f"keys={';'.join(slots.key_text(i) for i in pick)}")
            self.launch(pick)
        elif not issues:
            self.out(f"signal: no Backlog issue without a PR ({facts}): no START_CLAUDE")
        else:
            self.out(f"signal: the new-work slots are full ({facts}): no START_CLAUDE")

    def retry_starts(self, unstarted, cap):
        """Queue a worker start for each launched PR whose window has no Claude (oldest first,
        within the Claude cap; one queued start per PR at a time). Returns the cap left."""
        from . import spool
        reqs = spool.in_flight(('start', 'launch'))  # live: in the spool now
        flying = {int(r['pr']) for r in reqs if r.get('op') == 'start' and r.get('pr')}
        launching = {int(r['issue']) for r in reqs if r.get('op') == 'launch' and r.get('issue')}
        for pr, rec in sorted(unstarted.items(), key=lambda kv: kv[1].get('at', 0)):
            brief_only = bool(rec.get('brief_pane'))  # its Claude runs: only the brief line is sent
            if int(pr) in flying or int(rec.get('issue') or 0) in launching:
                continue  # its start or its launch runs (capacity() already counts it)
            if cap <= 0 and not brief_only:  # Codex on PR #26: a brief retry needs no capacity
                continue
            if self.dry:
                self.out(f"start: would queue the worker start of #{pr} ({rec.get('branch')})")
            else:
                spool.submit('start', '', pr=int(pr))
                self.out(f"start: queued the {'brief line' if brief_only else 'worker start'} of #{pr} "
                         f"({rec.get('branch')})" + ('' if brief_only else ': its window has no Claude'))
            cap -= 0 if brief_only else 1
        return cap

    def launch(self, pick):
        """MethBot 2026-11-15 3:35 AM ET: nothing consumed START_CLAUDE. Queue each named issue
        for tangentswarm.launcher (branch, draft PR "Fixes #N", brief, worker), which the
        swarm-promoted daemon runs outside the serial lock. The picker skips a queued issue
        until its launch ends; it counts as named only once its draft PR exists."""
        from . import slots, spool
        if self.dry:
            for i in pick:
                self.out(f"launch: would queue #{i['number']} ({i['title'][:60]})")
            return
        queued = []
        for i in pick:
            try:
                spool.submit('launch', '', issue=i['number'], title=i['title'])
                queued.append(i['number'])
            except (OSError, ValueError) as e:
                self.out(f"launch: #{i['number']} NOT queued: {e}")
        if queued:
            slots.launch_mark(queued)
            self.out(f"launch: queued {', '.join('#' + str(x) for x in queued)} for swarm-promoted "
                     "(branch, draft PR, brief, worker)")

    def run(self):
        from .promoted import read_columns
        t0 = self.clock()
        os.environ[gqlmeter.PASS_ENV] = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"  # hr-gate children log it too
        mark = dict(gqlmeter.TALLY)
        self.out(f"cascade: start{' (dry run)' if self.dry else ''}; budget {BUDGET}s; "
                 f"pass {os.environ[gqlmeter.PASS_ENV]}")
        lines, out, error = [], self.out, None

        def keep(line):
            lines.append(str(line))
            out(line)
        self.out = keep
        try:
            return self._run(t0)
        except Exception as e:
            error = f'{type(e).__name__}: {str(e)[:300]}'
            raise
        finally:
            self.out = out
            calls, pts = gqlmeter.since_mark(mark)
            self.out(f"graphql: {pts} points in {calls} queries in this process "
                     f"(remaining {gqlmeter.TALLY['remaining']}); per pass with hr-gate: "
                     f"python -m tangentswarm.gqlmeter --passes")
            if not self.dry:  # one events DB row per cascade pass (tangentswarm #32)
                from . import eventdb
                eventdb.log_event('cascade', source='cascade', actor=os.environ[gqlmeter.PASS_ENV],
                                  notes=[l for l in lines if not l.endswith('reading the board')][:80], error=error)

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
        self.f.retry_held(cols)  # after park: a parked worker may have freed a rework slot
        self.flush()
        self.signal(cols)
        self.out(f"cascade: done in {int(self.clock() - t0)}s")
        return 0


def hr_gate():
    """The live hr-gate namespace (its GraphQL, fetch, served_all, contains helpers)."""
    import runpy
    from .promoted import BIN
    return runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_advance')


def lagging(g, d, now=time.time):
    """[(site key, host, static sha, err)] for each staging site that does not serve the
    pulled head yet (platform #493: ptw lagged while the others served it). Every work push
    lands on all four staging sites within about 2.5 min (slowest 2:49; Michal 2026-11-15),
    so "deployed" is every .deploy-sha EQUAL to the pull's work commit: file reads only.
    After DEPLOY_FALLBACK s it falls back to "the .deploy-sha contains the head" (git), for a
    site that already serves a newer work commit."""
    sites = g['served_all']()
    exact = [(k, host, sha, err) for k, host, sha, err in sites if not (sha and sha == d.get('work'))]
    if not exact or now() - float(d.get('started') or 0) < DEPLOY_FALLBACK:
        return exact
    if isinstance(g.get('_fetched'), list):
        g['_fetched'].clear()  # fetch work again: a served commit may be new
    return [(k, host, sha, err) for k, host, sha, err in exact if not (sha and g['contains'](d['head'], sha))]


def deploy_logs(env=os.environ):
    """{site key: deploy log path} from SWARM_DEPLOY_LOGS ("ptw=/path,work=/path"; set by the
    ~/bin wrapper, so no host path is in the repo). REDEPLOY_LATE names the log of each
    lagging site."""
    out = {}
    for part in (env.get('SWARM_DEPLOY_LOGS') or '').split(','):
        k, _, v = part.partition('=')
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _age(s):
    s = int(max(0, s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


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


def queue_self(args=(), qdir=None):
    """Queue `swarm-advance [ARGS]` (the ~/bin wrapper) for the daemon. Returns a note."""
    from .promoted import BIN
    try:
        gatelock.enqueue([str(BIN / 'swarm-advance'), *args], by=f"pid {os.getpid()}", qdir=qdir)
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
    ap.add_argument('--minbot', type=int, metavar='N',
                    help='the MinBot check of one Auto Review PR, under the lock (the Stop hook)')
    ap.add_argument('--floor', type=int, help=f'--cascade: Staging floor (default {FLOOR}, env SWARM_STAGING_FLOOR)')
    ap.add_argument('--repo', help='--minbot: the PR\'s repository when it is not the platform repo (#45)')
    ap.add_argument('--promote-board', type=int, metavar='N',
                    help='a board without staging (Project 6, #45): Ready -> Human Review in pick order')
    ap.add_argument('--board-pass', action='store_true',
                    help='--promote-board: first the MinBot check of every Auto Review PR (the periodic pass)')
    a = ap.parse_args(argv)
    from . import eventdb
    eventdb.set_source('cascade')
    if a.floor is not None:
        FLOOR = a.floor
        os.environ['SWARM_STAGING_FLOOR'] = str(a.floor)  # the move log records it
    try:
        if (a.minbot and a.repo and a.repo != REPO) or a.promote_board:
            from . import boardflow
            lock = f"swarm-advance {'--minbot ' + str(a.minbot) + ' --repo ' + a.repo if a.minbot else '--promote-board ' + str(a.promote_board)}"
            if a.dry_run:
                return boardflow.main(a, Advance(dry_run=True))
            with gatelock.gate_lock(lock):
                return boardflow.main(a, Advance())
        if a.minbot:
            if a.dry_run:
                return Advance(dry_run=True).minbot_one(a.minbot)
            with gatelock.gate_lock(f'swarm-advance --minbot {a.minbot}'):
                return Advance().minbot_one(a.minbot)
        if a.cascade:
            watchdog()
            if a.dry_run:  # no lock, no flag: a dry run changes nothing
                return Cascade(dry_run=True, out=_say).run()
            return run_locked(lambda: Cascade(out=_say).run(), if_flagged=a.if_flagged, out=_say)
        if a.dry_run:
            return Advance(dry_run=True).step()
        with gatelock.gate_lock('swarm-advance'):
            return Advance().step()
    except gatelock.Busy as e:
        # No "busy, give up": the swarm-promoted daemon owns the queue and runs it under the lock.
        args = (['--minbot', str(a.minbot)] + (['--repo', a.repo] if a.repo else []) if a.minbot else []) + \
            (['--promote-board', str(a.promote_board)] if a.promote_board else []) + \
            (['--board-pass'] if a.board_pass else [])  # Codex on PR #47: keep them
        print(f"busy: {e}; " + queue_self(args))
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
