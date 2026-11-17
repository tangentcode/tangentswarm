"""Agent status reports (tangentswarm #53): an agent tells the swarm it is done, needs input,
is blocked or is working, through swarm-agent's report_status tool over MCP HTTP.

Michal 2026-11-15 7:54 PM ET: "We want an agent to be able to message us and tell us that it's
done, rather than having to wait one minute and then see if its screen has changed ... any
agent should be able to tell us. And it should do it through the MCP server over HTTP."

Two halves:
  accept()   the HTTP side (swarm-agent). It checks the report, writes one events DB row
             ('agent-report', the action is the state) and queues the spool op 'report'.
             The agent needs only the HTTP endpoint: no tmux, no files on its machine.
  execute()  the daemon side (swarm-promoted serves the spool outside swarm-workers.slice,
             where gh works). It checks the identity, acts, and writes one more events DB row
             ('agent-report', '<state> handled') with what it did.

Identity: a report names its PR and its tmux window. The window must be the one the swarm
gives that PR (swarm_parking.resolve), and an MCP session is bound to one PR@window.

Actions:
  done         the worktree must be clean and pushed (else the worker is told why, and it
               keeps running). The Stop-hook handoff advances the card (ready, "@codex
               review", Auto Review; a Codex-clean head gets the MinBot check). Then the
               Claude is closed with /exit once idle (slots park: the window and its shell
               stay, the session is kept for swarm-revive).
  needs-input  the question goes on the ticket (with the Stop hook's QUESTION_MARK, so the
               MinBot pass waits), the card moves to Needs Input, and the Claude is closed
               with /exit (a dirty worktree is fine). When Michal moves the card back to In
               Progress, swarm-webhook's 'answered' action resumes it with the answer.
  blocked      the blocker goes on the ticket and the card moves to Needs Input; the Claude
               keeps running, so a reply can unblock it.
  working      the row only (progress).

The #40 watchdog's pane capture is the fallback only: last_report(pr) tells it that an agent
reported recently, so it does not guess from an unchanged screen.
"""
import hashlib
import json
import re
import time

STATES = ('done', 'needs-input', 'blocked', 'working')
ALIASES = {'needs_input': 'needs-input', 'question': 'needs-input', 'input': 'needs-input',
           'blocker': 'blocked', 'status': 'working', 'progress': 'working'}
EVENT = 'agent-report'
TEXT_MAX = 4000
CLOSE_WAIT = 600   # s the daemon waits for the reporting Claude to end its turn (< spool.RUN_STALE)
POLL = 3
WINDOW_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:+-]{0,99}')
BLOCKED_MARK = '<!-- swarm-blocked -->'


class ReportError(ValueError):
    """A report the swarm refuses (bad state, PR or window, or a missing question)."""


def state_of(state):
    s = (state or '').strip().lower().replace(' ', '-')
    s = ALIASES.get(s, s)
    if s not in STATES:
        raise ReportError(f"state must be one of {', '.join(STATES)} (got {state!r})")
    return s


def check(state, pr, window, message=None, question=None):
    """The normalized report {state, pr, window, message, question}, or ReportError."""
    s = state_of(state)
    try:
        n = int(str(pr).lstrip('#'))
    except (TypeError, ValueError):
        raise ReportError(f"pr must be your PR number (got {pr!r})") from None
    if n <= 0:
        raise ReportError(f"pr must be your PR number (got {pr!r})")
    w = (window or '').strip()
    if not WINDOW_RE.fullmatch(w):
        raise ReportError("window must be your tmux window name (tmux display-message -p '#W')")
    message = (message or '').strip()[:TEXT_MAX]
    question = (question or '').strip()[:TEXT_MAX]
    if s == 'needs-input' and not (question or message):
        raise ReportError('needs-input needs the question: pass question="..."')
    if s == 'blocked' and not message:
        raise ReportError('blocked needs the reason: pass message="..."')
    return dict(state=s, pr=n, window=w, message=message, question=question or (message if s == 'needs-input' else ''))


# -- the events DB -------------------------------------------------------------------------

