"""Project 6 worker done: verify, merge and clean up on the server, with no AI (tangentswarm #89).

Before, a Project 6 (tangentswarm, methodology) worker's done report POSTed to MethBot and an
LLM run did fixed steps. Now the swarm does them. Triggers:
  done          the worker MCP handoff in a Project 6 worktree (worker_mcp.p6_done)
  comment       a fallback: the worker's PR comment whose first line is `MERGE-READY <sha>`
                (swarm-webhook, action p6merge)
  merged        the PR's merge event (swarm-webhook do_merged): never a merge; the cleanup of a
                merge the run could not confirm (a merge queue) or one done by hand, or only
                the MethBot POSTs still pending

Verify, all on the exact reported head (problems() lists every check that fails):
  - the PR is open, not a draft, and its base is main
  - head == the reported sha
  - Codex is clean on that head (advance.codex_decision, and the review-state summary of the
    head is completed with has_any_findings and has_unresolved_findings both false: #75/#81)
  - no unresolved review thread; an outdated thread counts too, unless the worker replied
  - every required CI check on the head is green
  - GitHub says the PR is MERGEABLE (UNKNOWN is read again a few times, then fails)
A read that may be cut off fails closed: more than 100 review threads or CI checks is a
reason, and a Codex summary or worker reply that is not in the read is not clean.
A PR that is already MERGED at the head into main gets the cleanup with no merge; one
merged into another base is a reason.

All pass: `gh pr merge --merge --match-head-commit <sha>`. Only when a new read shows the PR
MERGED at the head (a merge queue may only queue it), then (each step on its own; a failed
step is reported and the rest go on):
  - the PR card and the closed issue cards go to Done on Project 6
  - the PR's tmux windows (<code>-<PR>-*: swarm for tangentswarm, meth for methodology) close
  - `git worktree remove` when the worktree is clean (worktrees.remove_worktree), and the
    local branch is deleted; a dirty or busy worktree is reported and left. Only for a head
    branch in this repo (never a fork's), and only when the local branch has no commit that
    the merged head does not have
  - one events DB row
  - a tangentswarm merge wakes swarmbot: a deploy is needed. Only a note: deploys need
    Michal's OK
  - the Project 6 Backlog pull: about LIVE_TARGET live workers (launches still queued count
    too); the next Backlog issues in the strict pick order (board.pick_key) are each queued as
    one Project 6 launch (p6launch, tangentswarm #98: branch, draft PR, cards, brief,
    worktree, window and Claude, with no AI). Triage is never read, so tickets made from
    proposals stay there. A launch that fails is one MethBot exception (p6launch.execute).

Any check fails: no merge, and one exception POST to MethBot's alert URL with the reasons,
once per PR and head (alerts.tell_methbot 'exception'). The whole run holds the once-lock of
the PR and head, so the done report and the comment cannot both merge. A MethBot POST after
the merge (a cleanup exception) that fails stays in a pending file of the PR and
head; the next done report or MERGE-READY comment of that head sends it, with no new merge.
"""
import os
import re
import time
from pathlib import Path

from . import alerts, board

REPOS = alerts.METHBOT_REPOS
WINDOW_CODE = {'tangentcode/tangentswarm': 'swarm', 'tangentcode/methodology': 'meth'}
LIVE_TARGET = int(os.environ.get('SWARM_P6_LIVE_TARGET', '4'))
MERGEABLE_TRIES = 3     # reads of an UNKNOWN mergeable before it fails
MERGEABLE_WAIT = 3.0    # s between those reads
EXIT_WAIT = 20          # s to wait for the worker's processes to end after its window closes
WORKER_LOGIN = 'tangentstorm'  # the workers' gh identity (webhook.MICHAL)
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
MERGE_READY_RE = re.compile(r'^\s*MERGE-READY\s+([0-9a-f]{40})\b')
CI_OK = ('SUCCESS', 'NEUTRAL', 'SKIPPED')

