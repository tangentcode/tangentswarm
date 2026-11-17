"""swarm-api: the Swarm status API (tangentswarm #24, part of #23).

A loopback HTTP service under /api/ that the platform `swarm` app (the Petri-net control
panel, tangentcode/platform#471) and later a coordinator read and write. It is read-mostly.
It adds no new way to change the board: each human action calls the same functions the
swarm's own tools call (board.move_card, the gh write of tick.sh, the hub client).

    swarm-api [--host 127.0.0.1] [--port 8779]

It binds only a loopback address. The swarm host's front proxy routes /api/ to it. The app
(/) and the MCP (/mcp/*, /login, /token, /authorize, /register, /revoke, /.well-known/)
are other routes of that proxy; this service answers only /api/* (anything else is 404).

Read endpoints (one swarm = one configured board; {b} is its project number):

  GET  /api/health                         {ok}  (no auth)
  GET  /api/whoami                         the caller's identity
  GET  /api/swarms                         the boards the swarm runs
  GET  /api/swarms/{b}/places              each place (column) with its open cards and count
  GET  /api/swarms/{b}/transitions         the legal steps, who fires each, the rules, the limits
  GET  /api/swarms/{b}/events              the events DB as an audit log, each row marked
                                           handled, unhandled, pending, dropped or error
                                           (?since=ID&limit=N&source=S&pr=N)
  GET  /api/swarms/{b}/messages            the swarm-hub messages, marked handled (read) or
                                           unhandled (?since=ID&limit=N)
  GET  /api/swarms/{b}/lease               the coordinator lease and the serial gate lock

Human-action endpoints (JSON body; a human identity only, see below):

  POST /api/swarms/{b}/cards/{n}/triage    {"action": "approve"|"backlog"|"active"|"discuss",
                                            "repo": optional}
  POST /api/swarms/{b}/cards/{n}/answer    {"body": "...", "repo": optional}: the answer goes
                                           where the question is (the linked issue whose
                                           latest comment is the question, else the card
                                           itself) as a comment, then the card moves
                                           Needs Input -> In Progress (swarm-webhook then
                                           relays it to the worker, as for a move by hand)
  POST /api/swarms/{b}/messages/{id}/answer {"body": "..."}: a reply to a hub question or
                                           blocker, sent to its sender; the question is acked

Auth (see AuthConfig). Every /api/ route except /api/health needs one of:

  * Work login (browser). The front proxy does the work login (its redirect goes to the
    auth host) and forwards two headers: X-Swarm-User (the login) and
    X-Swarm-Proxy-Secret (a shared secret, ~/.config/tangentswarm/api-proxy-secret, mode
    0600). The user header counts only with the right secret, so a local process cannot
    claim to be a human. A proxy user is a signed-in human: read and write (when
    $TANGENTSWARM_API_HUMANS is set, write only for the logins it lists).
  * Machine credential. Authorization: Bearer <token> from `swarm auth token issue
    --client NAME --scopes tangentswarm:status`. tangentswarm:status reads. A write also
    needs tangentswarm:act and a --subject (the human the credential represents).
    Tokens for another resource (an MCP token) do not count.

A browser write must also be JSON (Content-Type: application/json) and, when the
browser sends Origin, same-origin. There is no CORS.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import auth as A
from . import board, eventdb, paths

DEFAULT_PORT = int(os.environ.get('SWARM_API_PORT', '8779'))
LOOPBACK = ('127.0.0.1', '::1', 'localhost')
MAX_BODY = 64 * 1024
CARDS_MAX_AGE = float(os.environ.get('SWARM_API_CARDS_MAX_AGE', '30'))  # reads of a board without boardcache
PENDING_GRACE = 300   # a received delivery without a handled row is 'pending' this long, then 'unhandled'

SCOPE_STATUS = A.SCOPE_STATUS
SCOPE_ACT = A.SCOPE_ACT

USER_HEADER = 'x-swarm-user'
SECRET_HEADER = 'x-swarm-proxy-secret'

# triage action -> the column the card goes to. approve puts the card in Backlog, the
# column the launcher takes work from (slots.backlog_issues).
TRIAGE_ACTIONS = {'approve': board.BACKLOG, 'backlog': board.BACKLOG, 'active': board.ACTIVE,
                  'discuss': board.DISCUSS}
TRIAGE_FROM = (board.INBOX, board.TRIAGE, board.DISCUSS, board.BACKLOG, board.ACTIVE)
DISPLAY = {board.STAGING: 'Staging'}   # the internal key "Pending" is shown as Staging


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ---------------------------------------------------------------------------
# auth

@dataclass
class AuthConfig:
    proxy_secret: str | None = None        # None: no browser (proxy) logins
    humans: tuple = ()                     # empty: every proxy user may write
    resource_url: str | None = None        # a token with another resource does not count
    store_factory: object = None           # () -> auth.AuthStore

    @classmethod
    def load(cls, env=None):
        env = os.environ if env is None else env
        path = Path(os.path.expanduser(env.get('TANGENTSWARM_API_PROXY_SECRET_FILE')
                                       or str(paths.config_dir() / 'api-proxy-secret')))
        secret = None
        try:
            st = path.stat()
            if st.st_mode & 0o077:
                raise ApiError(500, f'{path} is readable by group/others; chmod 600 it')
            secret = path.read_text().strip() or None
        except FileNotFoundError:
            pass
        if secret is not None and len(secret) < 32:
            raise ApiError(500, f'the proxy secret in {path} is shorter than 32 characters')
        humans = tuple(h for h in (env.get('TANGENTSWARM_API_HUMANS') or '').replace(',', ' ').split() if h)
        return cls(proxy_secret=secret, humans=humans, resource_url=env.get('TANGENTSWARM_API_RESOURCE_URL'))

    def store(self):
        return (self.store_factory or A.AuthStore)()


@dataclass
class Identity:
    kind: str                  # 'human' (proxy login) or 'token'
    name: str                  # the login, or the token's client id
    human: str | None          # the human this identity is or represents (None: a machine only)
    scopes: list = field(default_factory=list)

    @property
    def can_write(self):
        return bool(self.human) and SCOPE_ACT in self.scopes

    def as_dict(self):
        return {'kind': self.kind, 'name': self.name, 'human': self.human, 'scopes': list(self.scopes),
                'can_write': self.can_write}


def _same(a, b):
    return hmac.compare_digest(a.encode('utf-8', 'surrogateescape'), b.encode('utf-8', 'surrogateescape'))


def authenticate(headers, cfg: AuthConfig):
    """The caller's Identity, or None. headers: {lower-case name: value}."""
    user = (headers.get(USER_HEADER) or '').strip()
    secret = headers.get(SECRET_HEADER)
    if cfg.proxy_secret and secret is not None and _same(secret.strip(), cfg.proxy_secret):
        if not user:
            return None
        writer = not cfg.humans or user in cfg.humans
        return Identity('human', user, user, [SCOPE_STATUS] + ([SCOPE_ACT] if writer else []))
    auth = (headers.get('authorization') or '').strip()
    if auth[:7].lower() != 'bearer ':
        return None
    token = auth[7:].strip()
    if not token:
        return None
    row = cfg.store().lookup_token(token, kind='access')
    if not row:
        return None
    if row.get('resource') and row['resource'] != cfg.resource_url:
        return None   # a token for another server (an MCP token) is not an API credential
    scopes = (row.get('scopes') or '').split()
    if SCOPE_STATUS not in scopes:
        return None
    # only an act token names a human (`swarm auth token issue` refuses act without --subject)
    human = (row.get('subject') or None) if SCOPE_ACT in scopes else None
    return Identity('token', row['client_id'], human, scopes)