def log(action, pr, actor, notes=(), error=None, detail=None):
    """One events DB row (source 'mcp'). Uses #32's log_event when it is there, else the
    webhook EventStore's plain add(). Never raises: a report must not fail on its log."""
    from . import eventdb
    notes = [str(x)[:2000] for x in notes]
    try:
        if hasattr(eventdb, 'log_event'):
            return eventdb.log_event(EVENT, source='mcp', action=action, pr=pr, actor=actor, notes=notes,
                                     error=error, detail=detail)
        return eventdb.store().add('event', None, EVENT, decisions=[action], notes=[f"{actor}: {action}"] + notes,
                                   error=error, pr=pr)
    except Exception:
        return None


def reports(pr=None, since=0.0, path=None):
    """The 'agent-report' rows (oldest first), of one PR and newer than since (epoch s)."""
    from . import eventdb
    st = eventdb.EventStore(path) if path else eventdb.store()
    where, args = "event=? AND received>=?", [EVENT, since]
    if pr is not None:
        where += " AND pr=?"
        args.append(int(pr))
    return st.rows(where, tuple(args))


def _act(r):
    """The action of a report row: the state ('done', ...) or '<state> handled' and so on."""
    return r.get('action') or next(iter(json.loads(r.get('decisions') or '[]')), '') or ''


def last_report(pr, within=3600, now=None, path=None):
    """The newest report an agent sent for PR pr in the last `within` s (not the daemon's
    'handled' rows), or None. The #40 watchdog asks this before it guesses from the pane."""
    now = time.time() if now is None else now
    for r in reversed(reports(pr, now - within, path)):
        if _act(r) in STATES:
            return r
    return None


PENDING_TTL = 3600  # s: a report the daemon never handled stops holding the handoff


def pending_input(pr, within=PENDING_TTL, now=None, path=None):
    """The needs-input or blocked report of PR pr that the daemon has not handled yet, or
    None. The Stop-hook handoff waits for it (Codex on PR #54: spool requests run in
    parallel, so the handoff could move the card to Ready before the question was posted).
    The newest row that is not 'working' decides; the row is written before the agent gets
    its answer, so it is there before the agent's turn can end."""
    now = time.time() if now is None else now
    try:
        rows = reports(pr, now - within, path)
    except Exception:
        return None
    for r in reversed(rows):
        a = _act(r)
        if a == 'working':
            continue
        return r if a in ('needs-input', 'blocked') else None
    return None


# -- the HTTP side -------------------------------------------------------------------------

def accept(state, pr, window, message=None, question=None, agent=None, principal=None, submit=None):
    """Check, log and queue one report. Returns the answer for the agent."""
    rep = check(state, pr, window, message, question)
    detail = dict(rep, agent=agent, principal=principal)
    text = rep['question'] if rep['state'] == 'needs-input' else rep['message']
    row = log(rep['state'], rep['pr'], rep['window'], [text] if text else [], detail=detail)
    if rep['state'] == 'working':
        return dict(ok=True, state='working', pr=rep['pr'], event=row, next='noted; keep working')
    if submit is None:
        from . import spool
        submit = spool.submit
    try:
        q = submit('report', '', **dict(detail, event=row))
    except Exception as e:
        log(f"{rep['state']} not queued", rep['pr'], rep['window'], error=f'{type(e).__name__}: {str(e)[:300]}')
        return dict(ok=False, state=rep['state'], pr=rep['pr'], event=row,
                    error=f'the report was logged but its action was not queued: {type(e).__name__}: {e}')
    nxt = {'done': 'end your turn now: once you are idle the swarm hands off the PR and closes this Claude '
                   'with /exit (the window stays)',
           'needs-input': 'end your turn now: the question goes on the ticket, the card moves to Needs Input, '
                          'and this Claude is closed until Michal answers; the answer resumes you',
           'blocked': 'the blocker goes on the ticket and the card moves to Needs Input; wait for a reply'}
    return dict(ok=True, state=rep['state'], pr=rep['pr'], event=row, queued=q.get('queued'), next=nxt[rep['state']])


