"""swarm-watch: the swarm's watchdog in swarm-promoted (tangentswarm #40), no LLM.

It replaces the deleted LLM Auto Review watchdog and agent stall scan. Once a minute (a
thread of the swarm-promoted daemon):

  - the pane watcher (tangentswarm.panewatch): capture, compare, classify, act
  - a new Needs Input card pings Michal in MethBot's chat (item 4c)
  - Human Review empty while Staging holds PRs for PROMOTE_GRACE: one alert to Michal with
    the fast path's reason for each Staging PR and a suggested fix (item 4a)
  - hung test or automation processes in worker worktrees are killed, and swarmbot is
    woken to diagnose with the process facts and the pane capture (item 4d)
Every CODEX_EVERY s: a PR in Auto Review whose "@codex review" got no Codex answer for
CODEX_SILENT s, or got a usage-limit answer, is a Codex outage: ONE alert per outage (item 3).
Every SWEEP_EVERY s (item 6): issue cards with no PR in a PR column go to Backlog; a Done
PR whose head is not on main leaves Done (open or merged elsewhere: In Progress; closed
without a merge: Canceled).

    swarm-watch --once [--dry-run]    one pass (all checks) and exit
    swarm-watch --panes               the latest capture of every pane, with its class
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import board

HOME = Path.home()
REPO = 'tangentcode/platform'
PLATFORM_GIT = HOME / 'ver' / 'platform'
CODEX_EVERY = int(os.environ.get('SWARM_CODEX_CHECK', '300'))
CODEX_SILENT = int(os.environ.get('SWARM_CODEX_SILENT', '1800'))
SWEEP_EVERY = int(os.environ.get('SWARM_SWEEP_EVERY', '3600'))
PROMOTE_GRACE = int(os.environ.get('SWARM_PROMOTE_GRACE', '600'))
SNAP_AGE = int(os.environ.get('SWARM_WATCH_SNAP_AGE', '120'))
CODEX = 'chatgpt-codex-connector'
LIMIT_RE = re.compile(r'usage limits?', re.I)
ISSUE_SWEEP_COLS = (board.ACTIVE, board.IN_PROGRESS, board.AUTO_REVIEW, board.READY, board.STAGING,
                    board.HUMAN_REVIEW)


def say(line):
    print(time.strftime('%H:%M:%S ') + 'watch: ' + line, flush=True)


def _ts(iso):
    try:
        return datetime.fromisoformat((iso or '').replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


# -- Codex outage (item 3) -----------------------------------------------------------------

CODEX_Q = ('query{{repository(owner:"tangentcode",name:"platform"){{p: pullRequest(number:{n}){{headRefOid '
           'comments(last:40){{nodes{{author{{login}} body createdAt}}}} '
           'reviews(last:20){{nodes{{author{{login}} submittedAt commit{{oid}}}}}}}}}}}}')


def names_head(body, head):
    """True when a Codex comment is a result for this head: a review-state summary whose
    commit_sha is the head, or a receipt that names the head's short SHA."""
    m = re.search(r'codex-review-state:v1\s*(\{.*?\})\s*-->', body or '', re.S)
    if m:
        try:
            return (json.loads(m.group(1)).get('code_review') or {}).get('commit_sha') == head
        except ValueError:
            return False
    return bool(head) and head[:8] in (body or '')


def codex_facts(full):
    """{req, reply, limit}: the time of the latest "@codex review" by a person, whether Codex
    answered it for the CURRENT head (Codex on PR #51: a late review of an older head is no
    answer), and whether Codex sent a usage-limit notice after it (whatever head)."""
    def who(x):
        return ((x.get('author') or {}).get('login') or '')
    req = None
    for c in (full.get('comments') or {}).get('nodes') or []:
        if (c.get('body') or '').strip().lower() == '@codex review' and not who(c).endswith('[bot]'):
            t = _ts(c.get('createdAt'))
            req = max(req or t, t) if t else req
    if req is None:
        return {'req': None, 'reply': False, 'limit': False}
    from .webhook import codex_running
    head = full.get('headRefOid') or ''
    after = [c for c in (full.get('comments') or {}).get('nodes') or []
             if who(c).startswith(CODEX) and (_ts(c.get('createdAt')) or 0) > req
             and not codex_running(c.get('body'))]  # Codex on PR #51: "running" is not an answer
    reviews = [r for r in (full.get('reviews') or {}).get('nodes') or []
               if who(r).startswith(CODEX) and (_ts(r.get('submittedAt')) or 0) > req
               and (r.get('commit') or {}).get('oid') == head]
    return {'req': req, 'reply': bool(reviews or any(names_head(c.get('body'), head) for c in after)),
            'limit': any(LIMIT_RE.search(c.get('body') or '') for c in after)}


