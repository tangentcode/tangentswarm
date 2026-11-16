"""swarm-promoted: refill Human Review within seconds, with no LLM in the path.

A systemd user service. Every POLL seconds (or at once on SIGUSR1, which a webhook
receiver may send) it reads the Project 4 Status column of every PR in one lean
GraphQL query (through the minbot shim, so on the app quota). An OPEN PR counts; a
merged PR whose card still says Human Review does not. When Human Review holds fewer
than 2 and Staging is not empty, it takes the one serial lock (non-blocking; if busy
it retries every RETRY seconds) and:

  1. Fast path, a CHEAP check only (the full checks ran when the PR entered Staging).
     Staging PRs go fast-track first, then in hr-gate's queue order. One may move to
     Human Review when:
       - hr-gate's latest apply line for its current head is PASS or PENDING with no
         failed condition (the head did not change since the gate),
       - it is open, not a draft, and not stacked (base main, no stacked banner),
       - no person (a User, not a bot or app) commented or reviewed after that gate,
       - for a PR that touches web/, app/ or api/: the static .deploy-sha contains the
         head (hr-gate condition 6, strict on entry),
       - it merges cleanly with main, and with main plus each Human Review head
         (local merge-tree, no API call).
     The reads are one GraphQL query for the Staging PRs. The card moves and an hr-gate
     PASS line is written. This repeats until Human Review is full.
  2. When main moved (git ls-remote each poll, no API quota): recheck ONLY "merges
     cleanly with main" for each Staging PR left (tangentswarm.mainmerge). A conflict
     goes to In Progress with a merge-main relay; a clean PR is left alone.
  3. Queued gate requests: a caller that found the serial lock busy queued its command
     (tangentswarm.gatelock) instead of giving up; the daemon runs each one under the lock.
  4. Only if Human Review is still short and a Staging head has NO gate pass: hr-gate
     --apply N for that head, still under the lock (a full gate, no merge, no push). It
     runs ONCE per leftover (PR, head), then again only after BACKSTOP seconds. The work
     merge happens ONCE, at Staging entry: there is no pending-promote here and no -vN
     re-merge at promotion. A stacked PR, or one that conflicts with a Human Review PR,
     waits; when that PR merges, step 2 rechecks it against main. 
  5. With SWARM_PROMOTE_PULL=1 (off by default): the swarm-flow pull and its advance,
     with no promote step. When Staging is short and Ready has a PR, the top Ready PR
     that stacks cleanly is merged into work ONCE (the push redeploys staging) and gated
     into Staging by hr-gate --apply.

    swarm-promoted                  run the daemon
    swarm-promoted --once [--dry-run]   one check (and promotion) and exit
"""
import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import board, gatechecks, gatelock

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
POLL = int(os.environ.get('SWARM_PROMOTE_POLL', '30'))
STOP_LOG = Path.home() / '.local' / 'state' / 'swarm-flow' / 'stop-hook.log'
RETRY = int(os.environ.get('SWARM_PROMOTE_RETRY', '5'))
# The idle check reads the shared board snapshot when it is younger than this (a GraphQL
# budget: a full read every 30 s used more than the hourly quota). SIGUSR1 (a webhook event)
# makes the next check read the board now.
IDLE_AGE = int(os.environ.get('SWARM_PROMOTE_IDLE_AGE', '120'))
# An empty Human Review queues the cascade at once when it drops to 0, then again after this
# many seconds while it stays empty (the cascade escalates: NEEDS_REWORK, STALLED).
CASCADE_EMPTY_HR = int(os.environ.get('SWARM_CASCADE_EMPTY_HR', '900'))
PULL = os.environ.get('SWARM_PROMOTE_PULL') == '1'  # also run the Staging pull
REPO = 'tangentcode/platform'
BACKSTOP = int(os.environ.get('SWARM_PROMOTE_BACKSTOP', '900'))  # retry a leftover head at most this often
MAIN_RETRY = int(os.environ.get('SWARM_MAIN_RETRY', '300'))  # s between retries of a held merge-main handoff
HR_TARGET = int(os.environ.get('SWARM_PROMOTE_HR_TARGET', '2'))  # only raise it for a dry-run proof



