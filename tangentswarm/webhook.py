"""swarm-webhook: the deterministic GitHub hook for tangentcode/platform (tangentswarm #31).

Michal 2026-11-15 4:44 AM ET: swarmbot's GitHub activity hook woke an LLM on every platform
event and used 58% of all fleet usage. This server runs the same rules
(~/var/hook-rules-swarmbot.md) without an LLM, and wakes swarmbot only for the exceptions
it cannot handle.

  POST /github     a GitHub webhook delivery. The X-Hub-Signature-256 HMAC must match one
                   of the secrets (SECRET_FILES). The body goes to INBOX and the answer is
                   200 at once; one worker thread handles the inbox in order.
  GET  /health     "ok"

Every delivery and every decision is one JSON line in LOG. classify() is a pure function of
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

from . import board

HOME = Path.home()
STATE = HOME / '.local' / 'state' / 'swarm-flow'
LOG = Path(os.environ.get('SWARM_WEBHOOK_LOG', str(STATE / 'webhook.log')))
INBOX = Path(os.environ.get('SWARM_WEBHOOK_INBOX', str(STATE / 'webhook-inbox')))
SEEN = Path(os.environ.get('SWARM_WEBHOOK_SEEN', str(STATE / 'webhook-seen.json')))
SECRET_FILES = [Path(p) for p in os.environ.get(
    'SWARM_WEBHOOK_SECRETS', f"{HOME}/.config/tangentswarm/webhook-secret:{HOME}/example/hook-secret").split(':') if p]
WAKE_URL_FILE = Path(os.environ.get('SWARM_WAKE_URL_FILE', str(HOME / '.config' / 'tangentswarm' / 'swarmbot-wake-url')))
PORT = int(os.environ.get('SWARM_WEBHOOK_PORT', '8772'))
REPO = 'tangentcode/platform'
MICHAL = 'tangentstorm'
CODEX = 'chatgpt-codex-connector'
WAKE_EVERY = 60          # s between two wakes
WAKE_DEDUPE = 1800       # s: the same exception key wakes once
MAX_BODY = 2 * 1024 * 1024
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
    if event in ('check_run', 'status', 'workflow_job', 'pull_request_review_comment', 'push', 'create', 'delete'):
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

    return [Action('skip', why=f'{event} {action}: no rule')]


def project_move(p):
    """projects_v2_item: Michal's Status moves on Project 4."""
    item = p.get('projects_v2_item') or {}
    ch = ((p.get('changes') or {}).get('field_value') or {})
    if p.get('action') != 'edited' or not ch:
        return [Action('skip', why=f"projects_v2_item {p.get('action')}")]
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
        """(item id, column) of PR or issue n on Project 4, from the board snapshot."""
        from . import boardcache
        snap = boardcache.snapshot(self.g['gql'], max_age=0)
        for it, c in boardcache.open_prs(snap) + boardcache.open_issues(snap):
            if c['number'] == n:
                return it['id'], boardcache.col(it)
        return None, None

    def move(self, n, item, col):
        if self.dry:
            return self.note(f"would move #{n} to {col}")
        self._sh(['gh', 'project', 'item-edit', '--id', item, '--project-id', self.g['project_id'](),
                  '--field-id', self.g['STATUS_FIELD'], '--single-select-option-id', board.OPTION[col]])
        from . import boardcache
        boardcache.invalidate()
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
        rc, said = flow.Flow(dry_run=False, out=lambda l: None, g=self.g).give_worker(n, text)
        return rc == 0, said

    def back_to_work(self, n, text, why):
        """Relay, then In Progress, only when a worker has it (never demote into a column with
        no worker); else wake swarmbot."""
        ok, said = self.to_worker(n, text)
        if not ok:
            return self.wake(n, f"#{n}: {why}; no worker could take the relay ({said})", key=f'noworker:{n}:{why[:40]}')
        item, col = self.card(n)
        if item and col not in (board.IN_PROGRESS, board.NEEDS_INPUT):
            self.move(n, item, board.IN_PROGRESS)
        self.note(f"#{n} relayed to its worker ({said})")

    def wake(self, n, why, key=None, now=time.time):
        """An exception for swarmbot: deduped by key, at most one POST per WAKE_EVERY s."""
        key = key or why[:80]
        st = _seen()
        last = st.setdefault('wake', {})
        t = now()
        if t - float(last.get(key, 0)) < WAKE_DEDUPE:
            return self.note(f"exception already sent: {why[:120]}")
        last[key] = t
        _save_seen(st)
        payload = {'source': 'swarm-webhook', 'kind': 'exception', 'pr': n or None, 'why': why,
                   'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'log': str(LOG)}
        log(decision='wake', **payload)
        if self.dry:
            return self.note(f"would wake swarmbot: {why}")
        (self.wake_post or post_wake)(payload)
        self.note(f"woke swarmbot: {why}")

    # -- handlers -----------------------------------------------------------------------
    def run(self, a):
        fn = getattr(self, 'do_' + a.kind, None)
        if fn is None:
            return self.note(f"skip: {a.why}")
        try:
            fn(a)
        except Exception as e:  # one action failing never stops the rest
            self.wake(a.pr, f"{a.kind} on #{a.pr} failed: {type(e).__name__}: {str(e)[:200]}",
                      key=f'fail:{a.kind}:{a.pr}')

    def do_skip(self, a):
        self.note(f"skip: {a.why}")

    def do_cascade(self, a):
        self.cascade(a.why)

    def do_minbot(self, a):
        self.minbot(a.pr)

    def do_codex(self, a):
        from . import tick
        full = self.g['fetch'](a.pr, refresh=True)
        if a.args.get('commit') and a.args['commit'] != full.get('headRefOid'):
            return self.note(f"#{a.pr}: Codex reviewed an older head: skip")
        status, detail = tick.codex_status(full)
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

    def pr_of(self, node):
        d = self.g['gql']('query($id:ID!){node(id:$id){...on PullRequest{number} ...on Issue{number}}}', id=node)
        return ((d or {}).get('node') or {}).get('number')

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
        for i in ((full.get('closingIssuesReferences') or {}).get('nodes') or []):
            num = i.get('number')
            if not num or self.dry:
                continue
            self._sh(['gh', 'issue', 'close', str(num), '-R', REPO, '--reason', 'completed'])
            it, _ = self.card(num)
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


def post_wake(payload, url_file=None):
    """POST the exception to swarmbot's wake URL (from WAKE_URL_FILE). No URL: logged only."""
    import urllib.request
    try:
        url = (url_file or WAKE_URL_FILE).read_text().strip()
    except OSError:
        url = ''
    if not url:
        return log(decision='wake-not-sent', why='no wake URL configured', **payload)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(decision='wake-sent', status=r.status, **payload)
    except Exception as e:
        log(decision='wake-failed', error=f'{type(e).__name__}: {e}', **payload)


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


def first_time(delivery, keep=5000):
    """True the first time a delivery id is seen (GitHub redelivers on a timeout)."""
    with _lock:
        d = _seen()
        ids = d.setdefault('deliveries', [])
        if delivery in ids:
            return False
        ids.append(delivery)
        del ids[:-keep]
        _save_seen(d)
        return True


# -- processing ----------------------------------------------------------------------------

def handle(event, payload, delivery='', ex=None):
    """Classify and run one delivery; log the decision. Returns the actions."""
    acts = classify(event, payload)
    ex = ex or Executor()
    for a in acts:
        ex.run(a)
    pr = next((a.pr for a in acts if a.pr), None)
    log(delivery=delivery, event=event, action=payload.get('action'), pr=pr,
        decisions=[a.as_dict() for a in acts], done=ex.done)
    return acts


def drain(ex_factory=Executor):
    """Handle the inbox files oldest first (one thread)."""
    for f in sorted(INBOX.glob('*.json')) if INBOX.is_dir() else []:
        try:
            d = json.loads(f.read_text())
            handle(d['event'], d['payload'], d.get('delivery', ''), ex_factory())
        except Exception as e:
            log(delivery=f.stem, error=f'{type(e).__name__}: {e}')
        f.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    keys = None
    wake = None

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

    def do_POST(self):
        if self.path.split('?')[0] != '/github':
            return self._reply(404, 'not found\n')
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0 or n > MAX_BODY:
            return self._reply(400, 'bad length\n')
        raw = self.rfile.read(n)
        event = self.headers.get('X-GitHub-Event', '')
        delivery = self.headers.get('X-GitHub-Delivery', '') or hashlib.sha1(raw).hexdigest()
        if not signed(raw, self.headers.get('X-Hub-Signature-256', ''), self.keys or secrets()):
            log(delivery=delivery, event=event, decision='rejected: bad signature')
            return self._reply(401, 'bad signature\n')
        if not first_time(delivery):
            log(delivery=delivery, event=event, decision='duplicate delivery: skip')
            return self._reply(200, 'duplicate\n')
        try:
            payload = json.loads(raw)
        except ValueError:
            return self._reply(400, 'bad json\n')
        INBOX.mkdir(parents=True, exist_ok=True)
        f = INBOX / f"{time.time_ns()}-{re.sub(r'[^A-Za-z0-9-]', '', delivery)[:40]}.json"
        tmp = f.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(event=event, delivery=delivery, payload=payload)))
        os.replace(tmp, f)
        if self.wake:
            self.wake.set()
        self._reply(200, 'queued\n')


def serve(port=PORT, host='127.0.0.1'):
    wake = threading.Event()
    Handler.wake = wake

    def worker():
        while True:
            wake.wait(5)
            wake.clear()
            drain()
    threading.Thread(target=worker, name='webhook-worker', daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    log(decision=f'swarm-webhook listening on {host}:{port}')
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-webhook', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--port', type=int, default=PORT)
    ap.add_argument('--classify', metavar='FILE', help='print the actions for a recorded {event, payload} file')
    a = ap.parse_args(argv)
    if a.classify:
        d = json.loads(Path(a.classify).read_text())
        for act in classify(d['event'], d['payload']):
            print(json.dumps(act.as_dict()))
        return 0
    serve(a.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