def codex_wait(facts, now, silent=CODEX_SILENT):
    """('limit' | 'silent', detail) for a PR whose Codex review is out, else None."""
    if facts.get('limit'):
        return 'limit', 'Codex answered with a usage-limit notice'
    if facts.get('req') and not facts.get('reply') and now - facts['req'] >= silent:
        return 'silent', f"no Codex answer for {int((now - facts['req']) // 60)} min"
    return None


def outage_update(st, found, now):
    """One alert per outage. found: {pr: (kind, detail)}. Returns the alert text when an
    outage starts, else None. An outage ends when no PR waits."""
    cur = st.data.get('codex_outage')
    if not found:
        if cur:
            st.data.pop('codex_outage', None)
            st.save()
        return None
    prs = sorted(found)
    if cur:
        cur['prs'] = prs
        return None
    st.data['codex_outage'] = {'since': now, 'prs': prs}
    st.save()
    lines = '; '.join(f"#{n}: {d}" for n, (_, d) in sorted(found.items()))
    kind = 'usage limit' if any(k == 'limit' for k, _ in found.values()) else 'no answer'
    return (f"Codex outage ({kind}): {len(prs)} PR(s) wait for a Codex review: {lines}. "
            "One alert for this outage; the next one comes after Codex answers again.")


# -- Human Review empty while Staging is not (item 4a) ---------------------------------------

FIXES = [
    (r'has no gate pass|head moved', 'run hr-gate --apply {n} (the daemon slow path runs it once per head)'),
    (r'human comment', 'answer the comment on #{n}, then hr-gate --apply {n}'),
    (r'draft', 'mark #{n} ready (gh pr ready {n})'),
    (r'stacked', 'merge the base of #{n} first, or move it onto main'),
    (r'conflict|merge', 'send #{n} a merge-main relay (swarm-advance --cascade does it)'),
    (r'serve|deploy|staging', 'redeploy staging so it serves the head of #{n}'),
    (r'screenshot|where to see|7a|7b', 'the worker of #{n} adds the screenshots or the where-to-see line'),
    (r'not open', '#{n} is closed: move its card out of Staging'),
]


def fix_for(n, why):
    for rx, fix in FIXES:
        if re.search(rx, why or '', re.I):
            return fix.format(n=n)
    return 'swarmbot diagnoses'


def promote_stall(hr, staging, left, since, now, grace=None):
    """(text or None, since). hr: Human Review count; staging: Staging PR numbers; left:
    {n: why} from the daemon's last fast path. The text is the alert with a diagnosis."""
    if hr or not staging:
        return None, None
    since = since or now
    if now - since < (PROMOTE_GRACE if grace is None else grace):
        return None, since
    rows = []
    for n in sorted(staging):
        why = left.get(n) or 'no fast-path result (the gate lock was busy, or a GitHub limit)'
        rows.append(f"#{n}: {why} -> fix: {fix_for(n, why) if n in left else 'check swarm-promoted (journalctl --user -u swarm-promoted)'}")
    return (f"Human Review is empty and Staging holds {len(staging)} PR(s) for {int((now - since) // 60)} min: "
            "promote stalled. " + ' | '.join(rows)), since


# -- sweeps (item 6) -------------------------------------------------------------------------

def issue_sweep(snap, hold=()):
    """[(n, item, col)] of open issue cards in a PR column that no open PR closes (hold:
    issues being launched now)."""
    from . import boardcache, slots
    taken = slots.taken_issues(snap)
    out = []
    for it, c in boardcache.open_issues(snap):
        col = boardcache.col(it)
        if col in ISSUE_SWEEP_COLS and c['number'] not in taken and c['number'] not in hold:
            out.append((c['number'], it['id'], col))
    return out