def say(line):
    print(time.strftime('%H:%M:%S ') + line, flush=True)


def _names(node):
    return [l['name'] for l in ((node or {}).get('labels') or {}).get('nodes') or []]


def read_board(gql, max_age=None):
    """({column key: [{number, head, item, closes}]} for OPEN platform PRs by option id,
    [fast-track items for board.update_active]), from the shared board snapshot (boardcache;
    max_age 0 reads the board now)."""
    from . import boardcache
    snap = boardcache.snapshot(gql, max_age)
    cols, ft = {}, []
    for it, c in boardcache.open_prs(snap):
        col = boardcache.col(it)
        closes = [dict(number=i['number'], labels=_names(i)) for i in boardcache.closes(c)]
        ft.append(dict(number=c['number'], kind='PullRequest', state='OPEN', col=col, labels=_names(c), closes=closes))
        due, ms = boardcache.pr_due(c)
        cols.setdefault(col, []).append(dict(number=c['number'], head=c['headRefOid'], item=it['id'],
                                             closes=[i['number'] for i in closes], due=due, milestone=ms))
    for it, c in boardcache.open_issues(snap):
        ft.append(dict(number=c['number'], kind='Issue', state='OPEN', col=boardcache.col(it),
                       labels=_names(c), closes=[]))
    return cols, ft


def read_columns(gql, ft_state=None, max_age=None):
    """Columns as read_board, and keep the ONE active fast-track ticket current (board.FT_STATE)."""
    cols, ft = read_board(gql, max_age)
    try:  # the Ready entry time, for the Ready -> Human Review wait in the move log
        from . import moves
        moves.note_ready([p['number'] for p in cols.get(board.READY, [])])
    except OSError as e:
        say(f"ready-seen not saved: {e}")
    try:
        act, prev = board.update_active(ft, ft_state)
        if act != prev:
            say(f"active fast-track: {'#' + str(act) if act else 'none'} (was {'#' + str(prev) if prev else 'none'})")
    except OSError as e:
        say(f"fast-track state not saved: {e}")
    return cols


def needs_pull(cols, running):
    """With SWARM_PROMOTE_PULL=1: a pull runs, or Staging is short and Ready has a PR."""
    from . import flow
    if not PULL:
        return False
    return bool(running) or (len(cols.get(board.STAGING, [])) < flow.STAGING_MIN and bool(cols.get(board.READY)))


def needs_promotion(cols):
    return len(cols.get(board.HUMAN_REVIEW, [])) < HR_TARGET and bool(cols.get(board.STAGING))


def _when(text):
    try:
        t = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def gate_times(log_text):
    """{(#n, head8): time} for heads whose LATEST hr-gate apply line is PASS or PENDING with
    no failed condition. A later BLOCKED or RELAY line at the same head cancels the pass."""
    last = {}
    for line in log_text.splitlines():
        p = line.split('\t')
        if len(p) > 5 and p[1] == 'apply':
            last[(p[2], p[3])] = (p[4] in ('PASS', 'PENDING') and p[5] == 'fail=-', _when(p[0]))
    return {k: t for k, (ok, t) in last.items() if ok and t is not None}


def gated_heads(log_text):
    return set(gate_times(log_text))


DETAIL_FIELDS = """number state headRefOid headRefName isDraft baseRefName body files(first:100){nodes{path}}
 labels(first:20){nodes{name}} closingIssuesReferences(first:10){nodes{number repository{nameWithOwner} labels(first:20){nodes{name}}}}
 comments(last:30){nodes{createdAt author{__typename login}}}
 reviews(last:30){nodes{submittedAt author{__typename login}}}"""


