"""swarm-tmux-status: a standalone console table of the swarm tmux windows (tangentswarm #49).

Each window in the swarm session (default `agents`) gets one class, from tmux state only:

  working   Claude runs and is busy ("esc to interrupt" shows under its input box)
  idle      Claude runs but does no work
  shell     no Claude, only a shell prompt
  program   no Claude, another program runs in the foreground (shown with its name)

A row shows the class, the window, the ticket/PR from the window name and the last activity
line. The default view lists every window with a Claude, working or idle (#119); `a` toggles
all windows, shells and programs included. An idle row says why it is idle: how long it has
been idle (the Claude session record), its PR, draft state and board column (the board
snapshot file, never fetched here) and what it last said. There should be no idle Claudes. Up/down (or
j/k) select a row, Enter (or z) zooms into its window's recent output, Esc (or q) goes back.

  swarm-tmux-status [--session NAME] [--all] [--once] [--zoom WINDOW] [--interval S]
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field

from . import boardcache, livestate, tmux

SESSION = os.environ.get('SWARM_TMUX_SESSION', 'agents')
SHELLS = {'bash', 'sh', 'zsh', 'fish', 'dash', 'ksh', 'tcsh', 'csh'}
CLAUDE_COMMANDS = {'claude', 'claude.exe'}
BUSY_RE = re.compile(r'esc to interrupt|ctrl\+c to interrupt', re.I)
SPINNER_RE = re.compile(r'^[^\w\s●⎿]\s+\S.*…')   # '✻ Thinking… (12s · esc to interrupt)'
TICKET_RE = re.compile(r'^([A-Za-z]+)-(\d+)(?:-(.*))?$')
ESC_RE = re.compile(r'\x1b(?:\[[0-9;:?]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|.)', re.S)
CLASSES = ('working', 'idle', 'shell', 'program')
ZOOM_HISTORY = 200


# -- pure classification (no tmux) -----------------------------------------------------------

def _is_bar(line):
    return '─' in line and len(line.replace('─', '').strip()) < 15


def claude_box(lines):
    """(top, bottom) indices of Claude Code's input box: the two lowest ─ bars with a ❯
    line between them. None if the screen has no such box."""
    bars = [i for i, line in enumerate(lines) if _is_bar(line)]
    for top, bottom in zip(reversed(bars[:-1]), reversed(bars[1:])):
        if any('❯' in lines[i] for i in range(top + 1, bottom)):
            return top, bottom
    return None


def is_claude(command, screen):
    """True if the pane runs Claude: its foreground command is claude, or it is node (how
    some installs show) and the screen has Claude's input box."""
    if command in CLAUDE_COMMANDS:
        return True
    return command == 'node' and claude_box(screen.split('\n')) is not None


def is_busy(screen):
    """True if Claude shows it is working. Only the status area counts (the spinner line
    above the input box and the footer below it), so text in the transcript never does."""
    lines = screen.rstrip('\n').split('\n')
    box = claude_box(lines)
    if not box:
        return any(BUSY_RE.search(line) for line in lines[-6:])
    above = [line.strip() for line in lines[:box[0]] if line.strip()]
    area = lines[box[1] + 1:] + ([above[-1]] if above and SPINNER_RE.match(above[-1]) else [])
    return any(BUSY_RE.search(line) for line in area)


def classify(command, screen):
    """The class of one pane from its foreground command and its screen text."""
    if is_claude(command, screen):
        return 'working' if is_busy(screen) else 'idle'
    if command in SHELLS:
        return 'shell'
    return 'program'


PROMPT_RE = re.compile(r'^[$#%>❯]\s*$|^\S+@[\w.-]+:\S')   # a bare prompt, or a user@host:path line


def last_activity(screen, cls):
    """One line that says what the pane does. For Claude: the first line of its last
    transcript entry (a message or a tool call, marked ●) above the input box. For a shell:
    the last line that is not the prompt. For a program: the last non-empty line."""
    lines = [line.strip() for line in screen.rstrip('\n').split('\n')]
    if cls in ('working', 'idle'):
        box = claude_box(lines)
        body = [line for line in (lines[:box[0]] if box else lines) if line and not _is_bar(line)]
        for line in reversed(body):
            if line.startswith('●'):
                return line.lstrip('●').strip()
        return body[-1] if body else ''
    lines = [line for line in lines if line]
    if cls == 'shell':
        lines = [line for line in lines if not PROMPT_RE.search(line)]
    return lines[-1] if lines else ''


def ticket(window_name):
    """The ticket from a window name: 'mn-458-jt-viewmat' -> 'mn #458'. '' if none."""
    m = TICKET_RE.match(window_name or '')
    return f'{m.group(1)} #{m.group(2)}' if m else ''


# -- windows ----------------------------------------------------------------------------------

