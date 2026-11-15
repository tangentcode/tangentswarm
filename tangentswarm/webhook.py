"""swarm-webhook: the deterministic GitHub hook for tangentcode/platform (tangentswarm #31).

Michal 2026-11-15 4:44 AM ET: swarmbot's GitHub activity hook woke an LLM on every platform
event and used 58% of all fleet usage. This server runs the same rules
(~/var/hook-rules-swarmbot.md) without an LLM, and wakes swarmbot only for the exceptions
it cannot handle.

  POST /github     a GitHub webhook delivery. The X-Hub-Signature-256 HMAC must match one
                   of the secrets (SECRET_FILES). The body goes to INBOX and the answer is
                   200 at once; one worker thread handles the inbox in order.
  GET  /health     "ok"

Every delivery and every decision is one JSON line in LOG, and rows in the append-only
SQLite event log (eventdb: a 'received' row before the reply, a 'handled' row after). classify() is a pure function of
(event, payload) -> [Action]; Executor runs the actions with the swarm's own tools:

  minbot   queue `swarm-advance --minbot N` (CI green, mergeable, Codex clean on the head,
           no open question; a pass moves the card to Ready and resolves addressed threads)
  cascade  set the rerun flag and queue `swarm-advance --cascade` for swarm-promoted
  codex    read the PR: findings on the head -> relay them to the worker (root-cause rule),
           card In Progress; clean -> minbot + cascade; no result yet -> nothing
  ci       a completed check suite on the PR head: red -> relay to the worker, card In
           Progress (only with a worker); green -> minbot (Auto Review) or cascade (Staging)
  handoff  a new head on a card past In Progress: mark ready, "@codex review" once per head,
           card Auto Review, cascade
  relay    a human comment on a PR in work: relay it to the worker, card In Progress
  reject / answered   Michal's card moves (Human Review -> In Progress, Needs Input -> In
           Progress): relay his comments since the head to the worker
  merged   cascade first, card Done, close each linked issue and its card Done, park the
           worker, active-gate sweeps
  closed   card Canceled
  card     a new PR or issue: Project 4 card with the right Status
  wake     an exception (EXCEPTIONS): one deduped, rate-limited POST to swarmbot's wake URL

`swarm-webhook replay --pr N [--pr M ...] [--dry]` runs the same handlers on a PR from its
current state (the Codex result and CI on the head), for events that were missed.
`swarm-webhook replay --delivery ID` runs them again on a delivery stored in the event DB.

A worker's push to a PR in In Progress is left to the Stop hook: a worker often pushes and
goes on working, so a push alone is not a handoff.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import board, config, eventdb

HOME = Path.home()
STATE = HOME / '.local' / 'state' / 'swarm-flow'
LOG = Path(os.environ.get('SWARM_WEBHOOK_LOG', str(STATE / 'webhook.log')))
INBOX = Path(os.environ.get('SWARM_WEBHOOK_INBOX', str(STATE / 'webhook-inbox')))
SEEN = Path(os.environ.get('SWARM_WEBHOOK_SEEN', str(STATE / 'webhook-seen.json')))
SECRET_FILES = ([Path(p) for p in os.environ['SWARM_WEBHOOK_SECRETS'].split(':') if p]
                if os.environ.get('SWARM_WEBHOOK_SECRETS') else config.files('webhook', 'secret_files'))
WAKE_URL_FILE = Path(os.environ.get('SWARM_WAKE_URL_FILE', str(HOME / '.config' / 'tangentswarm' / 'swarmbot-wake-url')))
WAKE_KEY_FILE = Path(os.environ.get('SWARM_WAKE_KEY_FILE', str(HOME / '.config' / 'tangentswarm' / 'swarmbot-wake-key')))
PORT = int(os.environ.get('SWARM_WEBHOOK_PORT', '8772'))
REPO = 'tangentcode/platform'
MICHAL = 'tangentstorm'
CODEX = 'chatgpt-codex-connector'
WAKE_EVERY = 60          # s between two wakes
WAKE_DEDUPE = 1800       # s: the same exception key wakes once
WAKE_PENDING_MAX = 50    # exceptions kept for the next wake
MAX_BODY = 2 * 1024 * 1024
# Logged in the event DB only: one row each, no action, never queued (Michal 1:55 PM ET).
LOG_ONLY = ('push', 'commit_comment', 'discussion', 'discussion_comment', 'pull_request_review_thread',
            'pull_request_review_comment')
# The event types classify() has rules for (a skip is a rule). Any other type is log-only too:
# a 2xx answer and one row, never an error (Michal 1:58 PM ET).
HANDLED = ('ping', 'projects_v2_item', 'issue_comment', 'pull_request_review', 'check_suite', 'workflow_run',
           'pull_request', 'issues', 'check_run', 'status', 'workflow_job', 'create', 'delete')


def log_only(event):
    return event in LOG_ONLY or event not in HANDLED
OUR_MARKS = ('<!-- swarm-needs-input -->', '### MinBot', 'MinBot pass', 'MinBot check',
             '<!-- swarm-brief', 'Coordinator: #')

ROOT_CAUSE = ("ROOT-CAUSE RULE: fix the root cause, list the neighbor cases, add tests, and say which "
              "ones they cover. Fix and push; do not touch the Codex threads and do not post on GitHub.")


@dataclass
class Action:
    kind: str
    pr: int = 0
    why: str = ''
    args: dict = field(default_factory=dict)

    def as_dict(self):
        return dict(kind=self.kind, pr=self.pr, why=self.why, **self.args)


# -- signatures, log ---------------------------------------------------------------------

def secrets(files=None):
    out = []
    for f in files or SECRET_FILES:
        try:
            s = Path(f).read_text().strip()
        except OSError:
            continue
        if s:
            out.append(s)
    return out


def signed(raw, header, keys):
    """True when X-Hub-Signature-256 matches any secret."""
    if not header or not header.startswith('sha256='):
        return False
    return any(hmac.compare_digest('sha256=' + hmac.new(k.encode(), raw, hashlib.sha256).hexdigest(), header)
               for k in keys)


def log(**rec):
    rec = dict(ts=time.strftime('%Y-%m-%dT%H:%M:%S'), **rec)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, 'a') as f:
            f.write(json.dumps(rec, default=str) + '\n')
    except OSError:
        pass


# -- the rules (pure) --------------------------------------------------------------------

def login(obj):
    return ((obj or {}).get('login') or '').lower()


def is_codex(user=None, body=''):
    return 'codex' in login(user) or "Didn't find any major issues" in (body or '') or \
        'codex-review-state' in (body or '')


def is_bot_self(user):
    return 'minbot' in login(user)


def ours(body):
    """A comment the swarm itself posted (as tangentstorm through gh): never relayed back."""
    b = body or ''
    return b.strip().lower() == '@codex review' or any(m in b for m in OUR_MARKS)


def classify(event, p):
    """[Action] for one delivery (pure: no I/O). An empty list is "skip"; the first action's
    why says why when it is a skip."""
    repo = ((p.get('repository') or {}).get('full_name') or '')
    action = p.get('action') or ''
    sender = p.get('sender') or {}
    if event == 'ping':
        return [Action('skip', why='ping')]
    if event == 'projects_v2_item':
        return project_move(p)
    if repo != REPO:
        return [Action('skip', why=f'not {REPO} ({repo or "no repo"})')]
    if log_only(event):
        return [Action('log-only', why=f'{event}: logged only (Michal 2026-11-15 1:55 PM ET)')]
    if event in ('check_run', 'status', 'workflow_job', 'create', 'delete'):
        return [Action('skip', why=f'{event}: not handled (STEP 0)')]

    if event == 'issue_comment':
        c, issue = p.get('comment') or {}, p.get('issue') or {}
        n, body, is_pr = issue.get('number'), c.get('body') or '', 'pull_request' in issue
        if action != 'created':
            return [Action('skip', why=f'issue_comment {action}')]
        if is_codex(c.get('user'), body):
            if not is_pr:
                return [Action('skip', why='Codex comment on an issue')]
            if 'codex-review-state' in body and '"status": "running"' in body.replace("'", '"'):
                return [Action('skip', pr=n, why='Codex review is running')]
            return [Action('codex', n, 'Codex comment (summary or receipt)')]
        if is_bot_self(c.get('user')) or ours(body):
            return [Action('skip', pr=n, why='the swarm\'s own comment')]
        if login(c.get('user')).endswith('[bot]'):
            return [Action('skip', pr=n, why=f"bot comment by {login(c.get('user'))}")]
        if is_pr:
            return [Action('relay', n, f"comment by {login(c.get('user'))} on PR #{n}",
                           dict(author=login(c.get('user')), body=body[:2000], url=c.get('html_url')))]
        return [Action('issue_comment', n, f"comment by {login(c.get('user'))} on issue #{n}",
                       dict(author=login(c.get('user')), body=body[:2000], url=c.get('html_url')))]

    if event == 'pull_request_review':
        r, pr = p.get('review') or {}, p.get('pull_request') or {}
        n = pr.get('number')
        if action != 'submitted':
            return [Action('skip', pr=n, why=f'review {action}')]
        if is_codex(r.get('user'), r.get('body')):
            return [Action('codex', n, f"Codex review on {(r.get('commit_id') or '')[:8]}",
                           dict(commit=r.get('commit_id')))]
        if is_bot_self(r.get('user')):
            return [Action('skip', pr=n, why='MinBot review')]
        if pr.get('draft'):
            return [Action('skip', pr=n, why='review on a draft PR')]
        if (r.get('body') or '').strip():
            return [Action('relay', n, f"review by {login(r.get('user'))}",
                           dict(author=login(r.get('user')), body=(r.get('body') or '')[:2000], url=r.get('html_url')))]
        return [Action('skip', pr=n, why='review with no body (inline comments come with it)')]

    if event in ('check_suite', 'workflow_run'):
        o = p.get(event) or {}
        prs = [x.get('number') for x in o.get('pull_requests') or [] if x.get('number')]
        if action != 'completed' or not prs:
            return [Action('skip', why=f'{event} {action} (no open PR, or not completed)')]
        return [Action('ci', n, f"{event} {o.get('conclusion')} on {(o.get('head_sha') or '')[:8]}",
                       dict(sha=o.get('head_sha'), conclusion=o.get('conclusion'))) for n in prs]

    if event == 'pull_request':
        pr = p.get('pull_request') or {}
        n, base = pr.get('number'), (pr.get('base') or {}).get('ref')
        if action in ('labeled', 'unlabeled', 'assigned', 'unassigned', 'review_requested',
                      'review_request_removed', 'milestoned', 'demilestoned', 'locked', 'unlocked'):
            return [Action('skip', pr=n, why=f'pull_request {action}')]
        if action == 'closed':
            if pr.get('merged') and base == 'main':
                return [Action('merged', n, f"#{n} merged into main", dict(head=(pr.get('head') or {}).get('sha')))]
            if pr.get('merged'):
                return [Action('wake', n, f"#{n} merged into {base}, not main", dict(key=f'merge-base:{n}'))]
            return [Action('closed', n, f"#{n} closed without a merge")]
        if action in ('opened', 'reopened'):
            if base not in ('main', 'deploy'):
                return [Action('wake', n, f"#{n} targets {base}, not main", dict(key=f'base:{n}'))]
            return [Action('card', n, f"PR #{n} {action}", dict(draft=bool(pr.get('draft')), what='pr'))]
        if pr.get('draft') and action not in ('ready_for_review',):
            return [Action('skip', pr=n, why=f'draft PR: {action}')]
        if action == 'ready_for_review':
            return [Action('handoff', n, 'ready for review', dict(head=(pr.get('head') or {}).get('sha')))]
        if action == 'synchronize':
            return [Action('handoff', n, f"new head {(pr.get('head') or {}).get('sha', '')[:8]}",
                           dict(head=(pr.get('head') or {}).get('sha'), push=True))]
        if action == 'edited':
            return [Action('cascade', n, 'PR body edited (a Staging gate may pass now)')]
        return [Action('skip', pr=n, why=f'pull_request {action}')]

    if event == 'issues':
        i = p.get('issue') or {}
        if action == 'opened':
            return [Action('card', i.get('number'), f"issue #{i.get('number')} opened", dict(what='issue'))]
        return [Action('skip', pr=i.get('number'), why=f'issues {action}')]

    return [Action('skip', why=f'{event} {action}: no rule for this action')]


def project_move(p):
    """projects_v2_item: Michal's Status moves on Project 4."""
    item = p.get('projects_v2_item') or {}
    ch = ((p.get('changes') or {}).get('field_value') or {})
    if p.get('action') != 'edited' or not ch:
        return [Action('skip', why=f"projects_v2_item {p.get('action')}")]
    # Codex on PR #26: only the board's own project and its Status field (another project's
    # move with the same column names must never reach a platform worker)
    if item.get('project_node_id') != board.PROJECT_ID:
        return [Action('skip', why=f"projects_v2_item of another project ({item.get('project_node_id')})")]
    if ch.get('field_node_id') and ch.get('field_node_id') != board.STATUS_FIELD:
        return [Action('skip', why='projects_v2_item: not the Status field')]
    frm = board.column({'optionId': (ch.get('from') or {}).get('id'), 'name': (ch.get('from') or {}).get('name')})
    to = board.column({'optionId': (ch.get('to') or {}).get('id'), 'name': (ch.get('to') or {}).get('name')})
    if login(p.get('sender')) != MICHAL:
        return [Action('skip', why=f"card move by {login(p.get('sender'))}")]
    node = item.get('content_node_id')
    if frm == board.HUMAN_REVIEW and to == board.IN_PROGRESS:
        return [Action('reject', 0, 'Michal sent the PR back from Human Review', dict(node=node)),
                Action('cascade', 0, 'Human Review has a free slot')]
    if frm == board.NEEDS_INPUT and to == board.IN_PROGRESS:
        return [Action('answered', 0, 'Michal answered the question', dict(node=node))]
    if frm == board.HUMAN_REVIEW or to == board.DONE:
        return [Action('cascade', 0, f"card moved {frm} -> {to}")]
    return [Action('skip', why=f"card moved {frm} -> {to}: his decision")]