FIELDS = """number state isDraft baseRefName headRefOid headRefName headRepository{nameWithOwner} mergeable
 closingIssuesReferences(first:10){nodes{number state repository{nameWithOwner}}}
 comments(last:60){nodes{author{login} body createdAt}}
 reviews(last:30){nodes{author{login} commit{oid} comments{totalCount}}}
 reviewThreads(last:100){pageInfo{hasPreviousPage}
  nodes{id isResolved isOutdated comments(first:30){nodes{author{login} originalCommit{oid}}}}}
 commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){pageInfo{hasNextPage} nodes{__typename
  ...on CheckRun{name status conclusion isRequired(pullRequestNumber:%d)}
  ...on StatusContext{context state isRequired(pullRequestNumber:%d)}}}}}}}"""

CARD_Q = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
  issueOrPullRequest(number:$n){...on PullRequest{projectItems(first:20){nodes{id project{id}}}}
  ...on Issue{projectItems(first:20){nodes{id project{id}}}}}}}"""


def merge_ready_sha(body):
    """The sha of a comment whose first line is `MERGE-READY <full sha>`, else None."""
    first = next((line for line in (body or '').splitlines() if line.strip()), '')
    m = MERGE_READY_RE.match(first)
    return m.group(1) if m else None


def read(gql, repo, n):
    """The PR's FIELDS (one GraphQL read), or {}."""
    owner, name = repo.split('/')
    q = f'query{{repository(owner:"{owner}",name:"{name}"){{p: pullRequest(number:{int(n)}){{{FIELDS % (int(n), int(n))}}}}}}}'
    d = gql(q)
    return ((d or {}).get('repository') or {}).get('p') or {}


def _login(node):
    return (((node or {}).get('author') or {}).get('login') or '').lower()


def unresolved_threads(pr):
    """The review threads that block a merge: every thread that is not resolved. An outdated
    thread counts too, unless the worker replied in it (a comment after the first by
    WORKER_LOGIN, the gh identity the workers post as). Other reviewers' replies do not count."""
    out = []
    for t in (pr.get('reviewThreads') or {}).get('nodes') or []:
        if t.get('isResolved'):
            continue
        cs = (t.get('comments') or {}).get('nodes') or []
        # Codex on PR #90: only the worker's own login (its gh identity) is a worker reply
        replied = any(_login(c) == WORKER_LOGIN for c in cs[1:])
        if t.get('isOutdated') and replied:
            continue
        out.append(t.get('id') or '?')
    return out


def codex_problem(pr, head):
    """Why Codex is not clean on head, or None (the #75/#81 logic: advance.codex_decision is
    clean, and the head's review-state summary is completed with both findings flags false)."""
    from . import advance, codexsummary
    decision, detail = advance.codex_decision(pr)
    if decision != 'clean':
        return f"Codex is not clean on {head[:8]}: {detail}"
    summary = codexsummary.on_head((pr.get('comments') or {}).get('nodes') or [], head, advance._codex)
    if summary != codexsummary.CLEAN:
        return (f"Codex is not clean on {head[:8]}: no completed review-state summary of the head with "
                f"has_any_findings and has_unresolved_findings false ({summary or 'no summary'})")
    return None


def ci_problem(pr, head):
    """Why the required CI on head is not green, or None. Only required checks count; a
    repo with none required passes."""
    commit = (((pr.get('commits') or {}).get('nodes') or [{}])[-1].get('commit') or {})
    if commit.get('oid') and commit['oid'] != head:
        return f"the CI read is for {commit['oid'][:8]}, not {head[:8]}"
    contexts = (commit.get('statusCheckRollup') or {}).get('contexts') or {}
    if (contexts.get('pageInfo') or {}).get('hasNextPage'):  # a required check may be on the next page
        return 'more than 100 CI checks on the head: the required ones cannot all be read'
    ctx = contexts.get('nodes') or []
    bad, running = [], []
    for c in ctx:
        if not c.get('isRequired'):
            continue
        if c.get('__typename') == 'StatusContext':
            name, state = c.get('context'), (c.get('state') or '').upper()
            if state in ('PENDING', 'EXPECTED'):
                running.append(name)
            elif state != 'SUCCESS':
                bad.append(f'"{name}" {state.lower()}')
            continue
        name = c.get('name')
        if (c.get('status') or '').upper() != 'COMPLETED':
            running.append(name)
        elif (c.get('conclusion') or '').upper() not in CI_OK:
            bad.append(f'"{name}" {(c.get("conclusion") or "no result").lower()}')
    if bad:
        return 'required CI is not green: ' + ', '.join(bad)
    if running:
        return 'required CI is still running: ' + ', '.join(f'"{x}"' for x in running)
    return None