@dataclass
class Window:
    index: int
    name: str
    pane: str           # the pane the row reports on
    command: str
    cls: str
    activity: str
    path: str = ''
    panes: int = 1
    pid: int = 0        # the pane's process id
    idle_s: object = None   # seconds idle (idle Claude only), None when unknown
    branch: str = ''
    pr: object = None   # the open PR of the branch on the board, or None
    draft: bool = False
    card: str = ''      # its board column

    @property
    def ticket(self):
        return ticket(self.name)


RANK = {'working': 0, 'idle': 1, 'program': 2, 'shell': 3}


def window_rows(panes, screens):
    """One Window per tmux window. `panes` are tmux.list_panes() dicts; `screens` maps a
    pane id to its plain screen text. A window with several panes reports its busiest
    pane: working Claude, then idle Claude, then a program, then a shell; ties go to the
    active pane, then the lowest pane index."""
    by_window = {}
    for p in panes:
        if p.get('dead'):
            continue
        by_window.setdefault(p['window_index'], []).append(p)
    rows = []
    for index in sorted(by_window):
        group = by_window[index]
        scored = []
        for p in group:
            screen = screens.get(p['pane_id'], '')
            cls = classify(p['current_command'], screen)
            scored.append((RANK[cls], not p.get('active'), p['pane_index'], cls, p, screen))
        _, _, _, cls, p, screen = min(scored, key=lambda s: s[:3])
        rows.append(Window(index=index, name=p['window_name'], pane=p['pane_id'],
                           command=p['current_command'], cls=cls,
                           activity=last_activity(screen, cls), path=p.get('current_path', ''),
                           panes=len(group), pid=p.get('pid') or 0))
    return rows


def strip_escapes(text):
    return ESC_RE.sub('', text)


