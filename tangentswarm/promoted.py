"""swarm-promoted: refill Human Review within seconds, with no LLM in the path.

A systemd user service. Every POLL seconds (or at once on SIGUSR1, which a webhook
receiver may send) it reads the Project 4 Status column of every PR from the board cache
(tangentswarm.boardcache: the webhook events keep it current; a sync reads the board every
SWARM_BOARD_SYNC seconds, 30 min, and logs what the events missed). An OPEN PR counts; a
merged PR whose card still says Human Review does not. When Human Review holds fewer
than 2 and Staging is not empty, it takes the one serial lock (non-blocking; if busy
it retries every RETRY seconds) and:

  1. Fast path (tangentswarm #116, Michal 2026-11-17: "This is what ready means"). Every
     Human Review check ran when the PR entered Staging, and hr-gate logged the pass with
     the head. So a move only confirms that hr-gate's latest apply line for the PR's
     CURRENT head (in the board cache) is PASS or PENDING with no failed condition. No
     merge probe, no served check, no comment read: nothing is re-checked. The columns come
     from the board cache (tangentswarm.boardcache), which the webhook events keep current,
     so there is no board read before the move. Staging PRs go in the shared pick order.
     The card moves, an hr-gate PASS line is written, and the merge clock logs the time
     from the merge event to the move (tangentswarm.moves.merge_clock). The cache is read
     again after each move, and this repeats until Human Review is full (tangentswarm #67).
     After the moves, off the critical path, one GraphQL read confirms each moved head
     (confirm); a PR whose head moved, or that became a draft, goes back to Staging.
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

from . import board, gatelock

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
POLL = int(os.environ.get('SWARM_PROMOTE_POLL', '30'))
STOP_LOG = Path.home() / '.local' / 'state' / 'swarm-flow' / 'stop-hook.log'
RETRY = int(os.environ.get('SWARM_PROMOTE_RETRY', '5'))
# An empty Human Review queues the cascade at once when it drops to 0, then again after this
# many seconds while it stays empty (the cascade escalates: NEEDS_REWORK, STALLED).
CASCADE_EMPTY_HR = int(os.environ.get('SWARM_CASCADE_EMPTY_HR', '900'))
PULL = os.environ.get('SWARM_PROMOTE_PULL') == '1'  # also run the Staging pull
REPO = 'tangentcode/platform'
BACKSTOP = int(os.environ.get('SWARM_PROMOTE_BACKSTOP', '900'))  # retry a leftover head at most this often
BOARD_EVERY = int(os.environ.get('SWARM_BOARD_EVERY', '300'))  # s between passes of a board without staging (#45)
DEPLOY_LAG = int(os.environ.get('SWARM_DEPLOY_LAG', '600'))  # s before a staging deploy lag wakes swarmbot (#37)
LAG_EVERY = 60  # s between deploy-lag checks
HR_TARGET = int(os.environ.get('SWARM_PROMOTE_HR_TARGET', os.environ.get('SWARM_HR_TARGET', '2')))  # only raise it for a dry-run proof
WATCH = os.environ.get('SWARM_WATCH', '1') != '0'  # the watchdog thread (tangentswarm.watchdog)



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
        names = boardcache.pr_labels(c)  # tangentswarm #65: the promote order sees every fast-track label
        cols.setdefault(col, []).append(dict(number=c['number'], head=c['headRefOid'], item=it['id'],
                                             closes=[i['number'] for i in closes], due=due, milestone=ms,
                                             labels=names, own_labels=_names(c), ft=board.is_fast_track(names),
                                             draft=bool(c.get('isDraft'))))
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


class Promoter:
    def __init__(self, g=None, dry_run=False):
        import runpy
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_promoted')
        self.dry = dry_run
        self.left = []    # [(n, head, why)] Staging PRs the last fast path did not move
        self.tried = {}   # {(n, head): time} the slow path last ran for this leftover head
        self.last_main = None  # main SHA the last main-merge recheck ran for
        self.last_hr = None    # Human Review count at the last check
        self.cascade_at = 0.0  # when this daemon last queued a cascade for an empty Human Review
        self.last_deploy = None  # the staging .deploy-sha values at the last check
        self.work_seen = (None, 0.0)  # (origin work SHA, when this daemon first saw it)
        self.lag_woken = set()  # work SHAs whose lag already woke swarmbot (once per SHA)
        self.lag_checked = 0.0
        self.p6_retry_at = 0.0
        self.events = None       # tangentswarm.events reader (made on first use)
        self.board, self.hr_count = None, 0  # fast_path's latest board read and Human Review slots in use

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
        main moved (the cascade checks only the TOP Ready PR; tangentswarm #111: a merge of a
        PR its pre-merge was built on keeps that pre-merge, and the rest of Ready is not
        re-checked). There is no rule that Human Review must be empty: a promote fills each
        free slot, and each promote queues the cascade that pulls the next Ready PR."""
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

    def work_sha(self):
        """origin work's SHA via git ls-remote (no API quota), or None on an error."""
        try:
            r = self.g['sh']('git', 'ls-remote', self.g['origin_url'](), 'refs/heads/work', check=False)
            sha = (r.stdout or '').split()[0] if r.returncode == 0 and r.stdout.strip() else ''
            return sha if len(sha) == 40 else None
        except Exception:
            return None

    def deploy_lag(self, now=None, wake=None):
        """tangentswarm #37 (it replaces part of the deleted swarm tick): when a staging
        .deploy-sha has not reached origin work for DEPLOY_LAG s, one swarm-webhook exception
        wake per work SHA. Checked every LAG_EVERY s: one git ls-remote and file reads, no
        GraphQL. Returns the lagging sites, or []."""
        now = time.time() if now is None else now
        if now - self.lag_checked < LAG_EVERY:
            return []
        self.lag_checked = now
        work = self.work_sha()
        if not work:
            return []
        if work != self.work_seen[0]:
            self.work_seen = (work, now)
            return []
        try:
            sites = self.g['served_all']()
        except Exception:
            return []
        behind = [(k, host, sha) for k, host, sha, _ in sites if sha != work]
        age = now - self.work_seen[1]
        if not behind or age < DEPLOY_LAG or work in self.lag_woken:
            return []
        self.lag_woken = {work}  # Codex on PR #41: once per work SHA, not again after the dedupe window
        why = (f"staging deploy lag: origin work {work[:8]} is {int(age // 60)} min old and not served by "
               + ', '.join(f"{host} ({(sha or '-')[:8]})" for _, host, sha in behind))
        if wake is None:
            from . import webhook
            wake = webhook.wake_exception
        note = wake(None, why, key=f'deploy-lag:{work}', source='swarm-promoted')
        say(note)
        return behind

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
        self.retry_p6launch()
        # tangentswarm #31: GitHub events instead of the mn-workflow timer and the Codex poll
        try:
            if self.events is None:
                self.events = events.Events()
            if events.pending() or self.events.mn_pending:
                for line in self.events.poll():
                    say(f"event: {line}")
        except Exception as e:
            say(f"events: {type(e).__name__}: {e}")

    def retry_p6launch(self, now=None, retry=None):
        """Every p6launch.RETRY_EVERY s: send again the MethBot exceptions of Project 6
        launches whose POST failed (Codex on PR #108). Never raises."""
        from . import p6launch
        now = time.time() if now is None else now
        if now - self.p6_retry_at < p6launch.RETRY_EVERY:
            return
        self.p6_retry_at = now
        try:
            sent, left = (retry or p6launch.retry_pending)()
            if sent or left:
                say(f"p6launch: {sent} kept MethBot exception(s) sent, {left} still kept")
        except Exception as e:
            say(f"p6launch retry: {type(e).__name__}: {e}")

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
            give = flow.Flow(dry_run=self.dry, out=lambda l: None, g=self.g).hand_off  # (ok, note); now or waiting
            return mainmerge.recheck(staging, main, clean, lambda item, col, n=None: board.move_card(self.g, item, col, n=n), say,
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

    def head_check(self, p, gated):
        """None if Staging PR p may move to Human Review now, else why not. tangentswarm #116
        (Michal 2026-11-17: "they should have been checked before they went into ready. This is
        what ready means."): every Human Review check ran when the PR entered Staging, and
        hr-gate logged the pass with the head. A move only confirms that the head in the
        board cache is still the gated head. No other check runs here."""
        n, head = p['number'], p['head'] or ''
        if (f'#{n}', head[:8]) not in gated:
            return f'head {head[:8]} has no gate pass'
        if p.get('draft'):  # the cache follows converted_to_draft, and confirm() writes what it found
            return 'is a draft'
        return None

    def fast_path(self, cols, reread=None):
        """Fill Human Review from Staging. Returns the moved PRs. tangentswarm #116: the
        columns come from the board cache that the events keep current (no board read), and
        each move only confirms the gated head (head_check). The pass goes on while Human
        Review has a free slot and Staging has a candidate (tangentswarm #67: a merge during
        the pass is filled in the same pass); after each move the cache is read again
        (reread). A PR moved in this pass counts in Human Review even when the cache does not
        show it there yet. After the moves, off the critical path, confirm() asks GitHub."""
        from . import moves
        self.board, self.hr_count = cols, len(cols.get(board.HUMAN_REVIEW, []))
        if self.hr_count >= HR_TARGET or not cols.get(board.STAGING):
            return []
        reread = reread or (lambda: read_columns(self.g['gql']))
        gated = gate_times(self.log_text())
        moved, done = [], {}  # done: {n: the Staging card moved in this pass}
        while True:
            hr = list(cols.get(board.HUMAN_REVIEW, []))
            # A moved PR the read still shows in Staging is cache lag: it counts in Human
            # Review. One in no column is no longer open (merged during the pass): its slot
            # is free (Codex on PR #68).
            lag = {q['number'] for q in cols.get(board.STAGING, [])}
            hr += [q for n, q in done.items() if n in lag]
            staging = [q for q in cols.get(board.STAGING, []) if q['number'] not in done]
            self.board, self.hr_count = cols, len(hr)
            if len(hr) >= HR_TARGET or not staging:
                break
            order = staging_order(staging, {}, self.queue(), board.active_fast_track())
            self.left, pick = [], None
            for p in order:
                why = self.head_check(p, gated)
                if why:
                    say(f"#{p['number']} stays in Staging: {why}")
                    self.left.append((p['number'], p['head'], why))
                    continue
                pick = p
                break
            if pick is None:
                break
            n, head = pick['number'], pick['head']
            if self.dry:
                say(f"would move #{n} Staging -> Human Review (gate pass at {head[:8]})")
            else:
                board.move_card(self.g, pick['item'], board.HUMAN_REVIEW, n=n)
                for line in moves.merge_clock([n]):  # tangentswarm #116: event -> move time
                    say(line.split('\t', 2)[2].replace('\t', ' '))
                self.g['log']('apply', n, head[:8], 'PASS', [])
                moves.log_move(n, 'Staging', 'Human Review', head, note='gate pass at this head')
                say(f"moved #{n} Staging -> Human Review (gate pass at {head[:8]})")
            done[n] = pick
            moved.append(n)
            try:  # the cache again: a merge during this pass frees one more slot
                cols = reread()
            except Exception as e:  # go on with the last read (this pass's moves are in done)
                say(f"re-read after #{n} failed: {type(e).__name__}: {e}; the last read is used")
        if done and not self.dry:
            self.confirm(done)
        return moved

    def confirm(self, done):
        """tangentswarm #116: after the moves (off the critical path), ONE GraphQL read of the
        moved PRs. A PR that is open but no longer at the head it moved at, or that became a
        draft, goes back to Staging (its new head needs its own gate). Returns the PRs sent
        back. A failed read is logged; the next sync shows any difference."""
        owner, name = REPO.split('/')
        parts = ' '.join(f'p{int(n)}: pullRequest(number:{int(n)}){{number state isDraft headRefOid}}' for n in done)
        try:
            data = self.g['gql'](f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
        except Exception as e:
            say(f"confirm: GitHub read failed ({type(e).__name__}: {str(e)[:120]}); the moves stand")
            return []
        back = []
        for n, p in done.items():
            d = (data or {}).get(f'p{int(n)}') or {}
            if d.get('state') != 'OPEN':
                continue
            why = (f"head is {(d.get('headRefOid') or '-')[:8]}, not {p['head'][:8]}" if d.get('headRefOid') != p['head']
                   else 'is a draft' if d.get('isDraft') else None)
            if not why:
                continue
            from . import boardcache, moves
            # Codex on PR #117: the cache gets what GitHub said, so the next pass does not
            # pick the same PR again (a new head has no gate pass; a draft is refused). A
            # ready_for_review or synchronize event clears it the usual way.
            boardcache.patch({'op': 'pr', 'number': n,
                              'set': {'headRefOid': d.get('headRefOid'), 'isDraft': bool(d.get('isDraft'))}})
            try:
                board.move_card(self.g, p['item'], board.STAGING, n=n)
            except Exception as e:
                say(f"confirm: #{n} {why}; the move back to Staging failed: {type(e).__name__}: {str(e)[:120]}")
                continue
            moves.log_move(n, 'Human Review', 'Staging', d.get('headRefOid') or '', note=f'confirm: {why}')
            say(f"confirm: #{n} {why}: moved back to Staging")
            back.append(n)
        return back

    def fill(self, cols):
        """(latest board, moved PRs, Human Review slots in use) after the fast path, when
        Human Review needs it. The count is from fast_path's last board read (Codex on PR #68:
        a PR moved in this pass that merged since does not hold a slot)."""
        from . import moves
        start = time.time()
        if not needs_promotion(cols):
            if not self.dry:  # tangentswarm #116: a merge that frees no move still gets its clock line
                moves.merge_clock([], upto=start)
            return cols, [], len(cols.get(board.HUMAN_REVIEW, []))
        self.board = None
        moved = self.fast_path(cols)
        if not moved and not self.dry:
            moves.merge_clock([], upto=start)
        if self.board is None:  # no board from the fast path: count as before
            return cols, moved, len(cols.get(board.HUMAN_REVIEW, [])) + len(moved)
        return self.board, moved, self.hr_count

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

    def board_passes(self, now=None):
        """Every BOARD_EVERY s, queue the pass of each board without staging (#45: Project 6):
        its MinBot checks and promote, for what no event finished (Codex on PR #47: a promote
        that met GitHub's mergeable UNKNOWN had no retry). Returns the boards queued."""
        now = time.time() if now is None else now
        if now - getattr(self, 'boards_at', 0.0) < BOARD_EVERY:
            return []
        self.boards_at = now
        out = []
        for b in board.BOARDS:
            if not b.staging:
                gatelock.enqueue([str(BIN / 'swarm-advance'), '--promote-board', str(b.number), '--board-pass'],
                                 by='swarm-promoted')
                out.append(b.number)
        return out

    def deploy_pass(self, now=None, check=None):
        """tangentswarm #113: every deploy.PASS_EVERY s, start the deploy of tangentswarm main
        when the live checkout lacks it and no deploy of it was tried (a merge event lost
        during a restart). One git ls-remote, no GraphQL. Returns the note or None."""
        from . import deploy
        now = time.time() if now is None else now
        if now - getattr(self, 'deploy_at', 0.0) < deploy.PASS_EVERY:
            return None
        self.deploy_at = now
        try:
            note = (check or deploy.pass_check)()
        except Exception as e:
            note = f"deploy pass failed: {type(e).__name__}: {str(e)[:160]}"
        if note:
            say(note)
        return note

    def side_work(self, cols):
        """(main SHA, main moved, queued requests): the work of a check that is not a card
        move: git ls-remote of main, the board passes, the deploy-lag check, and the cascade
        when it is due. It runs after the move (tangentswarm #116)."""
        main = self.main_sha()
        main_moved = main is not None and main != self.last_main
        if not self.dry:
            self.board_passes()
            self.deploy_lag()
            self.deploy_pass()
        why = self.cascade_due(cols, main_moved) or self.deploy_due()
        if why:
            self.queue_cascade(why)
        if main_moved:
            say(f"main is at {main[:8]} (was {(self.last_main or 'unknown')[:8]}): Staging merge recheck")
        return main, main_moved, gatelock.queued()

    def once(self, retry=False):
        """One check. Returns 'idle', 'busy' or 'promoted'. A retry after busy goes straight
        to the lock. tangentswarm #116: the columns come from the event-fed board cache, and
        when Human Review has a free slot the move runs FIRST: the main SHA (git ls-remote),
        the board passes, the deploy-lag check and the cascade check run after it."""
        main = main_moved = None
        waiting = gatelock.queued()
        if not retry:
            cols = read_columns(self.g['gql'])  # tangentswarm #116: the event-fed cache
            if needs_promotion(cols):
                hr, st = len(cols.get(board.HUMAN_REVIEW, [])), len(cols.get(board.STAGING, []))
                say(f"Human Review {hr}/{HR_TARGET}, Staging {st}: promoting")
            else:
                main, main_moved, waiting = self.side_work(cols)
                from . import flow
                pull_due = needs_pull(cols, flow.load_pull())
                if not main_moved and not waiting and not pull_due:
                    return 'idle'
                if waiting:
                    say(f"{len(waiting)} queued gate request(s): draining")
        try:
            with (gatelock.gate_lock('swarm-promoted') if not self.dry else _null()):
                cols = read_columns(self.g['gql'])  # the cache: our own moves and the events patch it
                cols, moved, hr = self.fill(cols)
                if moved:  # a free Staging slot: restock it now (the drain below runs it under our lock)
                    self.queue_cascade(f"promoted {', '.join('#' + str(n) for n in moved)}: restock Staging")
                if main is None:  # after the move (tangentswarm #116)
                    main, main_moved, _ = self.side_work(cols)
                drained = self.drain()
                if drained:  # a queued run may have moved cards: read again before the rest
                    cols = read_columns(self.g['gql'])  # the cache already has the drained moves
                    # tangentswarm #67: a slot freed during the drain (a merge) is filled in this pass
                    cols, moved, hr = self.fill(cols)
                    if moved:
                        self.queue_cascade(f"promoted {', '.join('#' + str(n) for n in moved)}: restock Staging")
                if main and main_moved:
                    self.main_pass(cols, moved, main)
                    self.last_main = main
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
    if WATCH:
        from . import watchdog
        watchdog.start_thread(p)  # tangentswarm #40: the pane watcher and the watchdog checks
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
                wake['now'] = False  # an event: the webhook has patched the cache (tangentswarm #116)
                break
            sleep(1)
        wake['now'] = wake['now'] and not limited


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-promoted', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    from . import eventdb
    eventdb.set_source('daemon')
    if not a.dry_run:
        from . import launches
        moved = launches.migrate_held(warn=say)
        if moved:
            say('held-launch list retired: unstarted records for ' + ', '.join(f'#{n}' for n in moved))
        from . import flow
        cleared = flow.clear_held()  # tangentswarm #101: no parking; the first passes find each conflict again
        if cleared:
            say('parking retired: cleared held.json (' + ', '.join(f'#{n}' for n in cleared)
                + '); each conflict goes to In Progress on the next pass')
    p = Promoter(dry_run=a.dry_run)
    if a.once:
        say('result: ' + p.once())
        return 0
    run(p)


if __name__ == '__main__':
    sys.exit(main())