def launching_issues(st, now):
    """Issues with a launch in flight (launching, unstarted, or named in the last 10 min)."""
    hold = {int(k) for k in (st.get('launching') or {})}
    hold |= {int(r['issue']) for r in (st.get('unstarted') or {}).values() if r.get('issue')}
    hold |= {int(k) for k, t in (st.get('named') or {}).items() if now - float(t) < 600}
    return hold


CLOSED_Q = ('query($search:String!){search(query:$search,type:ISSUE,first:50){nodes{...on PullRequest{number state merged '
            'baseRefName headRefOid projectItems(first:10){nodes{id project{id} '
            'fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}}}}')


def done_target(state, merged, base, on_main):
    """Where a Done card goes, or None to stay: open, or merged off main -> In Progress;
    closed unmerged -> Canceled; merged into main with the head not on main -> In Progress."""
    if state == 'OPEN':
        return board.IN_PROGRESS
    if not merged:
        return board.CANCELED
    if base != 'main':
        return board.IN_PROGRESS
    return None if on_main in (True, None) else board.IN_PROGRESS


def head_on_main(head, n, git=None):
    """True/False: is head an ancestor of GitHub's main; None when it cannot be told. Only a
    yes is trusted from the local origin/main: a no (or a missing object) is checked again
    after a fetch of main and the PR head (Codex on PR #51: a stale origin/main sent a
    merged PR back to In Progress). A failed fetch is None: the card stays."""
    def run(*args):
        return subprocess.run(['git', '-C', str(PLATFORM_GIT), *args], capture_output=True, text=True, timeout=120)
    git = git or run
    if git('merge-base', '--is-ancestor', head, 'origin/main').returncode == 0:
        return True
    if git('fetch', '-q', 'origin', 'main', f'pull/{int(n)}/head').returncode != 0:
        return None
    r = git('merge-base', '--is-ancestor', head, 'origin/main')
    return r.returncode == 0 if r.returncode in (0, 1) else None


# -- the runner ------------------------------------------------------------------------------

