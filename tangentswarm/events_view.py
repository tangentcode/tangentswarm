"""swarm-events: a console viewer of the swarm events DB (Michal 2026-11-15; tangentswarm #34).

Opens the event DB read-only and shows the events since a day (today ET by default), then
follows new rows. One row per event:

  ET time  status  event  #PR  actor  outcome

- A GitHub delivery's 'received' and 'handled' rows are one event. The status is handled,
  queued (not handled yet) or failed. The outcome is in plain words ("moved #513 to Done;
  cascade").
- Skipped events (every decision is skip or log-only) and dropped redeliveries are hidden.
- The signature shows only when it FAILED, highlighted. A failed event is red.
- Server action rows (move, park, cascade, ...) are one row each, without empty columns.
- Age shading: the screen is redrawn every --redraw s (default 10). Rows from the last 2
  minutes are bright; each older minute is a darker gray (256 colors).

- The Agents screen (tangentswarm #44; agents_view, livestate): each tmux pane of the swarm,
  with its Claude, active or idle, tests and other processes, memory, CPU, PR and card, under
  the host load and the start capacity. In a terminal the keys are: a agents, e events, q quit.

  swarm-events [--since today|all|YYYY-MM-DD] [-v] [--once] [--db PATH] [--color|--no-color]
               [--redraw S] [--agents]
"""
import argparse
import json
import os
import re
import select
import shutil
import sqlite3
import sys
import termios
import time
import tty as _tty
from datetime import datetime

from . import agents_view, eventdb, livestate

RED, BOLD, RESET = '\033[31m', '\033[1m', '\033[0m'
SIG_BAD = '\033[1;37;41m'  # white on red: a signature failure
CLEAR = '\033[H\033[2J'
FRESH_SECONDS = 120        # rows this young are bright
GRAY_START, GRAY_END = 250, 238  # 256-color grays: one step darker per minute after FRESH_SECONDS
HIDDEN = {'skip', 'log-only'}
PLATFORM = 'tangentcode/platform'


def since_iso(since, now=None):
    """The received_at lower bound for --since (ISO with the ET offset), or '' for all."""
    if since == 'all':
        return ''
    now = now or datetime.now(eventdb.ET)
    if since == 'today':
        day = now.date()
    else:
        day = datetime.strptime(since, '%Y-%m-%d').date()
    return datetime(day.year, day.month, day.day, tzinfo=eventdb.ET).isoformat(timespec='seconds')


def _list(text):
    try:
        v = json.loads(text or '[]')
    except ValueError:
        return [str(text)]
    return v if isinstance(v, list) else [v]


# -- outcome in plain words -----------------------------------------------------------------

WORDS = [  # (regex on an executor line, plain words or None to drop it); the first match wins
    (r'^#(\d+) -> (.+)$', r'moved #\1 to \2'),
    (r'^would move #(\d+) to (.+)$', r'would move #\1 to \2'),
    (r'queued the MinBot check of #(\d+)', r'MinBot check #\1'),
    (r'^cascade ', 'cascade'),
    (r'^would queue a cascade', 'would cascade'),
    (r'woke swarmbot', 'woke swarmbot'),
    (r'swarmbot wake queued', 'swarmbot wake queued'),
    (r'exception already sent', None),
    (r'^#(\d+) relayed to its worker', r'relayed to #\1 worker'),
    (r"^#(\d+): Michal's answer relayed", r'answer relayed to #\1'),
    (r'^#(\d+) merged:', r'#\1 merged; card Done; issues closed'),
    (r'posted "@codex review"', 'asked Codex'),
    (r'^skip', None),
    # a cascade pass's lines that report nothing done (a pass with only these is hidden)
    (r'^cascade: (start|done)', None),
    (r'^promote: Human Review \d+/\d+$', None),
    (r'^promote: (reading the board|nothing)', None),
    (r'^fill: (reading the board|Staging \d+, target \d+: no pull needed)', None),
    (r'^signal: (no Backlog issue|the new-work slots are full)', None),
    (r'^graphql: ', None),
    (r'^--(sweep|promote-missing): ', r'active-gate --\1 failed'),
    (r'^park: (.+) parked', r'parked \1'),
]


def plain(line):
    """One executor line in plain words, or None when it says nothing worth showing."""
    for rx, words in WORDS:
        m = re.search(rx, line)
        if m:
            return m.expand(words) if words and '\\' in words else words
    return line.strip()[:80] or None


def outcome(notes):
    out = []
    for line in notes:
        w = plain(str(line))
        if w and w not in out:
            out.append(w)
    return '; '.join(out)


# -- events from rows (pure) ----------------------------------------------------------------