CARD_QUERY = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
  issueOrPullRequest(number:$n){...on PullRequest{projectItems(first:20){nodes{id project{id}
    fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}
  ...on Issue{projectItems(first:20){nodes{id project{id}
    fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}}}}"""


# -- the executor ------------------------------------------------------------------------

class Executor:
    """Runs actions with the swarm's tools. Every tool is injectable for tests."""

    def __init__(self, g=None, dry=False, sh=None, enqueue=None, cascade=None, give=None, wake_post=None):
        self._g, self.dry = g, dry
        self.sh = sh
        self.enqueue = enqueue
        self.cascade_fn = cascade
        self.give = give
        self.wake_post = wake_post
        self.done = []
        self.errors = []

    @property
    def g(self):
        if self._g is None:
            from .advance import hr_gate
            self._g = hr_gate()
        return self._g

    def note(self, text):
        self.done.append(text)

    def _sh(self, argv):
        if self.sh:
            return self.sh(argv)
        from . import tick
        return tick.sh(argv, check=True).stdout

    def card(self, n):
        """(item id, column) of PR or issue n on Project 4. The board snapshot has open items
        only, so a merged or closed one (do_merged, do_closed, a closed linked issue) is read
        from the item itself (Codex on PR #26: the Done/Canceled move never found its card)."""
        from . import boardcache
        snap = boardcache.snapshot(self.g['gql'], max_age=0)
        for it, c in boardcache.open_prs(snap) + boardcache.open_issues(snap):
            if c['number'] == n:
                return it['id'], boardcache.col(it)
        return self.card_direct(n)

    def card_direct(self, n):
        """(item id, column) from the PR or issue's own projectItems, open or closed."""
        owner, name = REPO.split('/')
        d = self.g['gql'](CARD_QUERY, owner=owner, name=name, n=n)
        node = (((d or {}).get('repository') or {}).get('issueOrPullRequest') or {})
        for it in (node.get('projectItems') or {}).get('nodes') or []:
            if ((it.get('project') or {}).get('id')) == board.PROJECT_ID:
                return it['id'], board.column(it.get('fieldValueByName'))
        return None, None

    def move(self, n, item, col):
        if self.dry:
            return self.note(f"would move #{n} to {col}")
        board.move_card(self.g, item, col, self._sh)
        self.note(f"#{n} -> {col}")

    def minbot(self, n):
        from . import gatelock
        from .promoted import BIN
        if self.dry:
            return self.note(f"would queue swarm-advance --minbot {n}")
        (self.enqueue or gatelock.enqueue)([str(BIN / 'swarm-advance'), '--minbot', str(n)], by='swarm-webhook')
        self.note(f"queued the MinBot check of #{n}")

    def cascade(self, why):
        from . import advance
        if self.dry:
            return self.note('would queue a cascade pass')
        advance.flag_rerun(by=f'swarm-webhook: {why}')
        self.note(f"cascade {(self.cascade_fn or advance.queue_cascade)()}")

    def to_worker(self, n, text):
        """(ok, note): the running worker gets the line, or its parked Claude is resumed."""
        if self.dry:
            return True, 'would relay to the worker'
        if self.give:
            return self.give(n, text)
        from . import flow
        return flow.Flow(dry_run=False, out=lambda l: None, g=self.g).hand_off(n, text)

    def back_to_work(self, n, text, why):
        """Relay, then In Progress, only when a worker has it (never demote into a column with
        no worker); else wake swarmbot. A merged or closed PR, or a card in Done, Canceled or
        Parked, is left alone (Codex on PR #26: a late Codex or CI delivery restarted done work)."""
        from . import advance
        state = (advance.codex_details(self.g['gql'], n).get('state') or 'OPEN')
        item, col = self.card(n)
        if state != 'OPEN' or col in (board.DONE, board.CANCELED, board.PARKED):
            return self.note(f"#{n}: {why}, but the PR is {state.lower()} and its card is in {col}: no rework")
        ok, said = self.to_worker(n, text)
        if not ok:
            return self.wake(n, f"#{n}: {why}; no worker could take the relay ({said})", key=f'noworker:{n}:{why[:40]}')
        if item and col not in (board.IN_PROGRESS, board.NEEDS_INPUT):
            self.move(n, item, board.IN_PROGRESS)
        self.note(f"#{n} relayed to its worker ({said})")

    def wake(self, n, why, key=None, now=time.time):
        """An exception for swarmbot: deduped by key for WAKE_DEDUPE s, and at most one POST
        per WAKE_EVERY s for all keys together (Codex on PR #26: a burst of distinct failures
        woke the LLM once per delivery). Exceptions inside the interval wait in the pending
        list; the next POST carries them in "also"."""
        key = key or why[:80]
        t = now()
        payload = {'source': 'swarm-webhook', 'kind': 'exception', 'pr': n or None, 'why': why,
                   'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'log': str(LOG)}
        with _lock:
            st = _seen()
            last = st.setdefault('wake', {})
            if t - float(last.get(key, 0)) < WAKE_DEDUPE:
                return self.note(f"exception already sent: {why[:120]}")
            last[key] = t
            if not self.dry:
                st.setdefault('wake_pending', []).append(payload)
                del st['wake_pending'][:-WAKE_PENDING_MAX]
            _save_seen(st)
        log(decision='wake', **payload)
        if self.dry:
            return self.note(f"would wake swarmbot: {why}")
        if flush_wakes(self.wake_post or post_wake, now=now):
            self.note(f"woke swarmbot: {why}")
        else:
            self.note(f"swarmbot wake queued (one POST per {WAKE_EVERY} s): {why}")

    # -- handlers -----------------------------------------------------------------------
    def run(self, a):
        fn = getattr(self, 'do_' + a.kind, None)
        if fn is None:
            return self.note(f"skip: {a.why}")
        try:
            fn(a)
        except Exception as e:  # one action failing never stops the rest
            self.errors.append(f"{a.kind} #{a.pr}: {type(e).__name__}: {str(e)[:300]}")
            try:
                self.wake(a.pr, f"{a.kind} on #{a.pr} failed: {type(e).__name__}: {str(e)[:200]}",
                          key=f'fail:{a.kind}:{a.pr}')
            except Exception as e2:  # the wake itself must never drop the delivery
                self.errors.append(f"wake failed: {type(e2).__name__}: {str(e2)[:200]}")

    def do_skip(self, a):
        self.note(f"skip: {a.why}")

    def do_cascade(self, a):
        self.cascade(a.why)

    def do_minbot(self, a):
        self.minbot(a.pr)

    def do_codex(self, a):
        from . import advance
        full = advance.codex_details(self.g['gql'], a.pr)
        if a.args.get('commit') and a.args['commit'] != full.get('headRefOid'):
            return self.note(f"#{a.pr}: Codex reviewed an older head: skip")
        status, detail = advance.codex_decision(full)  # exact head, like the MinBot pass
        if status == 'findings':
            text = (f"Coordinator: #{a.pr}: Codex found issues on your head {full['headRefOid'][:8]} ({detail}). "
                    f"Read them with: gh pr view {a.pr} --comments (and the review threads). {ROOT_CAUSE}")
            return self.back_to_work(a.pr, text, 'Codex findings')
        if status == 'clean':
            self.minbot(a.pr)
            return self.cascade(f'Codex clean on #{a.pr}')
        self.note(f"#{a.pr}: no Codex result on the head yet ({detail})")

    def do_ci(self, a):
        from . import advance
        full = self.g['fetch'](a.pr, refresh=True)
        if a.args.get('sha') != full.get('headRefOid'):
            return self.note(f"#{a.pr}: CI on an older head: skip")
        problem = advance.ci_problem(advance.check_runs(a.args['sha']))
        if problem and problem.startswith('CI is still running'):
            return self.note(f"#{a.pr}: {problem}")
        item, col = self.card(a.pr)
        if problem:
            text = (f"Coordinator: #{a.pr}: {problem} (head {a.args['sha'][:8]}). Read the failed job log "
                    f"(gh pr checks {a.pr}), fix the root cause, push. {ROOT_CAUSE}")
            return self.back_to_work(a.pr, text, 'CI failed')
        if col == board.AUTO_REVIEW:
            return self.minbot(a.pr)
        if col == board.STAGING:
            return self.cascade(f'CI green on Staging #{a.pr}')
        self.note(f"#{a.pr}: CI green; the card is in {col}: nothing to do")

    def do_handoff(self, a):
        """A new head (or ready_for_review) on a card past In Progress: back to Auto Review."""
        from . import tick
        item, col = self.card(a.pr)
        if col in (board.IN_PROGRESS, None) and a.args.get('push'):
            return self.note(f"#{a.pr}: a push in In Progress: left to the Stop hook")
        if col in (board.NEEDS_INPUT, board.DONE, board.CANCELED, board.PARKED):
            return self.note(f"#{a.pr}: card in {col}: not moved")
        full = self.g['fetch'](a.pr, refresh=True)
        head = full.get('headRefOid')
        if full.get('isDraft') and not self.dry:
            self._sh(['gh', 'pr', 'ready', str(a.pr), '-R', REPO])
        if col != board.AUTO_REVIEW:
            self.move(a.pr, item, board.AUTO_REVIEW)
        state = tick.State(tick.STATE_DIR / 'state.json')
        key = f"codex-request:{a.pr}@{head}"
        if not (state.done(key) or tick.codex_requested(full) or tick.codex_reviewed_head(full)):
            if not self.dry:
                self._sh(['gh', 'pr', 'comment', str(a.pr), '-R', REPO, '--body', '@codex review'])
                state.mark(key)
            self.note(f'#{a.pr}: posted "@codex review" for {head[:8]}')
        if col in (board.READY, board.STAGING, board.HUMAN_REVIEW):
            self.cascade(f'#{a.pr} left {col} with a new head')

    def do_relay(self, a):
        item, col = self.card(a.pr)
        if col in (board.HUMAN_REVIEW, board.NEEDS_INPUT):
            return self.note(f"#{a.pr}: comment while in {col}: waiting for Michal's card move")
        if col not in (board.IN_PROGRESS, board.AUTO_REVIEW, board.READY, board.STAGING):
            return self.note(f"#{a.pr}: comment while in {col}: no relay")
        text = (f"Coordinator: #{a.pr}: {a.args.get('author')} commented ({a.args.get('url')}): "
                f"{' '.join((a.args.get('body') or '').split())[:600]} {ROOT_CAUSE}")
        self.back_to_work(a.pr, text, f"comment by {a.args.get('author')}")

    def do_issue_comment(self, a):
        item, col = self.card(a.pr)
        if col in (board.TRIAGE, board.BACKLOG, board.INBOX):
            return self.wake(a.pr, f"TRIAGE handoff: {a.args.get('author')} commented on issue #{a.pr} "
                                   f"({col}): {a.args.get('url')}", key=f"triage:{a.pr}:{a.args.get('url')}")
        self.note(f"issue #{a.pr} comment while in {col}: nothing to do")

    def closing_issues(self, n):
        """The issues PR n closes, with their repository (a cross-repo "Fixes o/r#17" is
        another repo's issue 17, not this repo's)."""
        owner, name = REPO.split('/')
        d = self.g['gql'](f'query{{repository(owner:"{owner}",name:"{name}"){{pullRequest(number:{int(n)})'
                          '{closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}}}}}')
        pr = ((d or {}).get('repository') or {}).get('pullRequest') or {}
        return (pr.get('closingIssuesReferences') or {}).get('nodes') or []

    def pr_of(self, node):
        """The number of a card's PR or issue, only when it is in REPO (else None)."""
        d = self.g['gql']('query($id:ID!){node(id:$id){...on PullRequest{number repository{nameWithOwner}} '
                          '...on Issue{number repository{nameWithOwner}}}}', id=node)
        nd = (d or {}).get('node') or {}
        return nd.get('number') if ((nd.get('repository') or {}).get('nameWithOwner') == REPO) else None

    def comments_since_head(self, n):
        full = self.g['fetch'](n, refresh=True)
        head_at = ((full.get('commits') or {}).get('nodes') or [{}])[0].get('commit', {}).get('committedDate', '')
        return [c for c in (full.get('comments') or {}).get('nodes') or []
                if login(c.get('author')) == MICHAL and (c.get('createdAt') or '') > head_at and not ours(c.get('body'))]

    def do_reject(self, a):
        n = self.pr_of(a.args['node'])
        if not n:
            return self.note('reject: not a PR')
        cs = self.comments_since_head(n)
        said = ' | '.join(' '.join((c.get('body') or '').split())[:400] for c in cs) or '(no comment: see the PR)'
        ok, note = self.to_worker(n, f"Coordinator: #{n}: Michal sent it back from Human Review: {said} {ROOT_CAUSE}")
        if not ok:
            return self.wake(n, f"#{n} rejected; no worker could take it ({note})", key=f'reject:{n}')
        self.note(f"#{n}: Michal's rejection relayed ({note})")

    def do_answered(self, a):
        n = self.pr_of(a.args['node'])
        if not n:
            return self.wake(0, 'Needs Input answered on an issue with no PR: TRIAGE handoff', key=f"answered:{a.args['node']}")
        cs = self.comments_since_head(n)
        said = ' | '.join(' '.join((c.get('body') or '').split())[:600] for c in cs) or '(see the issue)'
        ok, note = self.to_worker(n, f"Coordinator: #{n}: Michal answered: {said} Build it, then push.")
        if not ok:
            return self.wake(n, f"#{n} answered; no worker could take it ({note})", key=f'answered:{n}')
        self.note(f"#{n}: Michal's answer relayed ({note})")

    def do_merged(self, a):
        self.cascade(f'#{a.pr} merged')
        full = self.g['fetch'](a.pr, refresh=True)
        item, _ = self.card(a.pr)
        if item:
            self.move(a.pr, item, board.DONE)
        for i in self.closing_issues(a.pr):
            num = i.get('number')
            repo = (i.get('repository') or {}).get('nameWithOwner')
            if repo != REPO:  # Codex on PR #26: never close a same-number platform issue
                self.note(f"#{a.pr} closes {repo}#{num}: not a {REPO} issue, left to GitHub")
                continue
            if not num or self.dry:
                continue
            it, _ = self.card(num)  # before the close: the issue is still in the open snapshot
            self._sh(['gh', 'issue', 'close', str(num), '-R', REPO, '--reason', 'completed'])
            if it:
                self.move(num, it, board.DONE)
        if not self.dry:
            from .promoted import BIN
            for argv in ([BIN / 'active-gate', '--sweep', '--demote'], [BIN / 'active-gate', '--promote-missing']):
                try:
                    self._sh([str(x) for x in argv])
                except Exception as e:
                    self.note(f"{argv[1]}: {str(e)[:120]}")
        self.note(f"#{a.pr} merged: cascade, Done, linked issues closed")

    def do_closed(self, a):
        item, col = self.card(a.pr)
        if item and col != board.CANCELED:
            self.move(a.pr, item, board.CANCELED)
        if not self.dry:  # a canceled launch must not be started again by the start retry
            from . import slots
            slots.unstarted_done(a.pr)

    def do_card(self, a):
        """A new PR (draft -> In Progress, ready -> Auto Review) or issue (Triage) gets its card."""
        if a.args.get('what') == 'issue':
            col = board.TRIAGE
            url = f'https://github.com/{REPO}/issues/{a.pr}'
        else:
            col = board.IN_PROGRESS if a.args.get('draft') else board.AUTO_REVIEW
            url = f'https://github.com/{REPO}/pull/{a.pr}'
        item, cur = self.card(a.pr)
        if item and cur:
            return self.note(f"#{a.pr} already has a card in {cur}")
        if self.dry:
            return self.note(f"would put #{a.pr} on Project 4 in {col}")
        item = item or json.loads(self._sh(['gh', 'project', 'item-add', '4', '--owner', 'tangentcode', '--url', url,
                                            '--format', 'json']))['id']
        self.move(a.pr, item, col)
        if a.args.get('what') == 'pr':
            from .promoted import BIN
            try:
                self._sh([str(BIN / 'swarm-label-sync'), str(a.pr), '--apply'])
            except Exception as e:
                self.note(f"label sync: {str(e)[:120]}")

    def do_wake(self, a):
        self.wake(a.pr, a.why, key=a.args.get('key'))


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def _retry_after(e):
    try:
        return int(e.headers.get('Retry-After') or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


def post_wake(payload, url_file=None, key_file=None, urlopen=None, sleep=time.sleep, background=True):
    """POST the exception to swarmbot's wake URL (from WAKE_URL_FILE). No URL: logged only.
    An optional key (WAKE_KEY_FILE, mode 600) goes as "Authorization: Bearer <key>"; the key
    is never logged. A failed POST is retried with board-sync's backoff() (Retry-After
    honored) up to eventdb.WAKE_MAX_ATTEMPTS; a 4xx other than 408/429 is not retried. The
    retries run in their own thread, so the handler never waits for them."""
    import urllib.error
    import urllib.request
    url = _read(url_file or WAKE_URL_FILE)
    if not url:
        return log(decision='wake-not-sent', reason='no wake URL configured', **payload)
    headers = {'Content-Type': 'application/json', 'User-Agent': 'swarm-webhook'}
    key = _read(key_file or WAKE_KEY_FILE)
    if key:
        headers['Authorization'] = f'Bearer {key}'
    data = json.dumps(payload).encode()
    urlopen = urlopen or urllib.request.urlopen

    def attempt():
        req = urllib.request.Request(url, data=data, method='POST', headers=headers)
        try:
            with urlopen(req, timeout=20) as r:
                return getattr(r, 'status', 200) or 200, None, None
        except urllib.error.HTTPError as e:
            return e.code, _retry_after(e), f'HTTP {e.code}'
        except Exception as e:
            return None, None, f'{type(e).__name__}: {e}'

    def run():
        for attempts in range(1, eventdb.WAKE_MAX_ATTEMPTS + 1):
            code, wait, err = attempt()
            if code is not None and 200 <= code < 300:
                return log(decision='wake-sent', status=code, attempts=attempts, auth=bool(key), **payload)
            permanent = code is not None and 400 <= code < 500 and code not in (408, 429)
            if permanent or attempts >= eventdb.WAKE_MAX_ATTEMPTS:
                return log(decision='wake-failed', status=code, attempts=attempts, error=err, **payload)
            sleep(eventdb.backoff(attempts, wait))
    if background:
        threading.Thread(target=run, name='wake-post', daemon=True).start()
    else:
        run()


def flush_wakes(post=None, now=time.time):
    """Send the pending exceptions as one POST when WAKE_EVERY s have passed since the last
    one. Returns True when it sent. The server's worker thread calls it every few seconds,
    so a held exception goes out without a new event."""
    t = now()
    with _lock:
        st = _seen()
        pending = st.get('wake_pending') or []
        if not pending or t - float(st.get('wake_last', 0)) < WAKE_EVERY:
            return False
        st['wake_pending'], st['wake_last'] = [], t
        _save_seen(st)
    payload = dict(pending[0])
    if len(pending) > 1:
        payload['also'] = [{k: p.get(k) for k in ('pr', 'why', 'at')} for p in pending[1:]]
    (post or post_wake)(payload)
    return True


# -- dedupe state ------------------------------------------------------------------------

_lock = threading.Lock()


def _seen():
    try:
        return json.loads(SEEN.read_text())
    except (OSError, ValueError):
        return {}


def _save_seen(d):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN.with_suffix('.tmp')
    tmp.write_text(json.dumps(d))
    os.replace(tmp, SEEN)


# -- replay --------------------------------------------------------------------------------

def replay_plan(n, full, ci):
    """[Action] for PR n from its current state (pure). full: advance.codex_details(); ci: the
    advance.ci_problem() text for the head, or None when CI is green. The actions are the
    ones a live delivery would give: the Codex result on the head (codex) and a completed CI
    run on the head (ci). Red CI goes back to the worker and a clean Codex then gives no MinBot
    check (it would refuse on red CI). The handlers re-read the PR, so a replay is safe to run twice."""
    from . import advance
    head = full.get('headRefOid') or ''
    if (full.get('state') or 'OPEN') != 'OPEN':
        return [Action('skip', n, f"#{n} is {full.get('state', '').lower()}")]
    status, detail = advance.codex_decision(full)
    acts = []
    if ci and not ci.startswith('CI is still running'):
        acts.append(Action('ci', n, f'replay: {ci}', dict(sha=head)))
    if status == 'findings' or (status == 'clean' and not acts):  # red CI: no MinBot check
        acts.append(Action('codex', n, f'replay: Codex {status} on {head[:8]} ({detail})', dict(commit=head)))
    elif not acts:
        acts.append(Action('skip', n, f'replay: {detail}' + (f'; {ci}' if ci else '')))
    return acts


def replay(n, ex=None):
    """Run the live handlers on PR n from its current state. Returns (actions, notes)."""
    from . import advance
    ex = ex or Executor()
    full = advance.codex_details(ex.g['gql'], n)
    ci = advance.ci_problem(advance.check_runs(full['headRefOid']))
    acts = replay_plan(n, full, ci)
    for a in acts:
        ex.run(a)
    if ci and ci.startswith('CI is still running'):
        ex.note(f"#{n}: {ci}")
    log(delivery=f'replay-{n}', event='replay', pr=n, decisions=[a.as_dict() for a in acts], done=ex.done)
    _add('replay', f'replay-{n}', 'replay', decisions=[a.kind for a in acts], notes=ex.done, pr=n,
         error='; '.join(ex.errors) or None)
    return acts, ex.done


# -- processing ----------------------------------------------------------------------------

def _add(*a, **kw):
    try:
        return eventdb.store().add(*a, **kw)
    except Exception as e:
        log(decision='eventdb failed', error=f'{type(e).__name__}: {e}')


def handle(event, payload, delivery='', ex=None):
    """Classify and run one delivery; log the decision. Returns the actions."""
    acts = classify(event, payload)
    ex = ex or Executor()
    for a in acts:
        ex.run(a)
    pr = next((a.pr for a in acts if a.pr), None)
    log(delivery=delivery, event=event, action=payload.get('action'), pr=pr,
        decisions=[a.as_dict() for a in acts], done=ex.done)
    _add('handled', delivery, event, payload, decisions=[a.kind for a in acts], notes=ex.done,
         error='; '.join(ex.errors) or None, pr=pr)
    return acts


def _inbox_name(delivery):
    return re.sub(r'[^A-Za-z0-9-]', '', delivery)[:40]


def enqueue(event, delivery, payload):
    """Install one inbox file (atomic rename)."""
    INBOX.mkdir(parents=True, exist_ok=True)
    f = INBOX / f"{time.time_ns()}-{_inbox_name(delivery)}.json"
    tmp = f.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(event=event, delivery=delivery, payload=payload)))
    os.replace(tmp, f)
    return f


def in_inbox(delivery):
    name = _inbox_name(delivery)
    return INBOX.is_dir() and any(f.name.split('-', 1)[-1] == f'{name}.json' for f in INBOX.glob('*.json'))


def _handled(delivery):
    try:
        return eventdb.store().handled(delivery)
    except Exception:
        return False


def recover_unqueued(now=None, min_age=60, max_age=2 * 86400):
    """Queue again each delivery the event DB received (and acknowledged) that has no
    'handled' row and no inbox file, older than min_age s (Codex on PR #26: a crash between
    the receipt row and the inbox write lost it, and its redelivery was a "duplicate").
    Returns the delivery ids queued."""
    now = time.time() if now is None else now
    out = []
    for delivery, event, body in eventdb.store().unhandled(now - max_age, now - min_age):
        if not delivery or in_inbox(delivery):
            continue
        try:
            enqueue(event, delivery, json.loads(body))
        except (OSError, ValueError, TypeError) as e:
            log(delivery=delivery, error=f'recover failed: {type(e).__name__}: {e}')
            continue
        log(delivery=delivery, event=event, decision='recovered an unqueued delivery')
        out.append(delivery)
    return out


def drain(ex_factory=Executor):
    """Handle the inbox files oldest first (one thread)."""
    for f in sorted(INBOX.glob('*.json')) if INBOX.is_dir() else []:
        delivery = f.stem
        try:
            d = json.loads(f.read_text())
            delivery = d.get('delivery') or delivery
            handle(d['event'], d['payload'], delivery, ex_factory())
        except Exception as e:
            log(delivery=delivery, error=f'{type(e).__name__}: {e}')
            _add('handled', delivery, '', error=f'{type(e).__name__}: {str(e)[:500]}')
        f.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    keys = None
    wake = None
    store = None

    def log_message(self, *a):
        pass

    def _reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, 'ok\n') if self.path == '/health' else self._reply(404, 'not found\n')

    def _received(self, *a, **kw):
        """The 'received' row, before the reply. A DB failure is logged and the delivery is
        still handled (webhook.log has it). Returns (row id, duplicate)."""
        try:
            return (self.store or eventdb.store()).add_received(*a, **kw)
        except Exception as e:
            log(decision='eventdb failed', error=f'{type(e).__name__}: {e}')
            return None, False

    def do_POST(self):
        if self.path.split('?')[0] != '/github':
            return self._reply(404, 'not found\n')
        event = self.headers.get('X-GitHub-Event', '')
        hdrs = dict(self.headers.items())
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_BODY:
            self._received(self.headers.get('X-GitHub-Delivery', ''), event, None, hdrs, None, None,
                           reason='bad length', error=f'Content-Length {n}')
            return self._reply(400, 'bad length\n')
        raw = self.rfile.read(n)
        delivery = self.headers.get('X-GitHub-Delivery', '') or hashlib.sha1(raw).hexdigest()
        if not signed(raw, self.headers.get('X-Hub-Signature-256', ''), self.keys or secrets()):
            log(delivery=delivery, event=event, decision='rejected: bad signature')
            # an unsigned body is not trusted: it is kept raw, but no field is read from it
            self._received(delivery, event, None, hdrs, raw, False, reason='bad signature')
            return self._reply(401, 'bad signature\n')
        try:
            payload = json.loads(raw)
        except ValueError:
            self._received(delivery, event, None, hdrs, raw, True, reason='bad json', error='bad json')
            return self._reply(400, 'bad json\n')
        if log_only(event):
            self._received(delivery, event, payload, hdrs, raw, True, reason='log-only', decisions=['log-only'])
            return self._reply(200, 'logged\n')
        try:
            kinds = [a.kind for a in classify(event, payload)]
        except Exception as e:
            kinds = [f'classify failed: {type(e).__name__}']
        _, dup = self._received(delivery, event, payload, hdrs, raw, True, decisions=kinds)
        if dup and (in_inbox(delivery) or _handled(delivery)):
            log(delivery=delivery, event=event, decision='duplicate delivery: skip')
            return self._reply(200, 'duplicate\n')
        if dup:  # Codex on PR #26: received before but never queued (a crash or a failed write)
            log(delivery=delivery, event=event, decision='redelivery of an unqueued delivery: queued')
        try:
            enqueue(event, delivery, payload)
        except OSError as e:  # recover_unqueued() queues it from the event DB later
            log(delivery=delivery, event=event, error=f'inbox write failed: {e}')
            return self._reply(500, 'not queued\n')
        if self.wake:
            self.wake.set()
        self._reply(200, 'queued\n')


def serve(port=PORT, host='127.0.0.1'):
    wake = threading.Event()
    Handler.wake = wake

    def worker():
        last_recover = 0.0
        while True:
            wake.wait(5)
            wake.clear()
            if time.time() - last_recover >= 60:  # also at start: deliveries lost in a crash
                last_recover = time.time()
                try:
                    recover_unqueued()
                except Exception as e:
                    log(decision='recover failed', error=f'{type(e).__name__}: {e}')
            drain()
            try:
                flush_wakes()
            except Exception as e:
                log(decision='wake flush failed', error=f'{type(e).__name__}: {e}')
    threading.Thread(target=worker, name='webhook-worker', daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    log(decision=f'swarm-webhook listening on {host}:{port}')
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-webhook', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--classify', metavar='FILE', help='print the actions for a recorded {event, payload} file')
    ap.add_argument('--dry', action='store_true', help='with replay: print what would be done')
    ap.add_argument('cmd', nargs='?', choices=['replay'], help='replay: run the handlers on --pr N from its current state')
    ap.add_argument('--pr', type=int, action='append', default=[], help='PR number for replay (repeat for more)')
    ap.add_argument('--delivery', action='append', default=[],
                    help='replay a stored delivery from the event DB (its raw body), by delivery id')
    a = ap.parse_args(argv)
    if a.cmd == 'replay':
        if not a.pr and not a.delivery:
            ap.error('replay needs --pr N or --delivery ID')
        rc = 0
        for d in a.delivery:
            got = eventdb.store().body_of(d)
            if not got:
                rc = 1
                print(f"REPLAY {d}: not in the event DB (or not a good delivery)")
                continue
            ex = Executor(dry=a.dry)
            acts = handle(got[0], got[1], f'replay:{d}', ex)
            print(f"REPLAY {d} ({got[0]}): " + ('; '.join(ex.done) or 'nothing done'))
        for n in a.pr:
            try:
                acts, done = replay(n, Executor(dry=a.dry))
                print(f"REPLAY #{n}: " + ('; '.join(done) or 'nothing done'))
            except Exception as e:
                rc = 1
                print(f"REPLAY #{n}: failed: {type(e).__name__}: {str(e)[:200]}")
        return rc
    if a.classify:
        d = json.loads(Path(a.classify).read_text())
        for act in classify(d['event'], d['payload']):
            print(json.dumps(act.as_dict()))
        return 0
    serve(a.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
