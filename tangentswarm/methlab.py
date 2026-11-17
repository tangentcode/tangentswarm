"""Meth Lab: the test environment for the swarm flow (tangentswarm #115).

Michal 2026-11-17 11:07 AM ET: "let's go ahead and make the test environment for the
methodology, and that way we can call it meth lab". Two parts:

1. The everyday check: no GitHub, no network, no Codex.

   FakeGitHub is an in-memory GitHub: repositories with PRs and issues, a project board
   with cards, Codex reviews, CI runs and comments. Its gql() and sh() answer the reads and
   writes that swarm-webhook's Executor makes, so the real rules run against it. Each step
   (open_pr, push, codex_review, ci, move, comment, merge, ...) changes the fake state the way
   GitHub would and gives the webhook delivery GitHub would send; deliver() runs that
   delivery through the real classify() and Executor. A test then checks the board path
   (fake.history, fake.col(n)) and what the swarm did (relays, queued checks, cascades,
   wakes, Codex asks). A read or a gh call the fake does not know raises FakeGitHubError, so a
   new GitHub call in the flow shows as an error, never as a silent pass. The full run writes
   the swarm's own state files (event DB, seen files), so it runs under the test isolation
   of tests/conftest.py, never against the live state.

   recorded() reads real deliveries from the event DB (their raw bodies and the decisions
   the live server took), and replay() runs each one through the current classify() with no
   side effect. diff() lists each delivery whose decision is different now. Run it before a
   deploy: `swarm-methlab replay [--db PATH] [--hours H]` (read-only: the DB is opened with
   mode=ro; --hours H limits it to the deliveries since the last deploy). A difference is
   not always a bug: it is a rule change to confirm.

2. The live sandbox, on demand: a separate Free GitHub organization with a private test
   repository and board (docs/meth-lab.md has the setup steps). The [methlab] config section
   names it. live_problems() refuses any setting that could touch production: a production
   org or repository (a board number is only read inside the Meth Lab org), the production
   webhook port or secret, or a Codex connection.
   `swarm-methlab live-env` prints the whole environment for a test copy of swarm-webhook
   (started with `env -i`): its own HOME, gh login, config, port, secret and every state
   path inside state_dir, which must not overlap a production directory. It is started by
   hand for a check and stopped after it, never left running.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from . import board, codexrounds, config, webhook as W

MICHAL = W.MICHAL
CODEX_USER = 'chatgpt-codex-connector[bot]'
SWARM_USER = 'tangentstorm'  # the swarm posts through gh as Michal's account


class FakeGitHubError(RuntimeError):
    """A GitHub read or write the fake does not model."""


class FakeGitHub:
    """An in-memory GitHub for the swarm flow. repo: the default repository of the steps."""

    def __init__(self, repo='tangentcode/tangentswarm', worker=True):
        self.repo = repo
        self.worker = worker          # a live worker takes each relay
        self.prs = {}                 # (repo, n) -> PR dict (the CODEX_FIELDS shape)
        self.issues = {}              # (repo, n) -> {'number', 'state', 'comments'}
        self.cards = {}               # (repo, n) -> {'id', 'col'}
        self.ci_runs = {}             # head sha -> [check run]
        self.calls = []               # every gh argv and GraphQL query, in order
        self.relays, self.queued, self.wakes, self.merges, self.deploys = [], [], [], [], []
        self.cascades = 0
        self.history = []             # one row per delivery: event, action, kinds, notes, errors
        self._sha = 0
        self._item = 0
        self._clock = 0

    # -- the state ----------------------------------------------------------------------
    def board_of(self, repo=None):
        return board.for_repo(repo or self.repo) or board.DEFAULT

    def pr(self, n, repo=None):
        return self.prs[(repo or self.repo, n)]

    def col(self, n, repo=None):
        """The column of the card of PR or issue n, or None (no card)."""
        c = self.cards.get((repo or self.repo, n))
        return c and c['col']

    def asks(self, n, repo=None):
        """The heads that got an "@codex review" comment, in order."""
        return [c['head'] for c in self.pr(n, repo)['comments']['nodes'] if c['body'].strip() == codexrounds.ASK]

    def _new_sha(self):
        self._sha += 1
        return f'{self._sha:040x}'

    def _now(self):
        self._clock += 1
        return f'2026-11-17T12:{self._clock // 60:02d}:{self._clock % 60:02d}Z'

    def _repo_payload(self, repo):
        return {'full_name': repo, 'name': repo.split('/')[1], 'owner': {'login': repo.split('/')[0]}}

    def _pr_payload(self, p, repo):
        return {'number': p['number'], 'draft': p['isDraft'], 'merged': p['state'] == 'MERGED',
                'state': 'closed' if p['state'] != 'OPEN' else 'open', 'node_id': p['id'],
                'base': {'ref': p['baseRefName']},
                'head': {'sha': p['headRefOid'], 'ref': p['headRefName'], 'repo': {'full_name': repo}},
                'merge_commit_sha': p.get('mergeCommit'),
                'html_url': f'https://github.com/{repo}/pull/{p["number"]}'}

    def _comment(self, p, body, user):
        c = {'author': {'login': user.removesuffix('[bot]')}, 'body': body, 'createdAt': self._now(),
             'head': p['headRefOid']}
        p['comments']['nodes'].append(c)
        return c

    # -- the steps (each changes the state and returns (event, payload)) ------------------
    def open_pr(self, n, branch=None, draft=True, closes=(), base='main', repo=None):
        repo = repo or self.repo
        self.prs[(repo, n)] = {
            'id': f'PR_{repo}_{n}', 'number': n, 'state': 'OPEN', 'isDraft': draft,
            'headRefOid': self._new_sha(), 'headRefName': branch or f'work-{n}', 'baseRefName': base,
            'mergeable': 'MERGEABLE', 'body': '', 'closes': list(closes),
            'closingIssuesReferences': {'nodes': []}, 'comments': {'nodes': []},
            'commits': {'nodes': [{'commit': {'committedDate': self._now()}}]},
            'reviews': {'nodes': []}, 'reviewThreads': {'nodes': []}}
        return 'pull_request', {'action': 'opened', 'repository': self._repo_payload(repo),
                                'sender': {'login': SWARM_USER}, 'pull_request': self._pr_payload(self.pr(n, repo), repo)}

    def open_issue(self, n, repo=None):
        repo = repo or self.repo
        self.issues[(repo, n)] = {'id': f'I_{repo}_{n}', 'number': n, 'state': 'OPEN', 'comments': []}
        return 'issues', {'action': 'opened', 'repository': self._repo_payload(repo),
                          'sender': {'login': SWARM_USER}, 'issue': {'number': n, 'node_id': f'I_{repo}_{n}'}}

    def push(self, n, repo=None):
        """A new head on PR n (a worker's push)."""
        repo = repo or self.repo
        p = self.pr(n, repo)
        p['headRefOid'] = self._new_sha()
        p['commits']['nodes'] = [{'commit': {'committedDate': self._now()}}]
        return 'pull_request', {'action': 'synchronize', 'repository': self._repo_payload(repo),
                                'sender': {'login': SWARM_USER}, 'pull_request': self._pr_payload(p, repo)}

    def ready(self, n, repo=None):
        """The worker marks PR n ready for review."""
        repo = repo or self.repo
        p = self.pr(n, repo)
        p['isDraft'] = False
        return 'pull_request', {'action': 'ready_for_review', 'repository': self._repo_payload(repo),
                                'sender': {'login': SWARM_USER}, 'pull_request': self._pr_payload(p, repo)}

    def codex_review(self, n, findings=False, repo=None):
        """A Codex review of the head of PR n: with findings, one open thread on the head."""
        repo = repo or self.repo
        p = self.pr(n, repo)
        head = p['headRefOid']
        p['reviews']['nodes'].append({'author': {'login': 'chatgpt-codex-connector'}, 'commit': {'oid': head},
                                      'comments': {'totalCount': 1 if findings else 0}})
        if findings:
            p['reviewThreads']['nodes'].append({
                'id': f'T{len(p["reviewThreads"]["nodes"])}_{n}', 'isResolved': False, 'isOutdated': False,
                'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'}, 'originalCommit': {'oid': head},
                                        'path': 'x.py', 'body': 'P1: a finding'}]}})
        return 'pull_request_review', {'action': 'submitted', 'repository': self._repo_payload(repo),
                                       'sender': {'login': CODEX_USER}, 'pull_request': self._pr_payload(p, repo),
                                       'review': {'user': {'login': CODEX_USER}, 'commit_id': head,
                                                  'body': 'P1: a finding' if findings else ''}}

    def resolve_threads(self, n, repo=None):
        for t in self.pr(n, repo)['reviewThreads']['nodes']:
            t['isResolved'] = True

    def ci(self, n, conclusion='success', repo=None):
        """A completed check suite on the head of PR n."""
        repo = repo or self.repo
        p = self.pr(n, repo)
        self.ci_runs[p['headRefOid']] = [{'name': 'tests', 'status': 'completed', 'conclusion': conclusion}]
        return 'check_suite', {'action': 'completed', 'repository': self._repo_payload(repo),
                               'sender': {'login': 'github-actions[bot]'},
                               'check_suite': {'conclusion': conclusion, 'head_sha': p['headRefOid'],
                                               'pull_requests': [{'number': n}]}}

    def comment(self, n, body, user=MICHAL, repo=None):
        """A comment on PR or issue n."""
        repo = repo or self.repo
        is_pr = (repo, n) in self.prs
        if is_pr:
            self._comment(self.pr(n, repo), body, user)
        else:
            self.issues[(repo, n)]['comments'].append({'author': {'login': user}, 'body': body})
        issue = {'number': n, **({'pull_request': {}} if is_pr else {})}
        return 'issue_comment', {'action': 'created', 'repository': self._repo_payload(repo), 'sender': {'login': user},
                                 'issue': issue, 'comment': {'user': {'login': user}, 'body': body,
                                                             'html_url': f'https://github.com/{repo}/issues/{n}#c'}}

    def move(self, n, to, by=MICHAL, repo=None):
        """A card move on the board (by Michal, unless by says otherwise)."""
        repo = repo or self.repo
        b = self.board_of(repo)
        card = self.cards[(repo, n)]
        frm, card['col'] = card['col'], to
        node = (self.prs.get((repo, n)) or self.issues.get((repo, n)))['id']
        return 'projects_v2_item', {
            'action': 'edited', 'sender': {'login': by},
            'projects_v2_item': {'id': card['id'], 'content_node_id': node, 'project_node_id': b.id},
            'changes': {'field_value': {'field_node_id': b.status_field, 'from': {'id': b.option.get(frm)},
                                        'to': {'id': b.option.get(to)}}}}

    def merge(self, n, repo=None):
        """PR n is merged into its base (by hand, or by p6merge through merged())."""
        repo = repo or self.repo
        p = self.pr(n, repo)
        p['state'] = 'MERGED'
        p['mergeCommit'] = self._new_sha()
        return 'pull_request', {'action': 'closed', 'repository': self._repo_payload(repo),
                                'sender': {'login': SWARM_USER}, 'pull_request': self._pr_payload(p, repo)}

    def close(self, n, repo=None):
        repo = repo or self.repo
        self.pr(n, repo)['state'] = 'CLOSED'
        return 'pull_request', {'action': 'closed', 'repository': self._repo_payload(repo),
                                'sender': {'login': SWARM_USER}, 'pull_request': self._pr_payload(self.pr(n, repo), repo)}

    # -- running a delivery -------------------------------------------------------------
    def executor(self):
        return W.Executor(g=self.g, sh=self.sh, enqueue=self.enqueue, cascade=self.cascade, give=self.give,
                          wake_post=self.wake_post, signal_promoted=lambda: 'woken (meth lab)',
                          p6merge=self.p6merge, check_runs=self.check_runs, deploy_start=self.deploy_start)

    def deliver(self, step):
        """Run one (event, payload) through classify() and the Executor. Returns the actions."""
        event, payload = step
        acts = W.classify(event, payload, poke=False)
        ex = self.executor()
        for a in acts:
            ex.run(a)
        self.history.append({'event': event, 'action': payload.get('action'), 'kinds': [a.kind for a in acts],
                             'notes': list(ex.done), 'errors': list(ex.errors)})
        return acts

    def errors(self):
        return [e for h in self.history for e in h['errors']]

    # -- the tools the Executor calls ---------------------------------------------------
    @property
    def g(self):
        b = board.DEFAULT
        return {'gql': self.gql, 'fetch': lambda n, refresh=False: self._pr_full(W.REPO, n),
                'project_id': lambda: b.id, 'STATUS_FIELD': b.status_field}

    def enqueue(self, argv, by=''):
        self.queued.append([os.path.basename(str(argv[0]))] + [str(x) for x in argv[1:]])

    def cascade(self):
        self.cascades += 1
        return 'queued (meth lab)'

    def give(self, n, text):
        self.relays.append((n, text))
        return self.worker, 'sent (meth lab)' if self.worker else 'no live worker (meth lab)'

    def wake_post(self, payload):
        self.wakes.append(payload)

    def deploy_start(self, sha, pr=None):
        self.deploys.append((sha, pr))
        return f'deploy of {sha[:8]} recorded (meth lab)'

    def check_runs(self, head, repo=None):
        return list(self.ci_runs.get(head, []))

    def p6merge(self, repo, n, head, trigger, gql=None, sh=None):
        """The fake p6merge: merges at head when the PR is open and head is its head."""
        p = self.prs.get((repo, n))
        self.merges.append((repo, n, head, trigger))
        if trigger == 'merged':
            return {'merged': False, 'why': 'cleanup only (meth lab)'}
        if not p or p['state'] != 'OPEN' or p['headRefOid'] != head:
            return {'merged': False, 'why': 'not the open head (meth lab)'}
        p['state'] = 'MERGED'
        return {'merged': True, 'notes': ['merged (meth lab)']}

    def _pr_full(self, repo, n):
        p = self.prs.get((repo, n))
        if p is None:
            return {}
        out = {k: v for k, v in p.items() if k not in ('closes', 'id', 'mergeCommit')}
        out['comments'] = {'nodes': [{k: v for k, v in c.items() if k != 'head'} for c in p['comments']['nodes']]}
        out['closingIssuesReferences'] = {'nodes': [
            {'number': i, 'repository': {'nameWithOwner': repo},
             'comments': {'nodes': list((self.issues.get((repo, i)) or {}).get('comments') or [])}}
            for i in p['closes']]}
        return out

    def _card_nodes(self, repo, n):
        c = self.cards.get((repo, n))
        if not c:
            return []
        b = self.board_of(repo)
        return [{'id': c['id'], 'project': {'id': b.id},
                 'fieldValueByName': {'name': c['col'], 'optionId': b.option.get(c['col'])}}]

    def gql(self, q, **v):
        self.calls.append(('gql', ' '.join(q.split())[:120], v))
        m = re.search(r'repository\(owner:"([^"]+)",name:"([^"]+)"\)', q)
        repo = f'{m.group(1)}/{m.group(2)}' if m else (f"{v['owner']}/{v['name']}" if 'owner' in v else None)
        num = re.search(r'pullRequest\(number:(\d+)\)', q)
        if 'issueOrPullRequest' in q:
            return {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': self._card_nodes(repo, v['n'])}}}}
        if 'node(id:$id)' in q:
            for (r, n), p in list(self.prs.items()) + list(self.issues.items()):
                if p['id'] == v.get('id'):
                    kind = 'PullRequest' if (r, n) in self.prs else 'Issue'
                    return {'node': {'__typename': kind, 'number': n, 'repository': {'nameWithOwner': r}}}
            return {'node': None}
        if 'organization' in q and 'projectV2' in q:  # the Project 4 snapshot: card() then reads the item
            empty = {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
            return {'organization': {'projectV2': {'prs': empty, 'issues': empty}}}
        if num and 'p: pullRequest' in q:
            return {'repository': {'p': self._pr_full(repo, int(num.group(1)))}}
        if 'pullRequest(number:$n)' in q and 'closingIssuesReferences' in q:  # ANSWERS_Q
            return {'repository': {'pullRequest': self._pr_full(repo, v['n'])}}
        if num and 'closingIssuesReferences' in q:
            full = self._pr_full(repo, int(num.group(1)))
            return {'repository': {'pullRequest': {'closingIssuesReferences': full.get('closingIssuesReferences')}}}
        if num and 'headRefName' in q:
            return {'repository': {'pullRequest': {'headRefName': self._pr_full(repo, int(num.group(1))).get('headRefName')}}}
        raise FakeGitHubError(f'meth lab: no fake answer for the query {" ".join(q.split())[:160]!r}')

    def sh(self, argv):
        argv = [str(x) for x in argv]
        self.calls.append(('sh', argv))
        if argv[:3] == ['gh', 'project', 'item-edit']:
            item, opt = argv[argv.index('--id') + 1], argv[argv.index('--single-select-option-id') + 1]
            col = board.BY_ID.get(opt)
            card = next((c for c in self.cards.values() if c['id'] == item), None)
            if card is None or col is None:
                raise FakeGitHubError(f'meth lab: no card {item} or no column for option {opt}')
            card['col'] = col
            return ''
        if argv[:3] == ['gh', 'project', 'item-add']:
            url = argv[argv.index('--url') + 1]
            m = re.match(r'https://github\.com/([^/]+/[^/]+)/(?:pull|issues)/(\d+)$', url)
            if not m:
                raise FakeGitHubError(f'meth lab: bad item url {url}')
            key = (m.group(1), int(m.group(2)))
            if key not in self.cards:
                self._item += 1
                self.cards[key] = {'id': f'ITEM_{self._item}', 'col': None}
            return json.dumps({'id': self.cards[key]['id']})
        if argv[:3] == ['gh', 'pr', 'ready']:
            p = self.pr(int(argv[3]), argv[argv.index('-R') + 1])
            p['isDraft'] = '--undo' in argv
            return ''
        if argv[:3] == ['gh', 'pr', 'comment']:
            p = self.pr(int(argv[3]), argv[argv.index('-R') + 1])
            self._comment(p, argv[argv.index('--body') + 1], SWARM_USER)
            return ''
        if argv[:3] == ['gh', 'issue', 'close']:
            i = self.issues.get((argv[argv.index('-R') + 1], int(argv[3])))
            if i is None:
                raise FakeGitHubError(f'meth lab: no issue {argv[3]}')
            i['state'] = 'CLOSED'
            return ''
        if argv[:2] == ['gh', 'api'] and any('matching-refs' in a or '/pulls?' in a for a in argv):
            return '[]'
        if argv and not argv[0].startswith('gh') and os.path.basename(argv[0]) in ('active-gate', 'swarm-label-sync'):
            return ''  # local swarm tools on a platform merge or card: no GitHub state in the fake
        raise FakeGitHubError(f'meth lab: no fake for the command {" ".join(argv)[:160]!r}')


# -- replay of recorded deliveries -------------------------------------------------------

def _open_ro(db):
    return sqlite3.connect(f'file:{Path(db).expanduser()}?mode=ro', uri=True)


def recorded(db=None, since=None, limit=None, conn=None):
    """[{delivery, event, payload, recorded}] of the good webhook deliveries in the event DB,
    oldest first: the raw body of the first 'received' row, and the decision kinds the live
    server took (its 'handled' row; the 'received' row when it was never handled). since: a
    unix time. The DB is opened read-only."""
    from . import eventdb
    own = conn is None
    conn = conn or _open_ro(db or eventdb.PATH)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT delivery, event, body, decisions, received FROM events WHERE stage='received' AND sig_ok=1 "
               "AND (reason IS NULL OR reason='log-only') AND body IS NOT NULL" + (' AND received >= ?' if since else '')
               + ' ORDER BY id')
        rows = conn.execute(sql, (since,) if since else ()).fetchall()
        handled = {}
        for r in conn.execute("SELECT delivery, decisions FROM events WHERE stage='handled' ORDER BY id"):
            handled.setdefault(r['delivery'], r['decisions'])
    finally:
        if own:
            conn.close()
    out, seen = [], set()
    for r in rows:
        if r['delivery'] in seen:
            continue
        seen.add(r['delivery'])
        try:
            payload = json.loads(r['body'])
        except (TypeError, ValueError):
            continue
        out.append({'delivery': r['delivery'], 'event': r['event'], 'payload': payload,
                    'recorded': json.loads(handled.get(r['delivery']) or r['decisions'] or '[]')})
    return out[-limit:] if limit else out


def replay(deliveries):
    """Each delivery with 'now': the decision kinds of the current classify(), with no side
    effect (no sanity poke). A classify() that raises gives 'classify failed: <type>', as the
    live server records it."""
    out = []
    for d in deliveries:
        if W.log_only(d['event']):
            now = ['log-only']
        else:
            try:
                now = [a.kind for a in W.classify(d['event'], d['payload'], poke=False)]
            except Exception as e:
                now = [f'classify failed: {type(e).__name__}']
        out.append(dict(d, now=now))
    return out


def diff(replayed):
    """The replayed deliveries whose decision kinds changed."""
    return [d for d in replayed if d['now'] != d['recorded']]


def groups(changed):
    """[((event/action, recorded, now), [delivery])] of the changed deliveries, the largest
    group first: one line per kind of change, not one per delivery."""
    out = {}
    for d in changed:
        key = (f"{d['event']}/{d['payload'].get('action')}", d['recorded'], d['now'])
        out.setdefault(json.dumps(key), (key, []))[1].append(d)
    return sorted(out.values(), key=lambda kv: -len(kv[1]))


# -- the live sandbox --------------------------------------------------------------------

def live_config():
    return config.get('methlab', default={}) or {}


def live_problems(cfg=None):
    """Why the Meth Lab live settings could touch production, or are not complete. [] = safe."""
    cfg = live_config() if cfg is None else cfg
    out = []
    org, repo = cfg.get('org') or '', cfg.get('repo') or ''
    for k in ('org', 'repo', 'project_number', 'port', 'secret_files', 'state_dir'):
        if not cfg.get(k):
            out.append(f'[methlab] {k} is not set')
    prod_orgs = {r.split('/')[0].lower() for r in board.REPOS}
    if org and org.lower() in prod_orgs:
        out.append(f'the org {org} is a production org: Meth Lab needs its own org')
    if repo and (repo in board.REPOS or repo.split('/')[0].lower() in prod_orgs):
        out.append(f'the repo {repo} is in a production org')
    if repo and org and repo.split('/')[0].lower() != org.lower():
        out.append(f'the repo {repo} is not in the Meth Lab org {org}')
    if cfg.get('port') and int(cfg['port']) == W.PORT:
        out.append(f'port {cfg["port"]} is the production webhook port')
    prod_secrets = {Path(p).expanduser().resolve() for p in config.files('webhook', 'secret_files')}
    for f in cfg.get('secret_files') or []:
        if Path(os.path.expanduser(str(f))).resolve() in prod_secrets:
            out.append(f'the secret file {f} is a production webhook secret')
    out += state_dir_problems(cfg.get('state_dir'))
    if cfg.get('codex'):
        out.append('codex is on: do not connect Codex to the Meth Lab repo (it uses the Codex allowance)')
    return out


def production_dirs(home=None):
    """The directories that hold the live swarm's state, cache and config (resolved)."""
    from . import paths
    home = Path(home or os.path.expanduser('~'))
    out = [home / '.local' / 'state' / 'swarm-flow', home / '.local' / 'state' / 'mn-workflow',
           home / '.cache', home / 'relay', home / 'bin', home / '.config' / 'tangentswarm',
           home / '.config' / 'gh', paths.state_dir(), paths.config_dir(), Path(W.LOG).parent,
           Path(W.INBOX).parent, Path(W.SEEN).parent]
    from . import eventdb
    out.append(Path(eventdb.PATH).parent)
    return sorted({p.expanduser().resolve() for p in out})


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def state_dir_problems(state_dir, home=None):
    """Why state_dir could share files with the live swarm: it is the home directory, or it is,
    holds or sits inside a production state, cache or config directory."""
    if not state_dir:
        return []
    home = Path(home or os.path.expanduser('~')).resolve()
    st = Path(os.path.expanduser(str(state_dir))).resolve()
    if st == home or st in home.parents:
        return [f'the state_dir {state_dir} is the home directory or above it: use a new directory of its own']
    return [f'the state_dir {state_dir} overlaps the production directory {p}'
            for p in production_dirs(home) if _overlap(st, p)]


# Every SWARM_* path setting of the package, set inside the sandbox state dir as well (with the
# sandbox HOME, the defaults are there already; this also covers a /tmp default and a value a
# caller forgets to clear).
SANDBOX_FILES = {
    'SWARM_WEBHOOK_LOG': 'webhook.log', 'SWARM_WEBHOOK_INBOX': 'webhook-inbox',
    'SWARM_WEBHOOK_SEEN': 'webhook-seen.json', 'SWARM_EVENTS_DB': 'events.sqlite',
    'SWARM_WEBHOOK_DB': 'events.sqlite', 'SWARM_EVENT_SOURCE': None,
    'SWARM_WAKE_URL_FILE': 'no-wake-url', 'SWARM_WAKE_KEY_FILE': 'no-wake-key',  # never wake the live swarmbot
    'SWARM_WATCH_STATE': 'watch.json', 'SWARM_ALERT_URL_FILE': 'no-alert-url', 'SWARM_ALERT_KEY_FILE': 'no-alert-key',
    'SWARM_FLOW_STATE': 'swarm-flow/pull.json', 'SWARM_MOVES_LOG': 'moves.log',
    'SWARM_MERGE_CLOCK_LOG': 'merge-to-move.log', 'SWARM_READY_SEEN': 'ready-seen.json',
    'SWARM_RELAY_DIR': 'relay', 'SWARM_CONFLICT_MAP': 'conflict-map.json', 'SWARM_GQL_LOG': 'gql.log',
    'SWARM_HANDOFF_SPOOL': 'spool', 'SWARM_GITHUB_EVENTS': 'github-events.jsonl',
    'SWARM_EVENTS_OFFSET': 'events.offset', 'SWARM_MN_PENDING': 'mn-pending', 'SWARM_MN_CONFIG': 'no-mn-config.json',
    'SWARM_BOARD_SNAPSHOT': 'board-snapshot.json', 'SWARM_BOARD_DETAILS': 'pr-details.json',
    'SWARM_BOARD_SYNC_LOG': 'board-sync.log', 'SWARM_GATE_LOCK': 'swarm-serial.lock',
    'SWARM_GATE_QUEUE': 'gate-queue', 'SWARM_CASCADE_LOG': 'cascade.log', 'SWARM_CASCADE_RERUN': 'swarm-serial.rerun',
    'SWARM_SLOTS_STATE': 'slots.json', 'SWARM_STOP_HOOK_LOG': 'stop-hook.log',
    'SWARM_STOP_HOOK_SEEN': 'stop-hook-seen.json', 'SWARM_STOP_HOOK_LOCKS': 'stop-hook-locks',
    'SWARM_TICK_STATE': 'tick-state', 'SWARM_PANES_REPORT': 'panes.json', 'SWARM_HELD': 'held.json',
    'SWARM_FASTTRACK_STATE': 'fasttrack.json', 'SWARM_MAINMERGE_CACHE': 'mainmerge-cache.json',
    'SWARM_MERGE_CLOCK': 'merge-clock.json', 'SWARM_FLOW_STATUS_CACHE': 'flow-status.json',
    'SWARM_HOOK_CACHE': 'hook-cache.json',
    'SWARM_DEPLOY_STATE': 'deploy-state', 'SWARM_FLOW_LIVE': 'no-flow-live',  # never deploys the live tree
}


def live_env(cfg=None):
    """The whole environment of a test copy of swarm-webhook for the live sandbox. Start it
    with `env -i` and only these variables, so nothing of the live service is inherited:
    HOME is a sandbox home inside state_dir (every default path under ~ is there: the swarm
    state, the cache, ~/bin, the sanity poke), gh has its own login dir (the sandbox's own
    machine account), the config is the sandbox's own, and each SWARM_* path is in state_dir.
    Raises ValueError with the problems when the settings could touch production."""
    cfg = live_config() if cfg is None else cfg
    problems = live_problems(cfg)
    if problems:
        raise ValueError('; '.join(problems))
    st = Path(os.path.expanduser(str(cfg['state_dir']))).resolve()
    env = {'HOME': str(st / 'home'), 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
           'GH_CONFIG_DIR': str(st / 'gh'), 'TANGENTSWARM_CONFIG': str(st / 'config.toml'),
           'TANGENTSWARM_STATE_DIR': str(st / 'tangentswarm'), 'TANGENTSWARM_CONFIG_DIR': str(st / 'config'),
           'XDG_STATE_HOME': str(st / 'home' / '.local' / 'state'), 'XDG_CONFIG_HOME': str(st / 'home' / '.config'),
           'XDG_CACHE_HOME': str(st / 'home' / '.cache'), 'XDG_DATA_HOME': str(st / 'home' / '.local' / 'share'),
           'SWARM_WEBHOOK_PORT': str(int(cfg['port'])),
           'SWARM_WEBHOOK_SECRETS': ':'.join(os.path.expanduser(str(f)) for f in cfg['secret_files'])}
    for k, name in SANDBOX_FILES.items():
        env[k] = 'methlab' if name is None else str(st / name)
    return env


# -- the command -------------------------------------------------------------------------

def main(argv=None, out=print):
    ap = argparse.ArgumentParser(prog='swarm-methlab', description=__doc__.split('\n', 1)[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('replay', help='run the recorded deliveries through the current rules (read-only)')
    r.add_argument('--db', help='the event DB (default: the live one, opened read-only)')
    r.add_argument('--hours', type=float, help='only deliveries from the last H hours')
    r.add_argument('--limit', type=int, help='only the last N deliveries')
    r.add_argument('--each', action='store_true', help='one line per changed delivery (default: grouped counts)')
    r.add_argument('--all', action='store_true', help='one line per delivery, changed or not')
    sub.add_parser('live-check', help='check the [methlab] live settings')
    sub.add_parser('live-env', help='print the environment for a test copy of swarm-webhook')
    a = ap.parse_args(argv)
    if a.cmd == 'replay':
        import time
        since = time.time() - a.hours * 3600 if a.hours else None
        done = replay(recorded(a.db, since=since, limit=a.limit))
        changed = diff(done)
        if a.each or a.all:
            for d in (done if a.all else changed):
                mark = 'CHANGED' if d in changed else 'same'
                out(f"{mark} {d['delivery']} {d['event']}/{d['payload'].get('action')}: "
                    f"recorded {d['recorded']} now {d['now']}")
        else:
            for (what, rec, now), ds in groups(changed):
                out(f"CHANGED {len(ds)} x {what}: recorded {rec} now {now} (last: {ds[-1]['delivery']})")
        out(f"METHLAB replay: {len(done)} deliveries, {len(changed)} changed")
        return 1 if changed else 0
    if a.cmd == 'live-check':
        problems = live_problems()
        for p in problems:
            out(f'PROBLEM {p}')
        out('METHLAB live settings: ' + ('safe' if not problems else f'{len(problems)} problem(s)'))
        return 1 if problems else 0
    try:
        env = live_env()
    except ValueError as e:
        out(f'METHLAB live settings are not safe: {e}')
        return 1
    import shlex
    for k, v in env.items():
        out(shlex.quote(f'{k}={v}'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