def events(rows):
    """[event dict] from DB rows, oldest first: a delivery's received and handled rows merged,
    skipped events and dropped redeliveries left out. An event: {t, at, status, event, pr,
    repo, actor, outcome, sig_bad, error, rows}."""
    by_delivery, order = {}, []
    for r in rows:
        rejected = r.get('stage') == 'received' and (r.get('sig_ok') == 0 or r.get('reason') not in (None, 'duplicate'))
        if r.get('stage') in ('received', 'handled') and r.get('delivery') and not rejected:
            key = r['delivery']
            if key not in by_delivery:
                by_delivery[key] = []
                order.append(('d', key))
            by_delivery[key].append(r)
        elif r.get('stage') in ('received', 'handled', 'replay'):
            # Codex on PR #36: each replay run is its own event (they share "replay-N"); a
            # request with no delivery id (a bad length) and a rejected attempt (bad signature,
            # bad JSON, log-only) are shown on their own, never merged with an accepted one
            order.append(('s', r))
        else:
            order.append(('r', r))
    out = []
    for kind, v in order:
        if kind == 'd':
            e = _delivery(by_delivery[v])
            key = ('d', v)
        else:
            e = _delivery([v]) if kind == 's' else _action(v)
            key = ('r', v.get('id'))
        if e:
            e['key'] = key
            out.append(e)
    return out


def _delivery(rs):
    # a duplicate receipt is GitHub redelivering a queued event: never the event itself, but
    # its handled rows still count (Codex on PR #36: the first receipt may be before --since)
    rec = [r for r in rs if r.get('stage') == 'received' and r.get('reason') != 'duplicate']
    rest = [r for r in rs if r.get('stage') != 'received']
    if not rec and not rest:
        return None
    rs = rec + rest
    first = rec[0] if rec else rest[0]
    if first.get('stage') == 'replay':
        rs, first = [first], dict(first, stage='replay')
    done = [r for r in rs if r.get('stage') in ('handled', 'replay')]
    if first.get('stage') == 'replay':
        done = [first]
    sig_bad = first.get('sig_ok') == 0
    decisions = [d for r in rs for d in _list(r.get('decisions'))]
    if not sig_bad and (first.get('reason') == 'log-only' or (decisions and all(d in HIDDEN for d in decisions))):
        return None
    error = '; '.join(r['error'] for r in rs if r.get('error'))
    if sig_bad or error or first.get('reason'):
        status = 'failed'
    elif done:
        status = 'handled'
    else:
        status = 'queued'
    notes = [n for r in done for n in _list(r.get('notes'))]
    ev = first.get('event') or '-'
    if first.get('action'):
        ev += '/' + first['action']
    return dict(t=first.get('received') or 0, at=first.get('received_at') or '', status=status, event=ev,
                pr=first.get('pr') or next((r.get('pr') for r in done if r.get('pr')), None),
                repo=first.get('repo'), actor=first.get('sender'), outcome=outcome(notes), sig_bad=sig_bad,
                error=error or (first.get('reason') or '' if status == 'failed' and not sig_bad else ''), rows=rs)


def _action(r):
    """A server action row (#32: move, park, cascade, ...): one row, no empty columns."""
    if r.get('stage') in ('received', 'handled', 'replay'):
        return None  # a webhook row without a delivery id: nothing to merge it with
    src, what = r.get('source'), r.get('event')
    words = [src] + ([] if what in (src, 'command') else [what]) + [r.get('action')]
    ev = ' '.join(str(x) for x in words if x) or '-'
    notes = outcome(_list(r.get('notes')))
    if src == 'cascade' and what == 'cascade' and not notes and not r.get('error'):
        return None  # a cascade pass that did nothing
    if what == 'command':
        notes = ''  # the command line is the event column already
    return dict(t=r.get('received') or 0, at=r.get('received_at') or '', status='failed' if r.get('error') else '',
                event=ev, pr=r.get('pr'), repo=r.get('repo'), actor=r.get('actor'),
                outcome=notes, sig_bad=False, error=r.get('error') or '', rows=[r])


# -- rendering -------------------------------------------------------------------------------