def read_details(gql, numbers):
    """{n: PR fields} for a few PRs in ONE GraphQL query (no REST). A PR whose head SHA and
    updatedAt in the board snapshot are unchanged is not read again (boardcache.detail_get)."""
    if not numbers:
        return {}
    from . import boardcache
    try:
        keys = boardcache.keys(boardcache.snapshot(gql))
    except Exception:
        keys = {}
    out = {}
    for n in numbers:
        hit = boardcache.detail_get(int(n), *keys.get(int(n), (None, None)))
        if hit is not None and hit.get('headRefOid') == keys[int(n)][0]:
            out[int(n)] = hit
    miss = [n for n in numbers if int(n) not in out]
    if miss:
        owner, name = REPO.split('/')
        parts = ' '.join(f'p{n}: pullRequest(number:{int(n)}){{{DETAIL_FIELDS}}}' for n in miss)
        data = gql(f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
        for k, v in data.items():
            if v:
                out[int(k[1:])] = v
                head, upd = keys.get(int(k[1:]), (None, None))
                if v.get('headRefOid') == head:
                    boardcache.detail_put(int(k[1:]), head, upd, v)
    return out


def human_after(pr, since):
    """Logins of people (not bots or apps) who commented or reviewed after `since`."""
    who = []
    for kind, key in (('comments', 'createdAt'), ('reviews', 'submittedAt')):
        for c in (pr.get(kind) or {}).get('nodes') or []:
            a, t = c.get('author') or {}, _when(c.get(key) or '')
            if a.get('__typename') == 'User' and t is not None and t > since:
                who.append(a.get('login') or '?')
    return who


def pr_labels(pr):
    names = [l['name'] for l in (pr.get('labels') or {}).get('nodes') or []]
    for iss in (pr.get('closingIssuesReferences') or {}).get('nodes') or []:
        names += [l['name'] for l in (iss.get('labels') or {}).get('nodes') or []]
    return names


def staging_order(staging, details, queue, active=None):
    """The promote order: the shared pick order (board.pick_key: the ONE active fast-track,
    the soonest milestone due date, the lowest number; tangentswarm #35). It replaces hr-gate's
    queue file order (queue is kept for the callers)."""
    def key(p):
        closes = p.get('closes')
        if closes is None:
            from . import boardcache
            closes = [i.get('number') for i in boardcache.closes(details.get(p['number'], {}))]
        return board.pick_key(dict(p, closes=closes), active)
    return sorted(staging, key=key)


def staging_head(cols, queue):
    """The Staging PR that goes next by queue order alone (no labels read)."""
    order = staging_order(cols.get(board.STAGING, []), {}, queue)
    return order[0] if order else None


def merge_problem(merge_commit, scratch, main, hr, head):
    """None if head merges cleanly onto main, and onto main plus each Human Review head
    (pairwise, like hr-gate's condition 9). Else a short reason. Local merge-tree only."""
    if merge_commit(scratch, main, head, 'probe main') is None:
        return 'conflicts with main'
    for n, sha in hr:
        base = merge_commit(scratch, main, sha, f'main + #{n}')
        if base is not None and merge_commit(scratch, base, head, 'probe') is None:
            return f'conflicts with #{n} in Human Review'
    return None


class Promoter:
    def __init__(self, g=None, dry_run=False):
        import runpy
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_promoted')
        self.dry = dry_run
        self.left = []    # [(n, head, why)] Staging PRs the last fast path did not move
        self.tried = {}   # {(n, head): time} the slow path last ran for this leftover head
        self.last_main = None  # main SHA the last main-merge recheck ran for
        self.main_retry_at = 0.0  # next retry of a merge-main handoff that found no worker
        self.last_hr = None    # Human Review count at the last check
        self.cascade_at = 0.0  # when this daemon last queued a cascade for an empty Human Review
        self.last_deploy = None  # the staging .deploy-sha values at the last check
        self.events = None       # tangentswarm.events reader (made on first use)

    def main_sha(self):
        """origin main's SHA via git ls-remote (no API quota), or None on an error."""
        try:
            r = self.g['sh']('git', 'ls-remote', self.g['origin_url'](), 'refs/heads/main', check=False)
            sha = (r.stdout or '').split()[0] if r.returncode == 0 and r.stdout.strip() else ''
            return sha if len(sha) == 40 else None
        except Exception:
            return None

    def cascade_due(self, cols, main_moved, now=None):
        """Why to queue `swarm-advance --cascade` now, or None (Michal 2026-11-14 9:10 PM ET):
        Human Review dropped to 0 (at once), it stays at 0 (again after CASCADE_EMPTY_HR s), or
        main moved (the cascade checks each Ready PR against the new main)."""
        now = time.time() if now is None else now
        hr = len(cols.get(board.HUMAN_REVIEW, []))
        prev, self.last_hr = self.last_hr, hr
        if hr == 0 and prev is not None and prev > 0:
            return f"Human Review dropped to 0 (was {prev})"
        if hr == 0 and now - self.cascade_at >= CASCADE_EMPTY_HR:
            return "Human Review is empty"
        if main_moved and self.last_main is not None:
            return "main moved"
        return None

    def deploy_due(self):
        """Why to queue a cascade for the pulls in flight, or None (Michal 2026-11-15). The
        cascade never waits on a redeploy; it checks once and exits. So when a staging
        .deploy-sha changes while a pull is in flight (or at the first look after a start),
        the next cascade must gate it. Local file reads only: no GraphQL, no git fetch."""
        from . import flow
        try:
            key = tuple((k, sha) for k, _, sha, _ in self.g['served_all']())
        except Exception:
            return None
        prev, self.last_deploy = self.last_deploy, key
        pulls = flow.load_pulls()
        if not pulls or key == prev:
            return None
        names = ', '.join(f"#{d['pr']}" for d in pulls)
        if prev is None:
            return f"pulls in flight ({names}): first .deploy-sha check"
        new = ', '.join(f"{k} {sha[:8] if sha else '-'}" for k, sha in key if (k, sha) not in prev)
        return f"a staging .deploy-sha changed ({new}) with pulls in flight ({names})"

    def each_second(self):
        """Cheap work every second (its own thread): serve the worker spool
        (tangentswarm.spool: the Stop hook and swarm-worker-mcp requests from inside
        swarm-workers.slice), and act on new GitHub events (tangentswarm.events, #31)."""
        if self.dry:
            return
        from . import events, spool
        try:
            for rid in spool.dispatch(log=str(STOP_LOG)):
                say(f"spool: serving {rid}")
        except Exception as e:
            say(f"spool: {type(e).__name__}: {e}")
        # tangentswarm #31: GitHub events instead of the mn-workflow timer and the Codex poll
        try:
            if self.events is None:
                self.events = events.Events()
            if events.pending() or self.events.mn_pending:
                for line in self.events.poll():
                    say(f"event: {line}")
        except Exception as e:
            say(f"events: {type(e).__name__}: {e}")

    def queue_cascade(self, why, now=None):
        from . import advance
        self.cascade_at = time.time() if now is None else now
        if self.dry:
            say(f"{why}: would queue swarm-advance --cascade")
            return
        advance.flag_rerun(by=f"swarm-promoted: {why}")
        say(f"{why}: cascade {advance.queue_cascade()}")

    def drain(self):
        """Run the queued gate requests (tangentswarm.gatelock queue) under our lock."""
        if self.dry:
            for _, req in gatelock.queued():
                say(f"would run queued {' '.join(req.get('argv') or [])}")
            return []
        return gatelock.drain(gatelock.run_held, say)

    def main_pass(self, cols, moved, main):
        """Main moved: recheck only "merges cleanly with main" for the Staging PRs left."""
        from . import flow, mainmerge
        staging = [p for p in cols.get(board.STAGING, []) if p['number'] not in moved]
        if not staging:
            return []
        details = read_details(self.g['gql'], [p['number'] for p in staging])
        with self.g['ScratchRepo']() as scratch:
            def clean(m, head):
                scratch.ensure(m, head)
                return flow.merge_commit(scratch, m, head, 'probe main') is not None
            give = flow.Flow(dry_run=self.dry, out=lambda l: None, g=self.g).hand_off  # (ok, note)
            return mainmerge.recheck(staging, main, clean, lambda item, col: board.move_card(self.g, item, col), say,
                                     details=details, dry=self.dry, give=give)

    def regate(self):
        """Leftover Staging heads with no gate pass: the only ones the slow path re-gates."""
        return [(n, h, w) for n, h, w in self.left if w.endswith('has no gate pass')]

    def slow_due(self, now=None):
        """True if a leftover head with no gate pass has not been re-gated yet, or not for
        BACKSTOP seconds. A blocked head is re-gated by its own event (the CI hook), so a
        re-gate on every poll would only hold the lock against that event."""
        now = time.time() if now is None else now
        return any(now - self.tried.get((n, h), -BACKSTOP) >= BACKSTOP for n, h, _ in self.regate())

    def mark_tried(self, now=None):
        now = time.time() if now is None else now
        for n, h, _ in self.regate():
            self.tried[(n, h)] = now

    def log_text(self):
        try:
            return Path(self.g['LOG']).read_text()
        except OSError:
            return ''

    def queue(self):
        try:
            return self.g['read_queue_file']()
        except Exception:
            return []

    def cheap_check(self, p, d, gated, merge):
        """None if PR p may move to Human Review now, else the reason it may not.
        The full checks ran when it entered Staging; here only what can change since."""
        n, head = p['number'], p['head']
        t = gated.get((f'#{n}', head[:8]))
        if t is None:
            return f'head {head[:8]} has no gate pass'
        if not d or d.get('state') != 'OPEN':
            return 'not open'
        if d['headRefOid'] != head:
            return f"head moved to {d['headRefOid'][:8]}"
        if d.get('isDraft'):
            return 'is a draft'
        if d.get('baseRefName') != 'main' or self.g['stacked_base'](d.get('body') or ''):
            return 'is stacked: waits (no re-merge at promotion)'
        who = human_after(d, t)
        if who:
            return f"human comment after the gate ({', '.join(sorted(set(who)))})"
        paths = [f['path'] for f in ((d.get('files') or {}).get('nodes') or []) if f.get('path')]
        return gatechecks.body_problem(paths, d.get('body') or '') or self.served_problem(d, head) or merge(head)

    def served_problem(self, d, head):
        """None if every staging site serves the head, else why not. Only for a PR that touches served
        files (web/, app/, api/): hr-gate condition 6, strict on entry (platform #479 entered
        Human Review before staging served its head). Local reads only: the .deploy-sha file
        and git merge-base."""
        paths = [f['path'] for f in ((d.get('files') or {}).get('nodes') or []) if f.get('path')]
        if not any(gatechecks.SERVED_RE.search(x) for x in paths):
            return None
        sites = [(h, s, e) for _, h, s, e in self.g['served_all']()]  # EVERY staging site (#493)
        ok, why = gatechecks.check_served_sites(paths, True, sites, lambda s: self.g['contains'](head, s))
        return None if ok else why

    def fast_path(self, cols):
        """Fill Human Review from Staging with the cheap check only. Returns the moved PRs."""
        from . import flow
        hr = list(cols.get(board.HUMAN_REVIEW, []))
        staging = cols.get(board.STAGING, [])
        if len(hr) >= HR_TARGET or not staging:
            return []
        details = read_details(self.g['gql'], [p['number'] for p in staging])
        gated = gate_times(self.log_text())
        moved, self.left = [], []
        with self.g['ScratchRepo']() as scratch:
            main = None

            def merge(head):
                nonlocal main
                if main is None:
                    main = scratch.ensure_main()
                scratch.ensure(head, *[q['head'] for q in hr])
                return merge_problem(flow.merge_commit, scratch, main,
                                     [(q['number'], q['head']) for q in hr], head)

            for p in staging_order(staging, details, self.queue(), board.active_fast_track()):
                if len(hr) >= HR_TARGET:
                    break
                n, head = p['number'], p['head']
                why = self.cheap_check(p, details.get(n), gated, merge)
                if why:
                    say(f"#{n} stays in Staging: {why}")
                    self.left.append((n, head, why))
                    continue
                if self.dry:
                    say(f"would move #{n} Staging -> Human Review (cheap check passed at {head[:8]})")
                else:
                    board.move_card(self.g, p['item'], board.HUMAN_REVIEW)
                    self.g['log']('apply', n, head[:8], 'PASS', [])
                    from . import moves
                    moves.log_move(n, 'Staging', 'Human Review', head, note='cheap check')
                    say(f"moved #{n} Staging -> Human Review (cheap check passed at {head[:8]})")
                hr.append(p)
                moved.append(n)
        return moved

    def slow_path(self, run=None):
        """After the fast move, Human Review still short: hr-gate --apply for each leftover head
        with no gate pass (a full gate under our lock; it moves the card to Human Review when a
        slot is free, or logs a pass the next fast path uses). No pending-promote, no merge."""
        run = run or gatelock.run_held
        for n, h, _ in self.regate():
            argv = [str(BIN / 'hr-gate'), '--apply', str(n)]
            if self.dry:
                say(f"would run {' '.join(argv)} (head {h[:8]} has no gate pass)")
                continue
            rc = run(argv)
            say(f"re-gated #{n} at {h[:8]}: hr-gate --apply rc={rc}")

    def pull_path(self):
        """SWARM_PROMOTE_PULL=1: the swarm-flow pull and its advance, with no promote step.
        The work merge happens ONCE, when the pulled PR enters Staging."""
        from . import flow
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        f.step(promote=False)
        for line in f.did + f.notes:
            say('flow: ' + line)

    def main_retry_due(self, cols, now=None):
        """Staging PRs whose merge-main handoff found no worker (mainmerge.recheck held them).
        Codex on PR #26: the recheck ran only when main moved, so a held handoff waited for
        the next merge. They are retried every MAIN_RETRY s, with or without a new main."""
        from . import flow
        now = time.time() if now is None else now
        if now < self.main_retry_at:
            return []
        held = flow.held()
        return [p['number'] for p in cols.get(board.STAGING, [])
                if str((held.get(p['number']) or {}).get('why', '')).startswith('conflicts with main')
                and held[p['number']].get('head') == p.get('head')]

    def once(self, retry=False):
        """One check. Returns 'idle', 'busy' or 'promoted'. A retry after busy goes straight
        to the lock (the read under the lock is fresh), so a long busy spell costs no reads."""
        main = self.main_sha()
        main_moved = main is not None and main != self.last_main
        waiting = gatelock.queued()
        if not retry:
            cols = read_columns(self.g['gql'], max_age=IDLE_AGE)
            why = self.cascade_due(cols, main_moved) or self.deploy_due()
            if why:
                self.queue_cascade(why)
                waiting = gatelock.queued()
            from . import flow
            pull_due = needs_pull(cols, flow.load_pull())
            main_retry = self.main_retry_due(cols)
            if main_retry:
                say(f"merge-main handoff retry: {', '.join('#' + str(n) for n in main_retry)} (no worker took it)")
            if not needs_promotion(cols) and not main_moved and not waiting and not pull_due and not main_retry:
                return 'idle'
            if waiting:
                say(f"{len(waiting)} queued gate request(s): draining")
            hr, st = len(cols.get(board.HUMAN_REVIEW, [])), len(cols.get(board.STAGING, []))
            if needs_promotion(cols):
                say(f"Human Review {hr}/{HR_TARGET}, Staging {st}: promoting")
            if main_moved:
                say(f"main is at {main[:8]} (was {(self.last_main or 'unknown')[:8]}): Staging merge recheck")
        try:
            with (gatelock.gate_lock('swarm-promoted') if not self.dry else _null()):
                cols = read_columns(self.g['gql'], max_age=0)  # fresh, under the lock
                moved = self.fast_path(cols) if needs_promotion(cols) else []
                if moved:  # a free Staging slot: restock it now (the drain below runs it under our lock)
                    self.queue_cascade(f"promoted {', '.join('#' + str(n) for n in moved)}: restock Staging")
                drained = self.drain()
                if drained:  # a queued run may have moved cards: read again before the rest
                    cols, moved = read_columns(self.g['gql'], max_age=0), []  # the new read already has our moves
                if main and (main_moved or self.main_retry_due(cols)):
                    self.main_pass(cols, moved, main)
                    self.last_main = main
                    self.main_retry_at = time.time() + MAIN_RETRY
                hr = len(cols.get(board.HUMAN_REVIEW, [])) + len(moved)
                if hr < HR_TARGET and any(a[0].endswith('pending-promote') for a, _ in drained):
                    self.mark_tried()  # a queued pending-promote just ran: that was the slow path
                elif hr < HR_TARGET and self.slow_due():
                    self.mark_tried()
                    self.slow_path()
                elif hr < HR_TARGET and self.regate():
                    say('re-gate already ran for ' + ', '.join(f'#{n} at {h[:8]}' for n, h, _ in self.regate())
                        + f'; waiting for a new head or gate event (backstop {BACKSTOP}s)')
                if PULL:
                    self.pull_path()
        except gatelock.Busy as e:
            say(f"busy: {e}; retrying in {RETRY}s")
            return 'busy'
        return 'promoted'


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def limit_pause(e, now=time.time):
    """Seconds to sleep after a GitHub rate-limit error (until the reset + 30 s), or None for
    any other error. No busy retries: each would fail again and the quota stays at 0."""
    from . import gqlmeter
    if not gqlmeter.limited(e):
        return None
    reset = getattr(e, 'reset', None)
    say(f"GitHub rate limit; it resets at {gqlmeter.when(reset)}; sleeping until then (SIGUSR1 ignored)")
    return int(max(60, (reset or now() + 600) - now() + 30))


def start_spool_thread(p, interval=1.0, stop=None):
    """Serve the worker spool every second in its own thread, so a long pass or a drain
    under the lock never delays a worker's Stop-hook or MCP request."""
    import threading
    stop = stop or threading.Event()
    if not p.dry:
        from . import spool
        back = spool.reclaim()  # a restart killed every serve process of the old daemon
        if back:
            say(f"spool: {len(back)} request(s) claimed before the restart are queued again")

    def loop():
        while not stop.is_set():
            p.each_second()
            stop.wait(interval)
    t = threading.Thread(target=loop, name='spool', daemon=True)
    t.start()
    return t


def run(p, poll=POLL, retry=RETRY, sleep=time.sleep):
    wake = {'now': False}
    signal.signal(signal.SIGUSR1, lambda *a: wake.update(now=True))
    say(f"swarm-promoted: polling every {poll}s (SIGUSR1 wakes it)")
    start_spool_thread(p)
    what = 'idle'
    while True:
        limited = None
        try:
            what = p.once(retry=what == 'busy')
        except Exception as e:  # keep the daemon alive; report and retry
            say(f"error: {type(e).__name__}: {e}")
            limited = limit_pause(e)
            what = 'idle' if limited else 'busy'
        pause = limited or (retry if what == 'busy' else poll)
        for _ in range(pause):
            if wake['now'] and not limited:
                wake['now'] = False
                from . import boardcache
                boardcache.invalidate()  # an event: read the board now
                break
            sleep(1)
        wake['now'] = wake['now'] and not limited


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-promoted', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    p = Promoter(dry_run=a.dry_run)
    if a.once:
        say('result: ' + p.once())
        return 0
    run(p)


if __name__ == '__main__':
    sys.exit(main())