class Watchdog:
    def __init__(self, g=None, promoter=None, dry=False, out=say, st=None, panes=None, alert=None,
                 wake=None, incident=None, move=None, kill=None, hung_sample=None, on_main=head_on_main):
        from . import alerts, hung, panewatch
        self._g, self.promoter, self.dry, self.out = g, promoter, dry, out
        self.st = st or alerts.state()
        self.alert = alert or alerts.tell_michal
        self.wake = wake or alerts.wake_swarmbot
        self.incident = incident or (lambda *a, **k: alerts.incident(*a, st=self.st, **k))
        self.panes = panes or panewatch.PaneWatch(dry=dry, st=self.st, wake=self.wake, incident=self.incident)
        self.move = move or (lambda item, col: board.move_card(self.g, item, col))
        self.kill = kill or hung.kill
        self.hung_sample = hung_sample or (lambda: hung.sample(worker_root))
        self.on_main = on_main
        self.procs = {}
        self.stall_since = None
        self.last_codex = 0.0
        self.last_sweep = 0.0

    @property
    def g(self):
        if self._g is None:
            if self.promoter is not None:
                self._g = self.promoter.g
            else:
                from .advance import hr_gate
                self._g = hr_gate()
        return self._g

    def step(self, name, fn, *a):
        try:
            for line in fn(*a) or []:
                self.out(line)
        except Exception as e:  # one check failing never stops the others
            self.out(f"{name}: {type(e).__name__}: {str(e)[:300]}")

    def once(self, now=None):
        from . import boardcache
        now = time.time() if now is None else now
        snap = boardcache.snapshot(self.g['gql'], SNAP_AGE)
        self.step('panes', self.pane_pass, snap, now)
        self.step('needs-input', self.needs_input_pass, snap)
        self.step('promote', self.promote_pass, snap, now)
        self.step('hung', self.hung_pass, now)
        if now - self.last_codex >= CODEX_EVERY:
            self.last_codex = now
            self.step('codex', self.codex_pass, snap, now)
        if now - self.last_sweep >= SWEEP_EVERY:
            self.last_sweep = now
            self.step('sweep', self.sweep_pass, snap, now)

    # each check returns the lines to log
    def pane_pass(self, snap, now):
        from . import boardcache
        prs = {c['headRefName']: dict(number=c['number'], col=boardcache.col(it), draft=c.get('isDraft'),
                                      head=c.get('headRefOid'))
               for it, c in boardcache.open_prs(snap)}
        return self.panes.once(prs, now)

    def needs_input_pass(self, snap):
        from . import boardcache, needsinput
        cards = {c['number']: c.get('title') or '' for it, c in boardcache.open_prs(snap) + boardcache.open_issues(snap)
                 if boardcache.col(it) == board.NEEDS_INPUT}
        if self.dry:
            return []
        return [f"pinged Michal: #{n} is new in Needs Input"
                for n in needsinput.new_cards(self.st, cards, self.alert, titles=cards)]

    def promote_pass(self, snap, now):
        from . import boardcache
        cols = {}
        for it, c in boardcache.open_prs(snap):
            cols.setdefault(boardcache.col(it), []).append(c['number'])
        left = {n: why for n, _, why in (getattr(self.promoter, 'left', None) or [])}
        text, self.stall_since = promote_stall(len(cols.get(board.HUMAN_REVIEW, [])),
                                               cols.get(board.STAGING, []), left, self.stall_since, now)
        if not text:
            return []
        key = 'promote-stall:' + ','.join(str(n) for n in sorted(cols.get(board.STAGING, [])))
        if self.dry:
            return [f"would alert Michal: {text}"]
        if self.alert(text, key=key, st=self.st):
            self.incident(key, 'Human Review empty while Staging holds PRs', text)
            return [f"alerted Michal: {text}"]
        return []

    def hung_pass(self, now):
        from . import hung
        self.procs = hung.track(self.procs, self.hung_sample(), now)
        out = []
        for pid, why in hung.hung(self.procs, now):
            p = self.procs.pop(pid)
            pane = next((x for x in self.panes.latest.values() if (x.get('wt') or {}).get('top') == p['cwd']), None)
            facts = dict(pid=pid, cmd=p['cmd'], cwd=p['cwd'], why=why)
            if self.dry:
                out.append(f"would kill hung pid {pid} ({p['cmd'][:80]}): {why}")
                continue
            killed = self.kill(pid)
            facts['killed'] = killed
            capture = '\n'.join(((pane or {}).get('screen') or '').rstrip('\n').split('\n')[-60:])
            what = f"hung process killed in {p['cwd']}: pid {pid} {p['cmd'][:160]} ({why})"
            self.wake(what, f"hung:{pid}:{int(p['started'])}", pr=(pane or {}).get('pr'),
                      detail=dict(facts, pane=(pane or {}).get('id'), capture=capture))
            self.incident(f"hung:{pid}:{int(p['started'])}", f"hung process in {Path(p['cwd']).name}",
                          f"{what}.\n\nPane capture:\n```\n{capture[-3000:]}\n```",
                          fix=f"The watcher killed pids {killed}; swarmbot diagnoses why it hung.")
            out.append(f"killed hung pid {pid} ({p['cmd'][:80]}): {why}; woke swarmbot to diagnose")
        return out

    def codex_pass(self, snap, now):
        from . import boardcache
        found = {}
        for it, c in boardcache.open_prs(snap):
            if boardcache.col(it) != board.AUTO_REVIEW:
                continue
            n = c['number']
            facts = boardcache.detail(f"codex-wait:{n}", c.get('headRefOid'), c.get('updatedAt'),
                                      lambda: codex_facts(((self.g['gql'](CODEX_Q.format(n=int(n))) or {})
                                                           .get('repository') or {}).get('p') or {}))
            w = codex_wait(facts, now)
            if w:
                found[n] = w
        if self.dry:  # Codex on PR #51: a dry run must not record the outage as announced
            import copy
            import types
            scratch = types.SimpleNamespace(data=copy.deepcopy(self.st.data), save=lambda: None)
            text = outage_update(scratch, found, now)
            return [f"would alert Michal: {text}"] if text else []
        text = outage_update(self.st, found, now)
        if not text:
            return []
        key = f"codex-outage:{int(now)}"
        self.alert(text, key=key, st=self.st)
        self.incident(key, 'Codex outage', text)
        return [f"alerted Michal: {text}"]

    def sweep_pass(self, snap, now):
        from . import boardcache, slots
        out = []
        for n, item, col in issue_sweep(snap, launching_issues(slots.load(), now)):
            out.append(self.sweep_move(n, item, col, board.BACKLOG, 'an issue card with no PR'))
        for it, c in boardcache.open_prs(snap):
            if boardcache.col(it) == board.DONE:
                out.append(self.sweep_move(c['number'], it['id'], board.DONE, board.IN_PROGRESS,
                                           'an open PR in Done'))
        since = time.strftime('%Y-%m-%d', time.gmtime(now - 3 * 86400))
        d = self.g['gql'](CLOSED_Q, search=f"repo:{REPO} is:pr is:closed closed:>={since}")
        for pr in ((d or {}).get('search') or {}).get('nodes') or []:
            item = next((x for x in (pr.get('projectItems') or {}).get('nodes') or []
                         if (x.get('project') or {}).get('id') == board.PROJECT_ID), None)
            if not item or board.column(item.get('fieldValueByName')) != board.DONE:
                continue
            on = self.on_main(pr['headRefOid'], pr['number']) if pr.get('merged') and pr.get('baseRefName') == 'main' \
                else None
            dest = done_target(pr['state'], pr.get('merged'), pr.get('baseRefName'), on)
            if dest:
                why = ('closed without a merge' if not pr.get('merged') else
                       f"merged into {pr.get('baseRefName')}" if pr.get('baseRefName') != 'main' else
                       f"head {pr['headRefOid'][:8]} is not on main")
                out.append(self.sweep_move(pr['number'], item['id'], board.DONE, dest, f"a Done PR {why}"))
        return out

    def sweep_move(self, n, item, frm, to, why):
        if self.dry:
            return f"would move #{n} {frm} -> {to} ({why})"
        self.move(item, to)
        from . import alerts
        alerts.record('sweep', f"#{n} {frm} -> {to}: {why}", pr=n)
        self.incident(f"sweep:{n}:{frm}:{to}", f"#{n} card in {frm}: {why}",
                      f"The sweep found #{n} in {frm}: {why}.", fix=f"The sweep moved the card to {to}.")
        return f"#{n} {frm} -> {to} ({why})"