def problems(pr, head):
    """Every reason PR pr may not merge at head; [] when it may."""
    if not pr:
        return ['the PR could not be read']
    out = []
    if pr.get('state') != 'OPEN':
        out.append(f"the PR is {(pr.get('state') or 'not open').lower()}")
    if pr.get('isDraft'):
        out.append('the PR is a draft')
    if pr.get('baseRefName') and pr.get('baseRefName') != 'main':
        out.append(f"the PR targets {pr.get('baseRefName')}, not main")
    now = pr.get('headRefOid') or ''
    if now != head:
        out.append(f"the PR head is {now[:8] or 'unknown'}, not the reported {head[:8]}")
        return out  # the other checks would read another head
    why = codex_problem(pr, head)
    if why:
        out.append(why)
    if ((pr.get('reviewThreads') or {}).get('pageInfo') or {}).get('hasPreviousPage'):
        out.append('more than 100 review threads: they cannot all be read')  # Codex on PR #90: no part read
    threads = unresolved_threads(pr)
    if threads:
        out.append(f"{len(threads)} unresolved review thread(s)")
    why = ci_problem(pr, head)
    if why:
        out.append(why)
    m = pr.get('mergeable')
    if m != 'MERGEABLE':
        out.append('the PR conflicts with main' if m == 'CONFLICTING'
                   else f"GitHub has not confirmed the PR merges cleanly (mergeable {m})")
    return out