# -- the daemon side -----------------------------------------------------------------------

class Reporter:
    """Runs one report. Every outside call is injectable for tests."""

    def __init__(self, g=None, sh=None, resolve=None, running=None, handoff=None, park=None, exit_claude=None,
                 tell=None, pushed_head=None, sleep=time.sleep, clock=time.monotonic, close_wait=CLOSE_WAIT,
                 slots_path=None, lock=None):
        self._g, self.sh = g, sh
        self._resolve, self._running, self._handoff = resolve, running, handoff
        self._park, self._exit, self._tell, self._pushed = park, exit_claude, tell, pushed_head
        self.sleep, self.clock, self.close_wait = sleep, clock, close_wait
        self.slots_path = slots_path
        self._lock = lock
        self.lines = []

    # outside calls
    @property
    def g(self):
        if self._g is None:
            from .stophook import hr_gate
            self._g = hr_gate()
        return self._g

    def run_sh(self, argv):
        if self.sh:
            return self.sh(argv)
        from . import tick
        return tick.sh(argv, check=True)

    def resolve(self, pr):
        """(window, worktree, branch) the swarm gives PR pr."""
        if self._resolve:
            return self._resolve(pr)
        from . import slots
        sp = slots.parking()
        window, wt, branch, _ = sp.resolve(slots.make_host(sp), str(pr))
        return window, str(wt), branch

    def claude(self, wt):
        from . import slots
        return next((c for c in (self._running or slots.running)() if c.get('cwd') == str(wt)), None)

    def pushed_head(self, wt):
        from . import stophook
        return (self._pushed or stophook.pushed_head)(wt)

    def handoff(self, wt):
        if self._handoff:
            return self._handoff(wt)
        from . import stophook
        return stophook.run(wt, g=self.g, sh=self.sh, force=True)  # tangentswarm #120: done even with no new push

    def tell(self, pr, text):
        try:
            if self._tell:
                return self._tell(pr, text)
            from . import slots
            return slots.tell_worker(pr, text, out=self.say)
        except Exception as e:
            self.say(f"#{pr}: could not tell the worker: {type(e).__name__}: {str(e)[:160]}")
            return False

    def say(self, line):
        self.lines.append(line)

    # the report
    def identify(self, rep):
        """(worktree, branch); raises ReportError when the window is not the PR's window."""
        try:
            window, wt, branch = self.resolve(rep['pr'])
        except Exception as e:
            raise ReportError(f"#{rep['pr']}: no swarm window or worktree: {str(e)[:200]}") from None
        if window != rep['window']:
            raise ReportError(f"#{rep['pr']} belongs to window {window}, not {rep['window']}: report refused")
        return wt, branch

    def card(self, branch):
        from . import stophook
        found = stophook.find_pr(self.g, branch, issues=True)
        if not found:
            raise ReportError(f"no open PR on the board has branch {branch}")
        return found

    def run(self, rep):
        wt, branch = self.identify(rep)
        n, item, col, _, issues = self.card(branch)
        if n != rep['pr']:
            raise ReportError(f"branch {branch} is PR #{n}, not #{rep['pr']}: report refused")
        getattr(self, 'do_' + rep['state'].replace('-', '_'))(rep, wt, branch, item, col, issues)
        return self.lines

    def do_done(self, rep, wt, branch, item, col, issues):
        n = rep['pr']
        b, head = self.pushed_head(wt)
        if not b or len(head or '') != 40:
            self.say(f"#{n} reported done, but it is not finished: {head}; the Claude keeps running")
            self.tell(n, f"Coordinator: your done report for #{n} was refused: {head}. Commit and push, "
                         "then call report_status(state='done') again.")
            return
        line = self.handoff(wt)
        self.say(f"#{n} done at {head[:7]}: " + (line or 'the handoff had nothing to do (already handed off)'))
        self.close(rep, wt, branch, head, strict=True)

    def post(self, rep, wt, branch, item, issues, kind, text, body):
        """Post text on the ticket once (keyed by its hash, never by the card column: Codex on
        PR #54) and move the card to Needs Input unless it is there already. Returns False when
        the card is in another column (nothing done)."""
        from . import board, stophook
        n = rep['pr']
        key = f"{kind[0]}:{branch}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        where = (issues or [n])[0]
        on = f"{'issue ' if issues else ''}#{where}"
        with stophook.branch_lock(wt):  # the Stop hook's own question path holds it too
            col = self.card(branch)[2]  # under the lock: the Stop hook may have moved it
            if stophook.seen(key):
                self.say(f"#{n}: this {kind} was already posted")
                return True
            if col not in (board.IN_PROGRESS, board.AUTO_REVIEW, board.NEEDS_INPUT):
                self.say(f"#{n} reported {rep['state']}, but its card is in {col}: not moved, not posted")
                return False
            moved = col != board.NEEDS_INPUT
            if moved:
                stophook.move_card(self.g, item, board.NEEDS_INPUT, self.run_sh)
            self.run_sh(['gh', 'issue', 'comment', str(where), '-R', stophook.REPO, '--body', body])
            stophook._mark_seen(key)
        self.say(f"#{n} {rep['state']}: " + ('moved the card to Needs Input and ' if moved else
                                             'the card was already in Needs Input; ') + f"posted the {kind} on {on}")
        return True

    def do_needs_input(self, rep, wt, branch, item, col, issues):
        from . import stophook
        n, q = rep['pr'], rep['question']
        body = (f"{stophook.QUESTION_MARK}\nQuestion from the worker on #{n} (the card is in Needs "
                f"Input; reported with report_status):\n\n" + quote(q))
        if not self.post(rep, wt, branch, item, issues, 'question', q, body):
            return
        _, head = self.pushed_head(wt)
        self.close(rep, wt, branch, head if len(head or '') == 40 else '', strict=False)

    def do_blocked(self, rep, wt, branch, item, col, issues):
        from . import stophook
        n = rep['pr']
        body = (f"{stophook.QUESTION_MARK}\n{BLOCKED_MARK}\nThe worker on #{n} is blocked (the card is in Needs "
                f"Input; reported with report_status):\n\n" + quote(rep['message']))
        if self.post(rep, wt, branch, item, issues, 'blocker', rep['message'], body):
            self.say(f"#{n}: the Claude keeps running")

    def do_working(self, rep, *_):
        self.say(f"#{rep['pr']} working")

    # /exit, keeping the window
    def resume_lock(self, window):
        """The window's resume lock (slots.revive holds it while it resumes or tells the
        Claude); a context manager that yields False when someone else holds it."""
        if self._lock:
            return self._lock(window)
        from . import slots
        sp = slots.parking()
        return sp.flocked(sp.resume_lock(window), blocking=False)

    def superseded(self, rep, branch, rework_before):
        """Why the close is stale, or None (Codex on PR #54): an answer or a relay reached the
        worker while the close waited for it to go idle (slots.revive records a rework entry),
        or the needs-input card was moved out of Needs Input (Michal answered)."""
        from . import board, slots
        if slots.load(self.slots_path)['rework'].get(str(rep['pr'])) != rework_before:
            return 'swarm-revive gave the worker new work while the close waited'
        if rep['state'] == 'needs-input':
            col = self.card(branch)[2]
            if col != board.NEEDS_INPUT:
                return f'the card moved to {col} (answered) while the close waited'
        return None

    def close(self, rep, wt, branch, head, strict):
        """Wait until the Claude of wt is idle (its turn with the report_status call ended),
        record its session for swarm-revive, then /exit it. strict: through slots park
        (clean and pushed worktree; park_one refuses while a resume holds the window); else
        any idle Claude, under the window's resume lock. A close made stale by an answer or
        a revive while it waited is dropped."""
        import contextlib
        from . import slots
        n = rep['pr']
        rework_before = slots.load(self.slots_path)['rework'].get(str(n))
        end = self.clock() + self.close_wait
        while True:
            c = self.claude(wt)
            if not c:
                self.say(f"#{n}: no running Claude in {wt}: nothing to close")
                return False
            if c.get('status') in (None, '', 'idle'):
                c['pr'] = n
                with (contextlib.nullcontext(True) if strict else self.resume_lock(rep['window'])) as got:
                    if not got:
                        why = 'not idle: a resume of this window is running'
                    else:
                        stale = self.superseded(rep, branch, rework_before)
                        if stale:
                            self.say(f"#{n}: the Claude was not closed: {stale}")
                            return False
                        with slots.update(self.slots_path) as st:  # first: the revive needs it if /exit fails
                            st['parked'][str(n)] = dict(session=c.get('session') or '', pane=c.get('pane') or '',
                                                        worktree=str(wt), branch=branch, head=head or '',
                                                        window=rep['window'], parked_at=slots.et_now(),
                                                        reason=rep['state'])
                        why = ((self._park or slots._park)(c, self.say) if strict
                               else (self._exit or exit_idle)(c, self.say))
                        if not why:  # Codex on PR #54: only once closed, under the lock, and only the
                            with slots.update(self.slots_path) as st:  # entry it held (never a new revive's)
                                if st['rework'].get(str(n)) == rework_before:
                                    st['rework'].pop(str(n), None)
                if not why:
                    self.say(f"#{n}: closed the Claude with /exit; window {rep['window']} stays; "
                             f"session {(c.get('session') or '?')[:8]} kept for swarm-revive")
                    return True
                if not busy(why) or self.clock() >= end:
                    self.say(f"#{n}: the Claude was not closed: {why}")
                    return False
            elif self.clock() >= end:
                self.say(f"#{n}: the Claude stayed busy for {self.close_wait}s: not closed")
                return False
            self.sleep(POLL)