def worker_root(cwd):
    """The worker worktree ~/ver/platform.<slug> that holds cwd, or None."""
    from . import slots
    p = Path((cwd or '').removesuffix(' (deleted)'))
    for q in (p, *p.parents):
        if slots.is_platform_worktree(q):
            return str(q)
    return None


def start_thread(promoter, interval=None, stop=None):
    """The watchdog in its own thread of swarm-promoted: one pass a minute."""
    from . import panewatch
    interval = interval or panewatch.INTERVAL
    stop = stop or threading.Event()
    w = Watchdog(promoter=promoter, dry=promoter.dry)

    def loop():
        while not stop.is_set():
            try:
                w.once()
            except Exception as e:
                say(f"error: {type(e).__name__}: {e}")
            stop.wait(interval)
    t = threading.Thread(target=loop, name='watchdog', daemon=True)
    t.start()
    return t


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-watch', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--once', action='store_true', help='one pass of every check, then exit')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--panes', action='store_true', help='print the latest pane captures and classes')
    a = ap.parse_args(argv)
    if a.panes:
        from . import panewatch
        rep = panewatch.read_report()
        for pid, p in sorted(rep.get('panes', {}).items()):
            print(f"{pid} {p.get('window')}: {p.get('cls')} pr={p.get('pr')} "
                  f"unchanged={int(p.get('unchanged_s') or 0)}s")
        return 0
    if not a.once:
        ap.error('use --once or --panes (the loop runs in swarm-promoted)')
    w = Watchdog(dry=a.dry_run)
    w.last_codex = w.last_sweep = 0.0
    w.once()
    return 0


if __name__ == '__main__':
    sys.exit(main())