class P6Merge:
    """One run for one PR and head. Every side effect is a method or an injected callable, so
    the tests run with no network, no tmux and no real git."""

    def __init__(self, repo, n, head, trigger, gql=None, sh=None, tell=None, wake=None, tmux=None,
                 remove=None, procs=None, live=None, backlog=None, sleep=time.sleep, wt=None, st=None,
                 now=time.time, delbranch=None, tipcheck=None, launch=None, inflight=None):
        self.repo, self.n, self.head, self.trigger = repo, int(n), head, trigger
        self._gql, self._sh, self.tell = gql, sh, tell or alerts.tell_methbot
        self.wake = wake or (lambda why, key, pr: alerts.wake_swarmbot(why, key, pr=pr))
        self._tmux, self._remove, self._procs = tmux, remove, procs
        self._live, self._backlog, self._delbranch, self._tipcheck = live, backlog, delbranch, tipcheck
        self._launch, self._inflight = launch, inflight
        self.sleep, self.wt, self.st, self.now = sleep, wt, st, now
        self.notes = []

    # -- tools ------------------------------------------------------------------------------
    def gql(self, q, **v):
        if self._gql is None:
            from .advance import hr_gate
            self._gql = hr_gate()['gql']
        return self._gql(q, **v)

    def sh(self, argv):
        if self._sh:
            return self._sh(argv)
        from . import tick
        return tick.sh(argv, check=True).stdout

    def tmux(self, *args):
        if self._tmux:
            return self._tmux(list(args))
        import subprocess
        r = subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError(f"tmux {args[0]}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    def note(self, line):
        self.notes.append(line)

    # -- the run ----------------------------------------------------------------------------
    def run(self):
        if self.repo not in REPOS:
            return {'merged': False, 'why': f"{self.repo} is not a Project 6 repo"}
        if not SHA_RE.match(self.head or ''):
            return {'merged': False, 'why': f"the full head sha is needed (got {self.head!r})"}
        st = self.st or alerts.state()
        once = f"p6merge:{self.repo}#{self.n}@{self.head}"
        with alerts.once_lock(once):
            if alerts.done_now(st, once):
                left = self.deliver([])  # Codex on PR #90: a POST that failed after the merge goes now
                return {'merged': True, 'already': True, 'pr': self.n, 'repo': self.repo, 'head': self.head,
                        'notes': self.notes, 'undelivered': left,
                        'why': f"{self.repo}#{self.n} at {self.head[:8]} was already merged by the swarm"}
            pr, why = self.verify()
            # merged at this head into main (Codex on PR #90: a merge into another base is not done)
            elsewhere = pr.get('state') == 'MERGED' and pr.get('headRefOid') == self.head \
                and pr.get('baseRefName') == 'main'
            if why and not elsewhere:
                return self.refuse(why)
            if elsewhere:
                self.note(f"{self.repo}#{self.n} was already merged at {self.head[:8]}: cleanup only")
            else:
                try:
                    self.sh(['gh', 'pr', 'merge', str(self.n), '-R', self.repo, '--merge',
                             '--match-head-commit', self.head])
                except Exception as e:
                    return self.refuse([f"gh pr merge failed: {type(e).__name__}: {str(e)[:200]}"])
                why = self.confirm_merged()
                if why:  # Codex on PR #90: a merge queue only queues it; no cleanup until GitHub merged it
                    return self.refuse([why])
                self.note(f"merged {self.repo}#{self.n} at {self.head[:8]}")
            st.mark(once)
        self.after_merge(pr)
        return {'merged': True, 'pr': self.n, 'repo': self.repo, 'head': self.head, 'notes': self.notes}

    def verify(self):
        """(pr, [reasons]): a fresh read; GitHub's UNKNOWN mergeable is read again."""
        pr = {}
        for i in range(MERGEABLE_TRIES):
            try:
                pr = read(self.gql, self.repo, self.n)
            except Exception as e:
                return {}, [f"the PR could not be read: {type(e).__name__}: {str(e)[:160]}"]
            if pr.get('mergeable') != 'UNKNOWN' or pr.get('state') != 'OPEN':
                break
            if i + 1 < MERGEABLE_TRIES:
                self.sleep(MERGEABLE_WAIT)
        return pr, problems(pr, self.head)

    def confirm_merged(self):
        """None when GitHub shows the PR MERGED at the head (read again a few times), else why
        not. The merge command can end with the PR only in a merge queue."""
        state = '?'
        for i in range(MERGEABLE_TRIES):
            try:
                pr = read(self.gql, self.repo, self.n)
            except Exception as e:
                pr, state = {}, f"not readable ({type(e).__name__})"
            else:
                if pr.get('state') == 'MERGED' and pr.get('headRefOid') == self.head:
                    return None
                state = f"{(pr.get('state') or '?').lower()} at {(pr.get('headRefOid') or '?')[:8]}"
            if i + 1 < MERGEABLE_TRIES:
                self.sleep(MERGEABLE_WAIT)
        return (f"gh pr merge ran, but GitHub does not show the PR merged at {self.head[:8]} (it is {state}; "
                "a merge queue?): no cleanup yet; the next report of this head, or the merge event, finishes it")

    def refuse(self, why):
        """No merge: one exception to MethBot (once per PR and head) and an events row."""
        from . import eventdb
        text = '; '.join(why)
        sent = self.tell('exception', self.repo, self.n, head=self.head,
                         reason=f"{self.repo}#{self.n} was not merged at {self.head[:8]} ({self.trigger}): {text}")
        eventdb.log_event('p6-merge', source='p6merge', action='refused', pr=self.n, repo=self.repo,
                          notes=[text], detail={'head': self.head, 'trigger': self.trigger, 'methbot': str(sent)})
        return {'merged': False, 'pr': self.n, 'repo': self.repo, 'head': self.head, 'why': text,
                'methbot': sent is True or sent == 'already sent'}

    def after_merge(self, pr):
        from . import eventdb
        problems_, notices = [], []
        for step in (self.cards, self.close_windows, self.cleanup_worktree):
            try:
                step(pr)
            except Exception as e:
                problems_.append(f"{step.__name__}: {type(e).__name__}: {str(e)[:200]}")
        eventdb.log_event('p6-merge', source='p6merge', action='merged', pr=self.n, repo=self.repo,
                          notes=self.notes + problems_, detail={'head': self.head, 'trigger': self.trigger})
        for step in (self.deploy_note, self.pull):
            try:
                notices += step(pr) or []
            except Exception as e:
                problems_.append(f"{step.__name__}: {type(e).__name__}: {str(e)[:200]}")
        if problems_:
            self.notes += problems_
            notices.append({'kind': 'exception', 'key': 'cleanup',
                            'reason': f"{self.repo}#{self.n} merged at {self.head[:8]}, but: " + '; '.join(problems_)})
        self.deliver(notices)

    # -- MethBot POSTs after the merge: kept until delivered (Codex on PR #90) -------------------
    def pending_path(self):
        import hashlib
        key = hashlib.sha1(f"{self.repo}#{self.n}@{self.head}".encode()).hexdigest()[:20]
        return Path(alerts.STATE).parent / 'p6merge-pending' / f"{key}.json"  # read at call time: tests move STATE

    def deliver(self, new):
        """Add the new notices ({kind, key, reason}) to this PR and head's pending file, then
        POST each one. A delivered notice (or one MethBot already has) leaves the file; a
        failed POST stays for the next done report or MERGE-READY comment of this head, which
        never merges again. Returns the notices still pending."""
        import json
        path = self.pending_path()
        with alerts.once_lock(f"p6merge-pending:{self.repo}#{self.n}@{self.head}"):
            try:
                pending = json.loads(path.read_text())
            except (OSError, ValueError):
                pending = []
            pending += new
            left = []
            for x in pending:
                sent = self.tell(x['kind'], self.repo, self.n, head=self.head, reason=x['reason'], key=x.get('key', ''))
                if sent is True or sent == 'already sent':
                    self.note(f"MethBot {x['kind']} sent")
                else:
                    left.append(x)
                    self.note(f"MethBot {x['kind']} not sent: {sent}; it is sent again on the next report")
            if left:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(left))
                os.replace(tmp, path)
            else:
                path.unlink(missing_ok=True)
        return left

    # -- after the merge ----------------------------------------------------------------------
    def card(self, n):
        """The item id of PR or issue n on its board, or None."""
        b = board.for_repo(self.repo)
        owner, name = self.repo.split('/')
        d = self.gql(CARD_Q, owner=owner, name=name, n=int(n))
        node = (((d or {}).get('repository') or {}).get('issueOrPullRequest') or {})
        for it in (node.get('projectItems') or {}).get('nodes') or []:
            if b is not None and ((it.get('project') or {}).get('id')) == b.id:
                return it['id']
        return None

    def to_done(self, n, what):
        item = self.card(n)
        if not item:
            return self.note(f"{what} #{n} has no Project 6 card")
        board.move_card({}, item, board.DONE, self.sh, n=n, b=board.for_repo(self.repo))
        self.note(f"{what} #{n} card -> Done")

    def cards(self, pr):
        self.to_done(self.n, 'PR')
        for i in (pr.get('closingIssuesReferences') or {}).get('nodes') or []:
            num = i.get('number')
            if (i.get('repository') or {}).get('nameWithOwner') != self.repo or not num:
                continue  # never a same-number issue of another repo
            if i.get('state') == 'OPEN':  # GitHub closes it on the merge; this makes sure
                try:
                    self.sh(['gh', 'issue', 'close', str(num), '-R', self.repo, '--reason', 'completed'])
                except Exception as e:
                    self.note(f"issue #{num} close: {str(e)[:120]}")
            self.to_done(num, 'issue')

    def windows(self):
        """[(window id, name)] of the PR's worker windows: <code>-<PR> or <code>-<PR>-*."""
        code = WINDOW_CODE[self.repo]
        out = []
        for line in (self.tmux('list-windows', '-a', '-F', '#{window_id}\t#{window_name}') or '').splitlines():
            wid, _, name = line.partition('\t')
            if name == f"{code}-{self.n}" or name.startswith(f"{code}-{self.n}-"):
                out.append((wid, name))
        return out

    def close_windows(self, pr):
        found = self.windows()
        if not found:
            return self.note(f"no {WINDOW_CODE[self.repo]}-{self.n}-* window")
        for wid, name in found:
            self.tmux('kill-window', '-t', wid)
            self.note(f"closed window {name}")

    def worktree(self, pr):
        """The worker worktree path: the done report's, else <agent root>/<repo name>.<slug>."""
        if self.wt:
            return Path(self.wt)
        from . import registry, worktrees
        branch = pr.get('headRefName') or ''
        return Path(registry.agent_root()) / f"{self.repo.split('/')[1]}.{worktrees.branch_slug(branch)}"

    def cleanup_worktree(self, pr):
        from . import worktrees
        branch = pr.get('headRefName') or ''
        if not branch:
            return self.note('no head branch name: worktree and branch left')
        head_repo = (pr.get('headRepository') or {}).get('nameWithOwner')
        if head_repo != self.repo:  # Codex on PR #90: a fork's branch name says nothing about ours
            return self.note(f"the head branch is in {head_repo or 'an unknown repo'}, not {self.repo}: "
                             "no local worktree or branch is touched")
        why = (self._tipcheck or self.tip_problem)(branch)
        if why:  # never remove a worktree whose branch has work the merge does not have
            raise RuntimeError(f"worktree and branch {branch} left: {why}")
        path = self.worktree(pr)
        procs = self._procs or worktrees.processes_in
        end = self.now() + EXIT_WAIT
        while path.exists() and procs(str(path)) and self.now() < end:
            self.sleep(1)  # the closed window's processes end on SIGHUP
        try:
            out = (self._remove or worktrees.remove_worktree)(self.repo.split('/')[1], branch, delete_branch=True)
        except worktrees.WorktreeError as e:
            if 'no worktree of' not in str(e):
                raise RuntimeError(f"worktree {path} left: {str(e)[:300]}") from None
            self.note(f"no worktree has {branch}")
            return self.delete_branch(branch)
        self.note(f"removed worktree {out.get('path') or path}")
        if out.get('branch_deleted'):
            self.note(f"deleted local branch {branch}")
        else:
            raise RuntimeError(f"local branch {branch} kept: {out.get('branch_kept') or 'not deleted'}")

    def tip_problem(self, branch):
        """Why the local branch must stay, or None: it has commits that are not in the merged
        head. A missing local branch is no problem (the cleanup reports it)."""
        from . import worktrees
        repo_path = worktrees.plain_repo(self.repo.split('/')[1])
        r = worktrees.git(['rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'], repo_path, check=False)
        tip = (r.stdout or '').strip()
        if r.returncode or not tip or tip == self.head:
            return None
        if worktrees._ok(['merge-base', '--is-ancestor', tip, self.head], repo_path):
            return None
        return f"its tip {tip[:8]} has commits that are not in the merged head {self.head[:8]}"

    def delete_branch(self, branch):
        """Delete the local branch when no worktree has it: only when it exists and is merged
        into origin/<default> (the same rule as worktrees.remove_worktree)."""
        if self._delbranch:
            return self.note(self._delbranch(self.repo.split('/')[1], branch))
        from . import worktrees
        repo_path = worktrees.plain_repo(self.repo.split('/')[1])
        if not worktrees._ok(['rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'], repo_path):
            return self.note(f"no local branch {branch}")
        worktrees.git(['fetch', '--prune', 'origin'], repo_path, check=False)
        default = worktrees.default_branch(repo_path)
        if not worktrees._ok(['merge-base', '--is-ancestor', f'refs/heads/{branch}', f'refs/remotes/origin/{default}'],
                             repo_path):
            raise RuntimeError(f"local branch {branch} kept: it is not merged into origin/{default}")
        worktrees.git(['branch', '-D', branch], repo_path)
        self.note(f"deleted local branch {branch}")

    def deploy_note(self, pr):
        if self.repo != 'tangentcode/tangentswarm':
            return
        why = (f"tangentcode/tangentswarm#{self.n} merged at {self.head[:8]}: a deploy is needed. "
               "Notify only: deploys need Michal's OK.")
        self.note(self.wake(why, f"p6-deploy:{self.n}@{self.head}", self.n))  # the wake queue retries a failed POST

    def pull(self, pr):
        """The Project 6 Backlog pull: free = LIVE_TARGET - live workers - launches still queued;
        the next free Backlog issues in pick order (not one already queued) are each queued as
        one Project 6 launch (tangentswarm #98: before, they went to MethBot in a 'pull' POST).
        The daemon runs each launch in its own process, so this run does not wait for a Claude
        to start. Returns no notices: a launch that fails tells MethBot itself; a pick that
        could not be queued raises, and after_merge reports it as a cleanup exception."""
        from . import p6launch
        b = board.for_repo(self.repo)
        live = (self._live or live_workers)()
        queued = (self._inflight or p6launch.in_flight)()
        free = LIVE_TARGET - live - len(queued)
        if free <= 0:
            return self.note(f"Backlog pull: {live} live Project 6 workers and {len(queued)} queued launches "
                             f"(target {LIVE_TARGET}): none pulled")
        picks = [i for i in (self._backlog or (lambda: backlog_issues(self.gql, b)))()
                 if (i['repo'], int(i['number'])) not in queued][:free]
        if not picks:
            return self.note(f"Backlog pull: {live} live workers, no Project 6 Backlog issue to start")
        after = {'repo': self.repo, 'pr': self.n, 'head': self.head}
        launch = self._launch or (lambda i: p6launch.queue(i['repo'], i['number'], i.get('title') or '', after=after))
        done, failed = [], []
        for i in picks:
            name = f"{i['repo']}#{i['number']}"
            try:
                launch(i)
                done.append(name)
            except Exception as e:  # the next pick still goes
                failed.append(f"{name} ({type(e).__name__}: {str(e)[:120]})")
        if done:
            self.note(f"Backlog pull: queued launches of {', '.join(done)} ({live} live workers, target {LIVE_TARGET})")
        if failed:
            raise RuntimeError('Backlog pull: not queued: ' + ', '.join(failed))
        return []