# ---------------------------------------------------------------------------
# data sources (each one injectable, so tests never touch GitHub, the hub or the live DBs)

ITEMS_Q = """query($org:String!,$n:Int!,$c:String){organization(login:$org){projectV2(number:$n){
 items(first:100, after:$c){pageInfo{hasNextPage endCursor} nodes{id
  fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on PullRequest{number title state isDraft headRefOid repository{nameWithOwner}
   milestone{title dueOn} labels(first:15){nodes{name}}
   closingIssuesReferences(first:5){nodes{number repository{nameWithOwner} milestone{title dueOn}
    labels(first:10){nodes{name}}}}}
   ...on Issue{number title state repository{nameWithOwner} milestone{title dueOn}
   labels(first:10){nodes{name}}}}}}}}}"""


def _labels(c):
    return [l['name'] for l in ((c or {}).get('labels') or {}).get('nodes') or []]


def _card(it, c):
    """One card, with the same priority inputs the pickers give board.pick_key (Codex on PR
    #74; promoted.read_board, boards.open_prs): the labels of the card and of the issues a PR
    closes in its own repo, and the soonest milestone due date among them."""
    kind = 'pr' if c.get('__typename') == 'PullRequest' else 'issue'
    repo = (c.get('repository') or {}).get('nameWithOwner') or ''
    closes = [i for i in ((c.get('closingIssuesReferences') or {}).get('nodes') or [])
              if (i.get('repository') or {}).get('nameWithOwner', repo) == repo]   # never another repo's #N
    own = _labels(c)
    labels = own + [l for i in closes for l in _labels(i)]
    due, ms = board.soonest_due([c.get('milestone') or {}] + [i.get('milestone') or {} for i in closes])
    return {'number': c['number'], 'repo': repo, 'kind': kind, 'title': c.get('title') or '',
            'item': it['id'], 'col': board.column(it.get('fieldValueByName')),
            'draft': bool(c.get('isDraft')), 'head': c.get('headRefOid'), 'labels': labels, 'own_labels': own,
            'closes': [i['number'] for i in closes], 'due': due, 'milestone': ms,
            'ft': board.is_fast_track(labels),
            'url': f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{c['number']}"}


def default_gql(q, **v):
    from . import hookgate
    return hookgate.gql(q, **v)


class Sources:
    """Where the API reads and writes. Every member can be replaced (tests inject fakes)."""

    def __init__(self, gql=None, gate=None, sh=None, events=None, hub=None, lock=None, queued=None):
        self.gql = gql or default_gql
        self._gate = gate                  # () -> the hr-gate namespace (Project 4 moves)
        self.sh = sh                       # argv -> run a gh write (default tick.sh, which logs it)
        self._events = events              # () -> eventdb.EventStore
        self.hub = hub                     # the hub client module (queue.client)
        self.lock = lock                   # gatelock.LOCK path
        self.queued = queued               # () -> gatelock.queued()
        self._cards = {}                   # board number -> (time, cards): another board's last read
        self.clock = time.monotonic

    def events(self):
        return self._events() if self._events else eventdb.store()

    def hub_client(self):
        if self.hub is None:
            from .queue import client
            return client
        return self.hub

    def gate(self):
        if self._gate is None:
            from . import advance
            self._gate = advance.hr_gate
        return self._gate()

    def run(self, argv):
        if self.sh:
            return self.sh(argv)
        from . import tick
        return tick.sh(argv, check=True)

    def cards(self, b, fresh=False):
        """[card] of the open PRs and issues on board b (its repos only). Project 4 comes from
        the shared boardcache snapshot; another board from one paged read, kept CARDS_MAX_AGE
        seconds for reads. fresh (every human action) always reads the board now."""
        if b is board.DEFAULT:
            from . import boardcache
            snap = boardcache.snapshot(self.gql, 0 if fresh else None)
            return [_card(it, c) for it, c in boardcache.open_prs(snap) + boardcache.open_issues(snap)]
        hit = self._cards.get(b.number)
        if not fresh and hit and self.clock() - hit[0] < CARDS_MAX_AGE:
            return hit[1]
        out = self._read_cards(b)
        self._cards[b.number] = (self.clock(), out)
        return out

    def _read_cards(self, b):
        out, cursor = [], None
        while True:
            items = self.gql(ITEMS_Q, org='tangentcode', n=b.number, c=cursor)['organization']['projectV2']['items']
            for it in items['nodes']:
                c = it.get('content') or {}
                if (c.get('__typename') in ('PullRequest', 'Issue') and c.get('state') == 'OPEN'
                        and (c.get('repository') or {}).get('nameWithOwner') in b.repos):
                    out.append(_card(it, c))
            if not items['pageInfo']['hasNextPage']:
                return out
            cursor = items['pageInfo']['endCursor']

    def move(self, b, card, col):
        """The swarm's one card mover (board.move_card): Project 4 through hr-gate's
        namespace, another board by its own project and option ids."""
        if b is board.DEFAULT:
            board.move_card(self.gate(), card['item'], col, sh=self.sh, n=card['number'])
        else:
            board.move_card({}, card['item'], col, sh=self.sh, n=card['number'], b=b)
            self._cards.pop(b.number, None)


# ---------------------------------------------------------------------------
# the read views

def swarm(b_number):
    b = next((x for x in board.BOARDS if x.number == b_number), None)
    if b is None:
        raise ApiError(404, f'no swarm runs board {b_number}')
    return b


def swarms():
    return [{'board': b.number, 'repos': list(b.repos), 'staging': b.staging,
             'path': f'/api/swarms/{b.number}'} for b in board.BOARDS]


def places(b, cards, active=None):
    """[{key, name, count, cards}] in board order; a card in an unknown column gets its own place."""
    by = {}
    for c in cards:
        by.setdefault(c['col'] or 'No Status', []).append(c)
    keys = [k for k in board.COLUMNS if k != board.STAGING or b.staging]
    keys += sorted(k for k in by if k not in keys)
    out = []
    for k in keys:
        cs = sorted(by.get(k, []), key=lambda c: board.pick_key(c, active))
        out.append({'key': k, 'name': DISPLAY.get(k, k), 'count': len(cs), 'cards': cs})
    return out


def _t(name, frm, to, by, rule, human=False, action=None):
    return {'name': name, 'from': list(frm), 'to': to, 'by': by, 'rule': rule, 'human': human,
            'action': action}


def transitions(b):
    """The legal steps of board b: who fires each (a tool, or a human), and the rule."""
    from . import boardflow, flow
    work = (board.AUTO_REVIEW, board.READY) + ((board.STAGING,) if b.staging else ())
    hr = flow.HR_TARGET if b.staging else boardflow.HR_TARGET
    ts = [
        _t('new issue', ['(none)'], board.TRIAGE, 'swarm-webhook', 'a new issue gets its card in Triage'),
        _t('new PR', ['(none)'], board.IN_PROGRESS, 'swarm-webhook',
           'a new draft PR gets its card in In Progress (a ready PR: Auto Review)'),
    ]
    for action, to in TRIAGE_ACTIONS.items():
        frm = [c for c in TRIAGE_FROM if c != to]
        rule = ('approve: the card goes to Backlog, where the launcher takes work' if action == 'approve'
                else f'a human moves the card to {to}')
        ts.append(_t(f'triage: {action}', frm, to, 'human', rule, True,
                     f'POST /api/swarms/{b.number}/cards/{{n}}/triage {{"action": "{action}"}}'))
    ts += [
        _t('launch', [board.BACKLOG], board.IN_PROGRESS, 'swarm-slots',
           'a free slot starts a worker on the top Backlog issue (board.pick_key); its draft PR is In Progress'),
        _t('handoff', [board.IN_PROGRESS], board.AUTO_REVIEW, 'swarm-stop-hook / swarm-webhook',
           'a pushed head: the PR is marked ready, "@codex review" once per head'),
        _t('needs input', [board.IN_PROGRESS, board.AUTO_REVIEW], board.NEEDS_INPUT, 'agent-report',
           'the worker asks a question or is blocked; the question goes on the ticket'),
        _t('answer', [board.NEEDS_INPUT], board.IN_PROGRESS, 'human',
           'a human answers on the ticket and moves the card; swarm-webhook relays the answer to the worker',
           True, f'POST /api/swarms/{b.number}/cards/{{n}}/answer {{"body": "..."}}'),
        _t('back to work', list(work), board.IN_PROGRESS, 'swarm-webhook',
           'Codex findings on the head, red CI, a merge conflict or a human comment: relayed to the worker'),
        _t('MinBot pass', [board.AUTO_REVIEW], board.READY, 'swarm-advance --minbot',
           'not a draft, mergeable, Codex clean on the exact head, CI green, no open question'),
    ]
    if b.staging:
        ts += [
            _t('pull', [board.READY], board.STAGING, 'swarm-flow',
               f'Staging holds fewer than {flow.STAGING_MIN}: the top Ready PR that stacks cleanly is merged '
               f'into work and gated (never more than {flow.STAGING_MAX})'),
            _t('promote', [board.STAGING], board.HUMAN_REVIEW, 'pending-promote',
               f'Human Review holds fewer than {hr}: the top Staging PR (board.pick_key)'),
        ]
    else:
        ts.append(_t('promote', [board.READY], board.HUMAN_REVIEW, 'swarm-advance --promote-board',
                     f'Human Review holds fewer than {hr}: the top Ready PR, after the MinBot gate again'))
    ts += [
        _t('reject', [board.HUMAN_REVIEW], board.IN_PROGRESS, 'human',
           "a human sends the PR back; swarm-webhook relays the comments since the head", True),
        _t('merged', [board.HUMAN_REVIEW], board.DONE, 'swarm-webhook',
           'the PR is merged: card Done, its issues closed, the worker parked'),
        _t('closed', ['(any)'], board.CANCELED, 'swarm-webhook', 'the PR is closed without a merge'),
    ]
    limits = {board.HUMAN_REVIEW: {'target': hr}}
    if b.staging:
        limits[board.STAGING] = {'target': flow.STAGING_TARGET, 'min': flow.STAGING_MIN, 'max': flow.STAGING_MAX}
    return {'board': b.number, 'transitions': ts, 'limits': limits}


AUDIT_COLS = ('id', 'stage', 'source', 'event', 'action', 'delivery', 'repo', 'pr', 'sender', 'actor',
              'received', 'received_at', 'reason', 'error')


def _state(r, handled, now):
    """handled | unhandled | pending | dropped | error, and why."""
    if r['error']:
        return 'error', r['error']
    if r['stage'] == 'received':
        if r['reason'] or r['sig_ok'] == 0:
            return 'dropped', r['reason'] or 'bad signature'
        if r['delivery'] in handled:
            return 'handled', None
        if now - (r['received'] or 0) < PENDING_GRACE:
            return 'pending', 'no handled row yet'
        return 'unhandled', 'received, but no handled row'
    return 'handled', None


def _json_list(s):
    try:
        v = json.loads(s or '[]')
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def audit(store, since=None, limit=100, source=None, pr=None, now=None):
    """{events: [row], next}: rows of the events DB, oldest first, each with its state. No
    raw webhook headers or bodies (the 'received' rows keep them for replay only)."""
    now = time.time() if now is None else now
    limit = max(1, min(int(limit), 1000))
    where, args = [], []
    if source:
        where.append('source = ?')
        args.append(source)
    if pr is not None:
        where.append('pr = ?')
        args.append(int(pr))
    if since is not None:
        where.append('id > ?')
        args.append(int(since))
        cond = ' AND '.join(where) or '1'
        rows = store.rows(f"{cond} AND id IN (SELECT id FROM events WHERE {cond} ORDER BY id LIMIT ?)",
                          tuple(args) * 2 + (limit,))
    else:   # the latest page
        cond = ' AND '.join(where) or '1'
        rows = store.rows(f"id IN (SELECT id FROM events WHERE {cond} ORDER BY id DESC LIMIT ?)",
                          tuple(args) + (limit,))
    deliveries = sorted({r['delivery'] for r in rows if r['stage'] == 'received' and r['delivery']})
    handled = set()
    for i in range(0, len(deliveries), 500):
        part = deliveries[i:i + 500]
        handled |= {r['delivery'] for r in store.rows(
            f"stage = 'handled' AND delivery IN ({', '.join('?' * len(part))})", tuple(part))}
    out = []
    for r in rows:
        state, why = _state(r, handled, now)
        row = {k: r.get(k) for k in AUDIT_COLS}
        row.update(state=state, why=why, done=_json_list(r.get('done')), notes=_json_list(r.get('notes')),
                   decisions=_json_list(r.get('decisions')))
        if r['stage'] == 'event' and r.get('body'):
            try:
                row['detail'] = json.loads(r['body'])
            except ValueError:
                row['detail'] = None
        out.append(row)
    return {'events': out, 'next': out[-1]['id'] if out else since}


def messages(hub, since=0, limit=100):
    """{messages: [msg], next}: the hub's messages after id since, oldest first; read = handled."""
    limit = max(1, min(int(limit), 1000))
    msgs = hub.read_messages('swarm-api', since=int(since or 0), limit=limit)
    for m in msgs:
        m['state'] = 'handled' if m.get('read') else 'unhandled'
    return {'messages': msgs, 'next': msgs[-1]['id'] if msgs else since}


def lease(src):
    """The coordinator lease. #23 plans it; until a lease store exists, holder is None. The
    serial gate lock (gatelock) is shown too: its holder note and the queued requests. The
    lock itself is never probed (a probe would take it for a moment)."""
    from . import gatelock
    lock = Path(src.lock or gatelock.LOCK)
    note = None
    try:
        note = gatelock.holder_path(lock).read_text().strip() or None
    except OSError:
        pass
    try:
        q = (src.queued or gatelock.queued)()
    except OSError:
        q = []
    return {'coordinator': {'holder': None, 'kind': None, 'expires_at': None, 'handoff_to': None,
                            'status': 'none', 'note': 'no coordinator lease exists yet (planned in #23)'},
            'serial_lock': {'holder': note, 'queued': [{'argv': r.get('argv'), 'by': r.get('by'), 'at': r.get('at')}
                                                       for _, r in q]}}


# ---------------------------------------------------------------------------
# the human actions

def find_card(src, b, n, repo=None):
    """The open card n on board b, read fresh (never from a stale snapshot)."""
    cs = [c for c in src.cards(b, fresh=True) if c['number'] == n and (not repo or c['repo'] == repo)]
    if not cs:
        raise ApiError(404, f"no open card #{n} on board {b.number}" + (f" in {repo}" if repo else ''))
    if len(cs) > 1:
        raise ApiError(409, f"#{n} is on board {b.number} in {', '.join(c['repo'] for c in cs)}: give its repo")
    return cs[0]


def _record(who, action, card, b, **extra):
    eventdb.log_event('human-action', source='api', action=action, pr=card['number'], actor=who.human,
                      repo=card['repo'], detail=dict(board=b.number, item=card['item'], col=card['col'],
                                                     via=who.kind, credential=who.name, **extra))


def triage(src, b, n, action, who, repo=None):
    to = TRIAGE_ACTIONS.get(action)
    if to is None:
        raise ApiError(400, f"action must be one of {', '.join(TRIAGE_ACTIONS)}")
    card = find_card(src, b, n, repo)
    if card['col'] not in TRIAGE_FROM:
        raise ApiError(409, f"#{n} is in {card['col']}: triage moves only cards in {', '.join(TRIAGE_FROM)}")
    if card['col'] == to:
        return {'ok': True, 'changed': False, 'number': n, 'col': to}
    _record(who, f'triage {action}', card, b, to=to)
    src.move(b, card, to)
    return {'ok': True, 'changed': True, 'number': n, 'from': card['col'], 'col': to}


def answer(src, b, n, body, who, repo=None):
    body = (body or '').strip()
    if not body:
        raise ApiError(400, 'body is required')
    card = find_card(src, b, n, repo)
    if card['col'] != board.NEEDS_INPUT:
        raise ApiError(409, f"#{n} is in {card['col']}, not Needs Input")
    on = question_targets(src, card)
    _record(who, 'answer', card, b, to=board.IN_PROGRESS, on=[f'{r}#{i}' for r, i in on])
    for r, i in on:
        src.run(['gh', 'issue', 'comment', str(i), '-R', r, '--body', body])
    src.move(b, card, board.IN_PROGRESS)
    return {'ok': True, 'number': n, 'from': board.NEEDS_INPUT, 'col': board.IN_PROGRESS,
            'answered_on': [f'{r}#{i}' for r, i in on]}


QUESTION_Q = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
 pullRequest(number:$n){closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}
  comments(last:1){nodes{body}}}}}}}"""


def question_targets(src, card):
    """[(repo, number)] where the answer goes: where the question is. The Stop hook and
    report_status post a PR's question on the issue it closes (stophook.needs_input,
    agent_report), and advance.open_question blocks the MinBot pass while that issue's latest
    comment is still the question (Codex on PR #74). So: every issue the PR closes whose
    latest comment has QUESTION_MARK; else the card itself (an issue card, or a PR that
    asked on itself)."""
    from .stophook import QUESTION_MARK
    if card['kind'] == 'pr':
        owner, name = card['repo'].split('/', 1)
        d = src.gql(QUESTION_Q, owner=owner, name=name, n=card['number'])
        pr = ((d or {}).get('repository') or {}).get('pullRequest') or {}
        out = []
        for i in (pr.get('closingIssuesReferences') or {}).get('nodes') or []:
            last = ((i.get('comments') or {}).get('nodes') or [{}])[-1]
            if QUESTION_MARK in (last.get('body') or ''):
                out.append(((i.get('repository') or {}).get('nameWithOwner') or card['repo'], i['number']))
        if out:
            return out
    return [(card['repo'], card['number'])]


def answer_message(hub, mid, body, who):
    body = (body or '').strip()
    if not body:
        raise ApiError(400, 'body is required')
    got = hub.read_messages('swarm-api', since=int(mid) - 1, limit=1)
    msg = got[0] if got and got[0]['id'] == int(mid) else None
    if msg is None:
        raise ApiError(404, f'no hub message {mid}')
    if msg['type'] not in ('question', 'blocker'):
        raise ApiError(409, f"message {mid} is a {msg['type']}, not a question or blocker")
    eventdb.log_event('human-action', source='api', action='answer message', actor=who.human,
                      detail=dict(message=int(mid), to=msg['sender'], via=who.kind, credential=who.name))
    sent = hub.send_message(f'human:{who.human}', msg['sender'], 'status', body,
                            json.dumps({'in_reply_to': int(mid)}))
    hub.ack(int(mid))
    return {'ok': True, 'message': int(mid), 'reply': sent.get('id'), 'to': msg['sender']}


# ---------------------------------------------------------------------------
# routing

ROUTES = []


def route(method, pattern, write=False):
    rx = re.compile('^' + pattern + '$')

    def deco(fn):
        ROUTES.append((method, rx, write, fn))
        return fn
    return deco


def _int(qs, k, default=None):
    v = qs.get(k)
    if v in (None, ''):
        return default
    try:
        return int(v)
    except ValueError:
        raise ApiError(400, f'{k} must be a number') from None


@route('GET', r'/api/whoami')
def _whoami(ctx, who, qs, body):
    return who.as_dict()


@route('GET', r'/api/swarms')
def _swarms(ctx, who, qs, body):
    return {'swarms': swarms()}


@route('GET', r'/api/swarms/(?P<b>\d+)/places')
def _places(ctx, who, qs, body, b):
    bd = swarm(int(b))
    # the active fast-track ticket is a Project 4 number (boardflow.promote passes None elsewhere)
    active = board.active_fast_track() if bd is board.DEFAULT else None
    return {'board': bd.number, 'at': time.time(), 'active_fast_track': active,
            'places': places(bd, ctx.src.cards(bd), active)}


@route('GET', r'/api/swarms/(?P<b>\d+)/transitions')
def _transitions(ctx, who, qs, body, b):
    return transitions(swarm(int(b)))


@route('GET', r'/api/swarms/(?P<b>\d+)/events')
def _events(ctx, who, qs, body, b):
    swarm(int(b))
    return audit(ctx.src.events(), _int(qs, 'since'), _int(qs, 'limit', 100), qs.get('source') or None,
                 _int(qs, 'pr'))


@route('GET', r'/api/swarms/(?P<b>\d+)/messages')
def _messages(ctx, who, qs, body, b):
    swarm(int(b))
    return messages(ctx.src.hub_client(), _int(qs, 'since', 0), _int(qs, 'limit', 100))


@route('GET', r'/api/swarms/(?P<b>\d+)/lease')
def _lease(ctx, who, qs, body, b):
    swarm(int(b))
    return lease(ctx.src)


@route('POST', r'/api/swarms/(?P<b>\d+)/cards/(?P<n>\d+)/triage', write=True)
def _triage(ctx, who, qs, body, b, n):
    return triage(ctx.src, swarm(int(b)), int(n), body.get('action'), who, body.get('repo'))


@route('POST', r'/api/swarms/(?P<b>\d+)/cards/(?P<n>\d+)/answer', write=True)
def _answer(ctx, who, qs, body, b, n):
    return answer(ctx.src, swarm(int(b)), int(n), body.get('body'), who, body.get('repo'))


@route('POST', r'/api/swarms/(?P<b>\d+)/messages/(?P<mid>\d+)/answer', write=True)
def _answer_message(ctx, who, qs, body, b, mid):
    swarm(int(b))
    return answer_message(ctx.src.hub_client(), int(mid), body.get('body'), who)


class Api:
    """handle(method, target, headers, body bytes) -> (status, {header: value}, JSON-able)."""

    def __init__(self, src: Sources | None = None, auth: AuthConfig | None = None):
        self.src = src or Sources()
        self.auth = auth if auth is not None else AuthConfig.load()

    def handle(self, method, target, headers, raw=b''):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        parts = urlsplit(target)
        path = parts.path.rstrip('/') or '/'
        try:
            if not path.startswith('/api/') and path != '/api':
                raise ApiError(404, 'not found: this service serves /api/ only')
            if path == '/api/health':
                return self._ok({'ok': True})
            matched = [(m, rx.match(path), w, fn) for m, rx, w, fn in ROUTES if rx.match(path)]
            if not matched:
                raise ApiError(404, f'no route {path}')
            hit = next(((rm, w, fn) for m, rm, w, fn in matched if m == method), None)
            if hit is None:
                return self._err(405, f'{method} is not allowed on {path}',
                                 {'Allow': ', '.join(sorted({m for m, *_ in matched}))})
            rm, write, fn = hit
            who = authenticate(h, self.auth)
            if who is None:
                return self._err(401, 'sign in with the work login, or send Authorization: Bearer <token> '
                                      f'with scope {SCOPE_STATUS}',
                                 {'WWW-Authenticate': 'Bearer realm="tangentswarm-api"'})
            body = {}
            if write:
                if not who.can_write:
                    raise ApiError(403, f'a human action needs a signed-in human (or a {SCOPE_ACT} token '
                                        'issued with --subject)')
                body = self._json_body(h, raw)
            qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
            return self._ok(fn(self, who, qs, body, **rm.groupdict()))
        except ApiError as e:
            return self._err(e.status, e.message)
        except Exception as e:   # the reply never carries a traceback
            return self._err(502, f'{type(e).__name__}: {str(e)[:300]}')

    @staticmethod
    def _json_body(h, raw):
        ctype = (h.get('content-type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            raise ApiError(415, 'a human action needs Content-Type: application/json')
        origin = h.get('origin')
        if origin and origin != 'null':
            o = urlsplit(origin)
            fwd = (h.get('x-forwarded-host') or '').split(',')[0].strip()
            if o.netloc not in {x for x in (h.get('host'), fwd) if x}:
                raise ApiError(403, f'cross-origin request from {origin}')
        elif origin == 'null':
            raise ApiError(403, 'cross-origin request from an opaque origin')
        if len(raw or b'') > MAX_BODY:
            raise ApiError(413, 'body too large')
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            raise ApiError(400, 'the body is not JSON') from None
        if not isinstance(body, dict):
            raise ApiError(400, 'the body must be a JSON object')
        return body

    @staticmethod
    def _ok(obj):
        return 200, {}, obj

    @staticmethod
    def _err(status, message, headers=None):
        return status, dict(headers or {}), {'error': message}


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'swarm-api'

        def _go(self, method):
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                status, extra, obj = 413, {}, {'error': 'body too large'}
            else:
                raw = self.rfile.read(n) if n else b''
                status, extra, obj = api.handle(method, self.path, dict(self.headers.items()), raw)
            data = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go('GET')

        def do_POST(self):
            self._go('POST')

        def do_PUT(self):
            self._go('PUT')

        def do_DELETE(self):
            self._go('DELETE')

        def log_message(self, fmt, *args):   # one line per request, never a header or body
            sys.stderr.write(f"[swarm-api] {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}\n")
    return Handler


def serve(api, host='127.0.0.1', port=DEFAULT_PORT):
    if host not in LOOPBACK:
        raise SystemExit(f'swarm-api binds only a loopback address, not {host}: the front proxy routes /api/ to it')
    cls = ThreadingHTTPServer
    if host == '::1':   # Codex on PR #74: ThreadingHTTPServer binds AF_INET only
        cls = type('ThreadingHTTPServer6', (ThreadingHTTPServer,), {'address_family': socket.AF_INET6})
    return cls((host, port), make_handler(api))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-api', description='the Swarm status API (loopback only)')
    ap.add_argument('--host', default='127.0.0.1', choices=LOOPBACK)
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    eventdb.set_source('api')
    try:
        api = Api()
    except ApiError as e:
        print(f'swarm-api: {e.message}', file=sys.stderr)
        return 2
    srv = serve(api, a.host, a.port)
    print(f"[swarm-api] http://{a.host}:{srv.server_address[1]}/api/ "
          f"(work login {'on' if api.auth.proxy_secret else 'off: no proxy secret'}, tokens with {SCOPE_STATUS})",
          file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