def board_prs(path=None):
    """{branch: (PR number, draft?, board column)} of the open PRs in the board snapshot file
    ({} when there is none). It is never fetched here."""
    try:
        snap = json.loads((path or boardcache.PATH).read_text())
        return {c.get('headRefName'): (c['number'], bool(c.get('isDraft')), boardcache.col(it) or '')
                for it, c in boardcache.open_prs(snap)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def git_of(path):
    """(origin repo or None, branch or '') of the worktree that holds path (path may be a
    subdirectory of it), from its files (no git process)."""
    try:
        repo, branch = livestate.git_of(path) if path else (None, None)
    except OSError:
        repo, branch = None, None
    return repo, branch or ''


def add_idle_info(windows, table=None, sess=None, cards=None, git=git_of, now=None):
    """Fill in why each idle Claude is idle: idle_s from its Claude session record
    (statusUpdatedAt of the claude process in the pane's process tree), and the branch of its
    directory with the branch's open PR, draft state and board column. The PR counts only
    when the worktree's repo is the board's repo (livestate.on_board). Other rows are left as
    they are. The sources are read only when a window is idle."""
    idle = [w for w in windows if w.cls == 'idle']
    if not idle:
        return windows
    now = time.time() if now is None else now
    table = livestate.procs() if table is None else table
    sess = livestate.sessions() if sess is None else sess
    cards = board_prs() if cards is None else cards
    for w in idle:
        claude = next((pid for pid in livestate.tree(table, w.pid) if livestate.is_claude(table[pid])), None)
        s = sess.get(claude) or {} if claude else {}
        if s.get('status') == 'idle' and s.get('statusUpdatedAt'):
            try:
                w.idle_s = max(0, int(now - float(s['statusUpdatedAt']) / 1000))
            except (TypeError, ValueError):
                w.idle_s = None
        repo, w.branch = git(s.get('cwd') or w.path)
        if w.branch in cards and livestate.on_board(repo):
            w.pr, w.draft, w.card = cards[w.branch]
    return windows


def read_windows(session):
    """Read every window of the tmux session. Raises tmux.TmuxError if it does not exist."""
    panes = tmux.list_panes(session)
    screens = {}
    for p in panes:
        if p.get('dead'):
            continue
        try:
            screens[p['pane_id']] = strip_escapes(tmux.capture_pane(p['pane_id']))
        except tmux.TmuxError:
            screens[p['pane_id']] = ''
    return add_idle_info(window_rows(panes, screens))


def read_zoom(pane, history=ZOOM_HISTORY):
    """The recent output of a pane: its screen plus `history` lines of scrollback."""
    try:
        text = strip_escapes(tmux.capture_pane(pane, history_lines=history))
    except tmux.TmuxError as e:
        return [f'(cannot read {pane}: {e.stderr or "no such pane"})']
    return text.rstrip('\n').split('\n')


# -- view state (no curses) -------------------------------------------------------------------

LABEL = {'working': 'WORKING', 'idle': 'IDLE', 'shell': 'shell', 'program': 'program'}
CLAUDE_CLASSES = ('working', 'idle')
CARD_NAMES = {'Pending': 'Staging'}   # board.STAGING keeps the internal key "Pending"


def ago(s):
    """A short age: 45s, 12m, 3h, 2d ('?' for None)."""
    if s is None:
        return '?'
    s = int(s)
    for unit, n in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def why_idle(w):
    """Why an idle Claude is idle, before what it last said: 'idle 12m, PR #123 Auto Review'."""
    if w.pr:
        card = CARD_NAMES.get(w.card, w.card) or 'no column'
        pr = f"PR #{w.pr}{' draft' if w.draft else ''} {card}"
    elif w.branch:
        pr = f"no open PR on the board for {w.branch}"
    else:
        pr = 'no branch'
    return f"idle {ago(w.idle_s)}, {pr}"


def _cells(ch):
    """Terminal cells one character takes: 0 for a combining mark, 2 for a wide one."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def width_of(text):
    return sum(_cells(ch) for ch in text)


def _fit(text, width):
    """Text clipped to `width` terminal cells (wide characters take 2), with '…' when cut.
    Tabs become spaces and other control characters are dropped, so a line never wraps."""
    text = ''.join(' ' if ch == '\t' else ch for ch in text if ch == '\t' or ch.isprintable())
    if width_of(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        if used + _cells(ch) > width - 1:
            break
        out.append(ch)
        used += _cells(ch)
    return ''.join(out) + ('…' if width > 0 else '')


def _pad(text, width):
    """Text clipped and padded to exactly `width` terminal cells."""
    text = _fit(text, width)
    return text + ' ' * (width - width_of(text))


@dataclass
class View:
    windows: list = field(default_factory=list)
    show_all: bool = False
    selected: int = 0           # a tmux window index, kept across refreshes
    zoomed: object = None       # the Window being zoomed, or None
    zoom_lines: list = field(default_factory=list)
    top: int = 0                # the first table row on screen (the table scrolls)
    session: str = SESSION
    error: str = ''

    def visible(self):
        """The rows of the current view: the windows with a Claude (working or idle), or all
        windows with show_all. A window with only a shell or a program is not an agent."""
        return [w for w in self.windows if self.show_all or w.cls in CLAUDE_CLASSES]

    def _cursor(self):
        rows = self.visible()
        for i, w in enumerate(rows):
            if w.index == self.selected:
                return i
        return 0

    def current(self):
        rows = self.visible()
        return rows[self._cursor()] if rows else None

    def set_windows(self, windows):
        self.windows = windows
        if self.zoomed is not None:
            self.zoomed = next((w for w in windows if w.index == self.zoomed.index), self.zoomed)
        cur = self.current()
        if cur is not None:
            self.selected = cur.index

    def key(self, k):
        """Apply one key. Returns 'quit', 'zoom' (the caller loads zoom_lines), or None."""
        if self.zoomed is not None:
            if k in ('q', 'esc', 'z', 'enter', 'left', 'h'):
                self.zoomed, self.zoom_lines = None, []
            return None
        rows = self.visible()
        if k in ('q', 'esc'):
            return 'quit'
        if k == 'a':
            cur = self.current()
            self.show_all = not self.show_all
            if cur is not None and cur in self.visible():
                self.selected = cur.index
            elif self.visible():
                self.selected = self.visible()[0].index
        elif k in ('down', 'j') and rows:
            self.selected = rows[min(len(rows) - 1, self._cursor() + 1)].index
        elif k in ('up', 'k') and rows:
            self.selected = rows[max(0, self._cursor() - 1)].index
        elif k in ('enter', 'z', 'right', 'l') and rows:
            self.zoomed = rows[self._cursor()]
            return 'zoom'
        return None

    def counts(self):
        return {c: sum(1 for w in self.windows if w.cls == c) for c in CLASSES}

    def header(self):
        n = self.counts()
        mode = 'all windows' if self.show_all else 'Claude windows'
        return (f"swarm tmux: {self.session} - {n['working']} working, {n['idle']} idle, "
                f"{n['shell']} shell, {n['program']} program - {mode}")

    def table(self, width):
        """The table lines of the current view (no header or key help)."""
        rows = self.visible()
        if not rows:
            return ['(no Claude windows; press a to show all)' if not self.show_all
                    else '(no windows)']
        name_w = min(36, max(width_of(w.name) for w in rows))
        out = []
        for w in rows:
            label = LABEL[w.cls] if w.cls != 'program' else w.command
            head = f"{_pad(label, 8)} {w.index:>3} {_pad(w.ticket, 10)} {_pad(w.name, name_w)}  "
            text = w.activity
            if w.cls == 'idle':
                text = f"{why_idle(w)} - said: {w.activity}"
            out.append(_fit(head + text, width))
        return out

    def render(self, width, height, keys=True):
        """The screen as (line, selected?) pairs. keys=False leaves out the key help line."""
        if self.zoomed is not None:
            w = self.zoomed
            top = [(_fit(f"zoom: {self.session}:{w.index} {w.name} ({LABEL[w.cls]}, {w.pane})"
                         " - Esc back", width), False)]
            body = self.zoom_lines[-(height - 1):] if height > 1 else []
            return top + [(_fit(line, width), False) for line in body]
        lines = [(_fit(self.header(), width), False)]
        if self.error:
            lines.append((_fit(self.error, width), False))
        table = self.table(width)
        if not keys:
            return lines + [(line, False) for line in table]
        room = max(1, height - len(lines) - 1)
        cursor = self._cursor() if self.visible() else -1
        if cursor >= 0:
            if cursor < self.top:
                self.top = cursor
            elif cursor >= self.top + room:
                self.top = cursor - room + 1
        self.top = max(0, min(self.top, len(table) - room))
        for i in range(self.top, min(len(table), self.top + room)):
            lines.append((table[i], i == cursor))
        help_line = 'a all/claude  up/down select  enter zoom  q quit'
        if len(table) > room:
            help_line += f'  [{self.top + 1}-{min(len(table), self.top + room)} of {len(table)}]'
        lines = lines[:max(0, height - 1)]
        lines.append((_fit(help_line, width), False))
        return lines


# -- terminal ---------------------------------------------------------------------------------

CURSES_KEYS = {10: 'enter', 13: 'enter', 27: 'esc'}


def _curses_key(ch):
    import curses
    names = {curses.KEY_UP: 'up', curses.KEY_DOWN: 'down', curses.KEY_LEFT: 'left',
             curses.KEY_RIGHT: 'right', curses.KEY_ENTER: 'enter'}
    if ch in names:
        return names[ch]
    if ch in CURSES_KEYS:
        return CURSES_KEYS[ch]
    return chr(ch) if 0 <= ch < 256 else None


def load(view, reader):
    try:
        view.set_windows(reader(view.session))
        view.error = ''
    except tmux.TmuxError as e:
        view.set_windows([])
        view.error = f'cannot read tmux session {view.session!r}: {e.stderr or e}'


def run_curses(view, interval, reader=read_windows, zoom_reader=read_zoom):
    import curses

    def loop(scr):
        curses.curs_set(0)
        scr.timeout(int(interval * 1000))
        load(view, reader)
        while True:
            if view.zoomed is not None:
                view.zoom_lines = zoom_reader(view.zoomed.pane)
            h, w = scr.getmaxyx()
            scr.erase()
            for y, (line, sel) in enumerate(view.render(w - 1, h)):
                try:
                    scr.addstr(y, 0, line, curses.A_REVERSE if sel else curses.A_NORMAL)
                except curses.error:   # a terminal that measures a character wider than we do
                    pass
            scr.refresh()
            ch = scr.getch()
            if ch == -1:
                load(view, reader)
                continue
            k = _curses_key(ch)
            if k == 'r':
                load(view, reader)
            elif view.key(k) == 'quit':
                return

    os.environ.setdefault('ESCDELAY', '25')
    curses.wrapper(loop)


def main(argv=None, reader=read_windows, zoom_reader=read_zoom, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog='swarm-tmux-status', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--session', default=SESSION, help='tmux session of the swarm (default %(default)s)')
    ap.add_argument('--all', action='store_true', help='start with all windows shown, shells and programs too')
    ap.add_argument('--once', action='store_true', help='print the table once and exit')
    ap.add_argument('--zoom', metavar='WINDOW', type=int,
                    help='print the recent output of one window (by index) and exit')
    ap.add_argument('--interval', type=float, default=2.0, help='refresh seconds (default %(default)s)')
    args = ap.parse_args(argv)
    view = View(show_all=args.all, session=args.session)
    if args.once or args.zoom is not None or not out.isatty():
        load(view, reader)
        if view.error:
            print(view.error, file=sys.stderr)
            return 2
        if args.zoom is not None:
            w = next((w for w in view.windows if w.index == args.zoom), None)
            if w is None:
                print(f'no window {args.zoom} in {args.session}', file=sys.stderr)
                return 2
            view.zoomed, view.zoom_lines = w, zoom_reader(w.pane)
            height = len(view.zoom_lines) + 1
        else:
            height = 10 ** 6
        width = os.get_terminal_size(out.fileno()).columns if out.isatty() else 200
        for line, _ in view.render(width, height, keys=False):
            print(line, file=out)
        return 0
    run_curses(view, args.interval, reader, zoom_reader)
    return 0


if __name__ == '__main__':
    sys.exit(main())