def live_workers(panes=None, root=None):
    """The number of Project 6 worktrees with a live Claude in a tmux pane."""
    from . import registry
    root = Path(root or registry.agent_root())
    if panes is None:
        import subprocess
        r = subprocess.run(['tmux', 'list-panes', '-a', '-F', '#{pane_current_path}\t#{pane_current_command}'],
                           capture_output=True, text=True, timeout=30)
        panes = [tuple(line.split('\t', 1)) for line in r.stdout.splitlines() if '\t' in line]
    seen = set()
    for path, cmd in panes:
        p = Path(path)
        top = next((x for x in [p, *p.parents] if x.parent == root), None)
        if top is None or cmd not in ('claude', 'node'):
            continue
        if any(top.name.startswith(f"{r.split('/')[1]}.") and len(top.name) > len(r.split('/')[1]) + 1
               for r in REPOS):
            seen.add(top.name)
    return len(seen)


ISSUES_Q = """query($org:String!,$n:Int!,$c:String){organization(login:$org){projectV2(number:$n){
 items(first:100, after:$c){pageInfo{hasNextPage endCursor} nodes{id
  fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on Issue{number state title repository{nameWithOwner}
   milestone{title dueOn} labels(first:15){nodes{name}}}}}}}}}"""


def backlog_issues(gql, b, org='tangentcode'):
    """The OPEN issues of board b's repos in its Backlog column, in the strict pick order
    (board.pick_key: fast-track by the lowest number, then the milestone due date, then the
    number). Only Backlog: an issue in Triage (a ticket made from a proposal) is never pulled."""
    out, cursor = [], None
    while True:
        d = gql(ISSUES_Q, org=org, n=b.number, c=cursor)
        items = d['organization']['projectV2']['items']
        for it in items['nodes']:
            c = it.get('content') or {}
            repo = (c.get('repository') or {}).get('nameWithOwner')
            if c.get('__typename') != 'Issue' or c.get('state') != 'OPEN' or repo not in b.repos:
                continue
            if board.column(it.get('fieldValueByName')) != board.BACKLOG:
                continue
            names = [l['name'] for l in (c.get('labels') or {}).get('nodes') or []]
            due, ms = board.soonest_due([c.get('milestone') or {}])
            out.append(dict(number=c['number'], repo=repo, title=c.get('title') or '', item=it['id'],
                            labels=names, ft=board.is_fast_track(names), due=due, milestone=ms))
        if not items['pageInfo']['hasNextPage']:
            break
        cursor = items['pageInfo']['endCursor']
    return sorted(out, key=lambda i: board.pick_key(i, None))


def run(repo, n, head, trigger, **kw):
    """Verify, merge and clean up one Project 6 PR at head. Never raises."""
    try:
        return P6Merge(repo, n, head, trigger, **kw).run()
    except Exception as e:
        from . import eventdb
        eventdb.log_event('p6-merge', source='p6merge', action='error', pr=int(n), repo=repo,
                          error=f"{type(e).__name__}: {str(e)[:300]}", detail={'head': head, 'trigger': trigger})
        return {'merged': False, 'pr': int(n), 'repo': repo, 'head': head,
                'why': f"the merge run failed: {type(e).__name__}: {str(e)[:200]}"}