def busy(why):
    """True for a park refusal that only means "not idle yet" (try again)."""
    return any(w in (why or '') for w in ('busy', 'prompt is not empty', 'screen changed', 'not idle'))


def quote(text):
    return '\n'.join('> ' + line for line in (text or '').strip()[-1500:].splitlines())


def exit_idle(c, out, wait=30.0, sleep=time.sleep):
    """/exit the Claude in pane c['pane'] when it is idle at an empty prompt, whatever its
    worktree (needs-input may leave work uncommitted; claude --resume brings it back).
    Returns None when it exited, else why not."""
    from . import slots
    sp = slots.parking()
    host = slots.make_host(sp)
    agent, apid = host.agent(c['pane'])
    if agent != 'claude' or not apid:
        return f"pane {c['pane']} is not running Claude in the foreground ({agent or 'none'})"
    screen, blank = host.screen(c['pane'])
    if 'esc to interrupt' in screen:
        return "busy (shows 'esc to interrupt')"
    if not blank:
        return 'prompt is not empty'
    try:
        host.tell(c['pane'], '/exit')
    except Exception as e:
        return f"/exit not sent: {e}"
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not host.pid_alive(apid):
            return None
        sleep(1)
    return f"sent /exit but Claude pid {apid} is still running after {wait:.0f}s"


def execute(req, reporter=None):
    """The spool op 'report' (daemon side). Returns {ok, lines} or {ok: False, error}."""
    rep = {k: req.get(k) for k in ('state', 'pr', 'window', 'message', 'question')}
    r = reporter or Reporter()
    try:
        lines = r.run(rep)
    except ReportError as e:
        r.say(str(e))
        log(f"{rep['state']} refused", rep.get('pr'), rep.get('window'), r.lines, error=str(e)[:300],
            detail={'event': req.get('event')})
        return {'ok': False, 'error': str(e), 'lines': r.lines}
    except Exception as e:
        err = f'{type(e).__name__}: {str(e)[:300]}'
        log(f"{rep['state']} failed", rep.get('pr'), rep.get('window'), r.lines, error=err,
            detail={'event': req.get('event')})
        raise
    log(f"{rep['state']} handled", rep.get('pr'), rep.get('window'), lines, detail={'event': req.get('event')})
    from .stophook import say
    for line in lines:
        say(f"report: {line}")
    return {'ok': True, 'lines': lines}