def shade(age):
    """The 256-color gray for a row of this age in seconds, or None (bright)."""
    if age < FRESH_SECONDS:
        return None
    return max(GRAY_END, GRAY_START - int((age - FRESH_SECONDS) // 60))


def line(e, now=None, color=True):
    """One console line for an event."""
    now = time.time() if now is None else now
    t = (e.get('at') or '')[11:19]
    where = f"#{e['pr']}" if e.get('pr') else ''
    if where and e.get('repo') and e['repo'] != PLATFORM:
        where = e['repo'].split('/')[-1] + where
    cols = [t, f"{e.get('status') or '':<7}", f"{e['event']:<28}", f"{where:<14}", f"{(e.get('actor') or '')[:20]:<20}",
            e.get('outcome') or '']
    text = ' '.join(cols).rstrip()
    err = e.get('error') or ''
    if not color:
        if e.get('sig_bad'):
            return text + ' SIGNATURE FAILED'
        return text + (f'  ERROR: {err}' if err else '')
    if e.get('sig_bad'):
        return f"{text} {SIG_BAD} SIGNATURE FAILED {RESET}"
    if e.get('status') == 'failed':
        return f"{RED}{text}{('  ERROR: ' + err) if err else ''}{RESET}"
    g = shade(now - float(e.get('t') or 0))
    return f"{BOLD}{text}{RESET}" if g is None else f"\033[38;5;{g}m{text}{RESET}"


def full(r):
    d = dict(r)
    body = d.get('body')
    if isinstance(body, (bytes, bytearray)):
        try:
            d['body'] = json.loads(body)
        except ValueError:
            d['body'] = body.decode('utf-8', 'replace')
    for k in ('headers', 'decisions', 'done', 'notes'):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return json.dumps(d, indent=2, default=str)


def connect(path):
    """Read-only: the viewer never writes (and the DB is append-only anyway)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def rows_after(db, last_id, since='', sources=()):
    q, args = 'SELECT * FROM events WHERE id > ? AND received_at >= ?', [last_id, since]
    if sources:  # tangentswarm #32: only these sources (the old DB has only webhook rows)
        has_source = any(r[1] == 'source' for r in db.execute('PRAGMA table_info(events)'))
        if has_source:
            q += f" AND source IN ({', '.join('?' * len(sources))})"
            args += list(sources)
        elif 'webhook' not in sources:
            return []
    return [dict(r) for r in db.execute(q + ' ORDER BY id', args)]


def blocks(rows, now=None, color=True, verbose=False):
    """[(event key, stable text, text)] in order: the event's line, and its rows as JSON with
    -v. The stable text has no color, so the age shading alone is never a change."""
    out = []
    for e in events(rows):
        rows_json = ('\n' + '\n'.join(full(r) for r in e['rows'])) if verbose else ''
        out.append((e['key'], line(e, now, False) + rows_json, line(e, now, color) + rows_json))
    return out


def render(rows, now=None, color=True, verbose=False, height=None, width=None):
    """The lines of one screen: the newest events that fit in height (all when None), each cut
    to width so that none wraps (Codex on PR #48)."""
    out = [l for _, _, text in blocks(rows, now, color, verbose) for l in text.split('\n')]
    if width:
        out = [agents_view.clip(l, width) for l in out]
    return out[-height:] if height else out


class TermKeys:
    """The keys pressed on a terminal stdin, read without Enter (cbreak) while in a with block."""

    def __init__(self, stream=None, sleep=time.sleep):
        self.stream, self.sleep = stream or sys.stdin, sleep
        self.saved = None

    def __enter__(self):
        try:
            fd = self.stream.fileno()
            if os.isatty(fd):
                self.saved = termios.tcgetattr(fd)
                _tty.setcbreak(fd)
        except (OSError, ValueError, termios.error):
            self.saved = None
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, self.saved)

    def __call__(self, timeout):
        """The keys pressed in the next timeout s ('' when none; waits the whole time)."""
        if self.saved is None:
            self.sleep(timeout)
            return ''
        r, _, _ = select.select([self.stream], [], [], timeout)
        return os.read(self.stream.fileno(), 32).decode(errors='replace') if r else ''


def agents_screen(sample, color=True, height=None, top=0):
    """(the lines of the Agents screen, the first row shown after clamping top)."""
    width = shutil.get_terminal_size((200, 40)).columns
    st = sample()
    top = agents_view.clamp(top, len(st['agents']), height) if height else 0
    return agents_view.render(st, color, width, height, top), top


SCROLL = {'j': 1, 'k': -1, '\x1b[B': 1, '\x1b[A': -1}  # a row; space, b, PgDn, PgUp: a page
PAGE = {' ': 1, 'b': -1, '\x1b[6~': 1, '\x1b[5~': -1}


def key_list(pressed):
    """The keys in what was read: an escape sequence (arrow, PgUp/PgDn) is one key."""
    out, i = [], 0
    while i < len(pressed):
        m = re.match(r'\x1b\[[0-9;]*[~A-Za-z]', pressed[i:])
        out.append(m.group(0) if m else pressed[i])
        i += len(out[-1])
    return out


def run(path, since='today', verbose=False, once=False, color=None, out=sys.stdout, sleep=time.sleep,
        polls=None, redraw=10.0, clock=time.time, tty=None, screen='events', keys=None, sample=None,
        sources=()):
    """--once or a pipe: print the events, then append new ones. A terminal: redraw the screen
    on a new event and every `redraw` s, so the age shading stays true. screen='agents' shows
    the Agents screen (once on a pipe); in a terminal, keys(timeout) switches the screen."""
    tty = out.isatty() if tty is None else tty
    color = tty if color is None else color
    lower = since_iso(since)
    if screen == 'agents' and (once or not tty):
        sample = sample or livestate.primed(sleep=sleep)  # Codex on PR #48: measure the CPU
        out.write('\n'.join(agents_screen(sample, color)[0]) + '\n')
        out.flush()
        return 0
    if sample is None:
        watch = livestate.PaneWatch()
        sample = lambda: livestate.collect(watch, now=clock())  # noqa: E731
    if tty and keys is None:
        with TermKeys(sleep=sleep) as k:
            return run(path, since, verbose, once, color, out, sleep, polls, redraw, clock, tty, screen, k, sample)
    # the server makes the DB on its first request; a terminal waits for it in the main loop
    # (Codex on PR #48: the keys must work before it exists)
    while not os.path.exists(path) and not keys:
        if once:
            print(f"swarm-events: no event DB at {path} yet", file=out)
            return 1
        sleep(1)
    db = None
    rows, last, n, shown, drawn, pressed, top = [], 0, 0, None, None, '', 0
    while True:
        switched = False
        height = shutil.get_terminal_size((120, 40)).lines - 1
        for ch in key_list(pressed):
            if ch in ('q', 'Q'):
                return 0
            want = {'a': 'agents', 'e': 'events'}.get(ch.lower())
            if want and want != screen:
                screen, switched = want, True
            elif screen == 'agents' and (ch in SCROLL or ch in PAGE):
                # Codex on PR #48: every pane row must be reachable in a short terminal
                top = max(0, top + SCROLL.get(ch, 0) + PAGE.get(ch, 0) * agents_view.page(height))
                switched = True
        if db is None and os.path.exists(path):
            db = connect(path)
        try:
            got = rows_after(db, last, lower, sources) if db is not None else []
        except sqlite3.OperationalError as e:  # e.g. a busy WAL checkpoint: try again
            got = []
            print(f"swarm-events: {e}", file=sys.stderr)
        if got:
            rows += got
            last = got[-1]['id']
        now = clock()
        if once or not tty:
            # a queued event becomes handled later: print an event again only when it changed,
            # tracked by its identity (Codex on PR #36: by position, one change reprinted the rest)
            shown = shown or {}
            for key, stable, text in blocks(rows, now, color, verbose):
                if shown.get(key) != stable:  # Codex on PR #36: a new shade is not a change
                    print(text, file=out)
                    shown[key] = stable
        elif screen == 'agents':
            if switched or drawn is None or now - drawn >= redraw:
                lines, top = agents_screen(sample, color, height, top)
                out.write(CLEAR + '\n'.join(lines) + '\n')
                drawn = now
        elif got or switched or drawn is None or now - drawn >= redraw:
            width = shutil.get_terminal_size((120, 40)).columns
            out.write(CLEAR + '\n'.join(render(rows, now, color, verbose, height, width)) + '\n')
            drawn = now
        out.flush()
        n += 1
        if once or (polls is not None and n >= polls):
            return 0
        if keys:
            pressed = keys(1.0)
        else:
            sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-events', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--since', default='today', help='today (ET, default), all, or YYYY-MM-DD')
    ap.add_argument('-v', '--verbose', action='store_true', help='print the full rows as JSON')
    ap.add_argument('--once', action='store_true', help='print the events and exit (no follow)')
    ap.add_argument('--db', default=eventdb.PATH)
    ap.add_argument('--source', action='append', default=[],
                    help='only these sources (repeat or comma list): webhook, mcp, stop-hook, launcher, slots, '
                         'relay, minbot, gh, board, cascade, hr-gate')
    ap.add_argument('--redraw', type=float, default=10.0, help='seconds between redraws in a terminal (age shading)')
    ap.add_argument('--color', dest='color', action='store_true', default=None)
    ap.add_argument('--no-color', dest='color', action='store_false')
    ap.add_argument('--agents', action='store_true', help='start on the Agents screen (a pipe or --once: print it once)')
    a = ap.parse_args(argv)
    try:
        since_iso(a.since)
    except ValueError:
        ap.error('--since: today, all or YYYY-MM-DD')
    try:
        sources = tuple(x.strip() for v in a.source for x in v.split(',') if x.strip())
        return run(a.db, a.since, a.verbose, a.once, a.color, redraw=a.redraw,
                   screen='agents' if a.agents else 'events', sources=sources)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
