"""Talk to coding agents (Claude Code, Codex, Gemini and others) that run in tmux.

This is a port of scialect's src/agents/{tui-agent,claude-cli,codex-cli,gemini-cli}.mts,
plus the agent detection from tell-worker.mts and local-status.mts. It also has the
send sequence that every tell-worker handoff uses:

1. Reach an empty prompt with the space probe.
2. Optionally send /new and submit it (see 3), then wait 10s.
3. Type the text literally, wait until it shows in the input box, press Enter, and
   check that the text left the box. Enter is pressed again with backoff while the
   text is still sitting there; SubmitError if it never goes.

The prompt-detection functions take screen text and return a bool, so the tests
can run them without tmux.
"""
import hashlib
import os
import re
import subprocess
import time

from . import registry, tmux
from .workers import load_known_agents

PROBE_SETTLE = 0.8       # after the space probe
PROBE_RECHECK = 0.3
PROBE_CLEANUP = 0.1      # after BSpace
ENTER_DELAY = 0.5        # least wait between the text and Enter, because TUIs drop an Enter sent too soon
TYPED_POLL = 0.25        # while waiting for typed text to show in the input box and settle
TYPED_TIMEOUT = 8.0      # give up waiting for it after this long (a loaded box can be slow to draw)
SUBMIT_BACKOFF = (0.5, 1.0, 2.0, 4.0)   # waits before each check that the text left the box
NEW_CONVERSATION_WAIT = 10.0   # after "/new" is submitted
STARTUP_WAIT = 5.0       # how long start_agent watches a new pane for a first-run screen
STARTUP_POLL = 0.5


# ---------------------------------------------------------------------------
# prompt-blank detection (pure functions over capture-pane text)

def _bar_lines(lines):
    """Indices of lines that are mostly a long horizontal bar (─)."""
    return [i for i, line in enumerate(lines)
            if '─' in line and len(line.replace('─', '').strip()) < 15]


def _claude_input_region(lines):
    """Return (first, end) line indices of Claude Code's input box: from the ❯ line down to
    the lowest ─ bar, so multiline input's continuation lines count. Without two bars, only
    the last ❯ line counts. None if there is no ❯ line."""
    bars = _bar_lines(lines)
    if len(bars) < 2:
        for i in range(len(lines) - 1, -1, -1):
            if '❯' in lines[i]:
                return i, i + 1
        return None
    for i in range(bars[-2] + 1, bars[-1]):
        if '❯' in lines[i]:
            return i, bars[-1]
    return None


def claude_prompt_blank(screen):
    """Return True if Claude Code's input is blank. The input box sits between the two
    lowest ─ bars and starts with ❯. It is blank when nothing follows the ❯, on the ❯ line
    or on any continuation line below it."""
    lines = screen.split('\n')
    region = _claude_input_region(lines)
    if region is None:
        return False
    first, end = region
    body = [lines[first][lines[first].find('❯') + 1:]] + lines[first + 1:end]
    return all(line.strip() == '' for line in body)


def codex_prompt_blank(screen):
    """Return True if Codex's prompt is blank. The last line that contains › is the live prompt."""
    for line in reversed(screen.split('\n')):
        if '›' in line:
            return line[line.find('›') + 1:].strip() == ''
    return False


def gemini_prompt_blank(screen):
    """Return True if Gemini's prompt is blank. The layout matches Claude Code, but the
    prompt symbol is '>', and the lowest '>' between the two bottom bars counts."""
    lines = screen.split('\n')
    bars = _bar_lines(lines)
    if len(bars) < 2:
        for line in reversed(lines):
            pos = line.find('>')
            if pos != -1:
                return line[pos + 1:].strip() == ''
        return False
    prompt_line = ''
    for line in lines[bars[-2] + 1:bars[-1]]:
        if '>' in line:
            prompt_line = line
    if prompt_line:
        return prompt_line[prompt_line.find('>') + 1:].strip() == ''
    return False


def muse_prompt_blank(screen):
    """Return True if Muse Code's input line is blank. The layout matches Claude Code, with
    a ❯ input line between the two lowest ─ bars and then a footer
    ('<model> · <effort> · <path> · Auto-review'). Muse draws a grey placeholder
    ('Ask to monitor ...') after the ❯, so pass in the input_view() of an escape-coded
    capture. On plain text the placeholder reads as typed input, and the space probe
    then sees past it."""
    return claude_prompt_blank(screen)


def grok_prompt_blank(screen):
    """Return True if Grok's input box is blank. Grok draws the ❯ input line inside a
    rounded box (│ ❯ ... │), so this goes by input_box()."""
    box = input_box(screen, '❯', framed=True)
    return box is not None and box.strip() == ''


def is_muse_screen(screen):
    """Match Muse's footer line, '<model> · <effort> · <path> · <approval mode>'."""
    lines = [l for l in screen.split('\n') if l.strip()]
    return any(re.match(r'^\s*muse-[\w.-]+ · ', l) for l in lines[-4:])


PROMPT_DETECTORS = {
    'claude': claude_prompt_blank,
    'codex': codex_prompt_blank,
    'gemini': gemini_prompt_blank,
    'muse': muse_prompt_blank,
    'grok': grok_prompt_blank,
}


_BAR_RUN = '─' * 10
_BORDER = '│┃'


def _indent(line):
    """The column of the line's first character that is not a blank or a │ ┃ border."""
    return len(line) - len(line.lstrip(' ' + _BORDER))


def _is_frame_bar(line, col):
    """True if the line is a ─ bar that is not indented past column col. Typed text is
    drawn indented under the glyph, so a bar in it (a quoted screen) sits further right."""
    return _BAR_RUN in line and _indent(line) <= col


def input_box(screen, glyph, framed=False):
    """Return what is typed in the input box, or None if no input box is on screen.

    The input box starts at a line whose first character (after spaces and a │ border)
    is the prompt glyph. Box borders are dropped.

    With framed=True (Claude, Muse, Grok, Gemini) a ─ bar must sit right above the glyph
    line and another must close the box, so a transcript line or a dialog's menu
    ('❯ No, exit') never passes for the box. Only a bar that is not indented past the
    glyph counts as the frame: typed lines are drawn indented under the glyph, so a
    quoted screen in the text (a bar, '❯ old request', a bar) is box content. A glyph
    line that sits inside another candidate box is such a quote, so the lowest glyph line
    that is not inside another box is the input box.

    Codex's box has no frame: its glyph sits in column 0 and its continuation lines are
    indented, so a typed line that starts with the glyph is not taken for the prompt. The
    box ends at the first blank line, above the footer. Codex's dialogs' menus
    ('› 3. Cancel') also start in column 0, so callers compare the box with what they
    typed rather than only asking if it is blank."""
    lines = screen.split('\n')
    boxes = []      # (start, end): the glyph line and the line that ends the box
    for start, line in enumerate(lines):
        if not line.lstrip(' ' + _BORDER).startswith(glyph):
            continue
        if not framed:
            if not line.startswith(glyph):
                continue    # Codex indents continuation lines, so a '› ...' in the text is not the prompt
            end = next((j for j in range(start + 1, len(lines))
                        if not lines[j].strip(' ' + _BORDER)), len(lines))
            boxes.append((start, end))
            continue
        col = _indent(line)
        if start == 0 or not _is_frame_bar(lines[start - 1], col):
            continue
        end = next((j for j in range(start + 1, len(lines)) if _is_frame_bar(lines[j], col)), None)
        if end is not None:
            boxes.append((start, end))
    for start, end in reversed(boxes):
        if framed and any(s < start and e >= end for s, e in boxes):
            continue        # a quoted prompt inside the real box
        out = [lines[start].lstrip(' ' + _BORDER)[len(glyph):]] + lines[start + 1:end]
        return '\n'.join(l.strip(' ' + _BORDER) for l in out)
    return None


# Long pastes collapse to a placeholder: Claude and Muse '[Pasted text #1 +40 lines]',
# Codex '[Pasted Content 2048 chars]'.
_PASTE_RE = re.compile(r'\[Pasted (text|content)\b', re.I)
_LEADING_BLANK_RE = re.compile(r'\A(?:[ \t]*\n)+')
_FLAT_RE = re.compile(r'[\s│┃]+')
FINGERPRINT_CHARS = 24


def _flat(s):
    """The text with all whitespace and box side borders (│ ┃) removed. Typed text and the
    box are both compared in this form: a box wraps, indents and frames lines, and the
    message itself can hold a │ that input_box() cannot tell from a border."""
    return _FLAT_RE.sub('', s)


def fingerprint(text):
    """The first FINGERPRINT_CHARS characters of _flat() of the text's first line that is
    not blank in that form. Codex's box ends at a blank line, so stay on the first line."""
    first = next((l for l in text.split('\n') if _flat(l)), '')
    return _flat(first)[:FINGERPRINT_CHARS]


def holds_text(box, text):
    """Return True if the input box (from input_box()) shows `text`, or a paste placeholder."""
    if box is None:
        return False
    fp = fingerprint(text)
    return bool(fp and fp in _flat(box)) or bool(_PASTE_RE.search(box))


def detector_for(agent):
    """Return the prompt detector for a detected agent name. Codex matches by
    substring, as in tell-worker.mts."""
    a = (agent or '').lower()
    if 'codex' in a:
        return codex_prompt_blank
    return PROMPT_DETECTORS.get(a)


# ---------------------------------------------------------------------------
# first-run and other blocking screens (pure functions over capture-pane text)
#
# Each entry is (kind, agents it applies to or None for any, phrases that must all
# appear, case-insensitively, in the bottom BLOCKING_WINDOW non-empty lines). The
# Claude Code 2.1.302, Codex 0.157.0 and Grok 1.0.62 phrases come from the screens captured in
# tests/fixtures; the generic trust question also covers older versions and Gemini.

BLOCKING_WINDOW = 30
BLOCKING_SCREENS = [
    ('trust_prompt', {'claude'}, ('Yes, I trust this folder', 'No, exit')),
    ('trust_prompt', {'codex'}, ('Trust this folder?', 'Trust and continue')),
    ('trust_prompt', None, ('Do you trust the files in this folder?',)),
    ('trust_prompt', None, ('Do you trust this folder?',)),
    ('theme_picker', {'claude'}, ('Choose the text style that looks best with your terminal',)),
    ('login', {'claude'}, ('Select login method',)),
    ('login', {'claude'}, ('Paste code here if prompted',)),
    ('login', {'codex'}, ('Sign in with ChatGPT', 'Provide your own API key')),
    ('api_key_prompt', {'claude'}, ('Detected a custom API key', 'Do you want to use this API key?')),
    ('bypass_permissions_confirm', {'claude'}, ('Bypass Permissions mode', 'Yes, I accept')),
    ('update_prompt', {'codex'}, ('Update available', 'Update now')),
    ('trust_prompt', {'grok'}, ('Do you trust the contents of this directory?', 'Yes, proceed')),
    ('login', {'grok'}, ('Approve in your browser to finish signing in',)),
]

BLOCKING_HINTS = {
    'trust_prompt': "the folder is not trusted; start_agent pre-trusts its worktree, so check "
                    "the start_agent 'trust' result for a warning",
    'theme_picker': "the agent has never been set up as this user; finish its first-run "
                    "setup once by hand",
    'login': "the agent is not logged in as this user; log in once by hand",
    'api_key_prompt': "the agent is asking whether to use an API key from the environment; "
                      "answer it once by hand",
    'bypass_permissions_confirm': "the agent wants the bypass-permissions mode confirmed by hand",
    'update_prompt': "the agent is offering an update; answer it once by hand or update it",
}


def _has_input_box(screen, glyph):
    """True if a prompt line (starting with glyph) sits between the two lowest ─ bars."""
    lines = screen.split('\n')
    bars = _bar_lines(lines)
    return len(bars) >= 2 and any(l.lstrip().startswith(glyph) for l in lines[bars[-2] + 1:bars[-1]])


def _codex_input_live(screen):
    """True if Codex's live › prompt is up: the last › line is not a numbered option of
    one of its dialogs ("› 1. Trust and continue") and no option follows it."""
    lines = screen.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        if '›' in lines[i]:
            menu = re.compile(r'^\s*›?\s*\d+\.\s')
            return not any(menu.match(l) for l in lines[i:])
    return False


def _grok_input_live(screen):
    """True if Grok's input box ('│ ❯ ...│', between ╭ and ╰ borders) is near the bottom."""
    lines = [l for l in screen.split('\n') if l.strip()][-6:]
    return any(re.match(r'^\s*│\s*❯', l) for l in lines) and any(l.lstrip().startswith('╰') for l in lines)


def blocking_screen(screen, agent=None):
    """Return {'kind', 'agent', 'lines', 'hint'} if the screen shows a first-run or other
    blocking screen (trust, theme, login, API key, bypass confirmation, update) that
    typing a message into would answer, else None. `screen` is plain text."""
    a = (agent or '').lower()
    a = 'codex' if 'codex' in a else a
    if a in ('claude', 'muse', 'gemini') and _has_input_box(screen, '>' if a == 'gemini' else '❯'):
        return None     # the normal input box is up, so any matching text is in the transcript
    if a == 'codex' and _codex_input_live(screen) or a == 'grok' and _grok_input_live(screen):
        return None
    lines = [l.rstrip() for l in screen.split('\n') if l.strip()][-BLOCKING_WINDOW:]
    hay = '\n'.join(lines).lower()
    for kind, only, phrases in BLOCKING_SCREENS:
        if only is not None and a and a not in only:
            continue
        if not all(p.lower() in hay for p in phrases):
            continue
        if (only is None or not a) and not _live_dialog(lines, phrases):
            continue        # quoted in a transcript (no input-box check applies), not a live dialog
        return {'kind': kind, 'agent': agent, 'lines': lines[-12:], 'hint': BLOCKING_HINTS[kind]}
    return None


GENERIC_DIALOG_DEPTH = 12    # a live dialog's text and choices sit this close to the bottom
_CHOICE_RE = re.compile(r'^\s*(?:[›>❯●]?\s*\d+\.\s|[›>❯●]\s*\S)')   # "2. No" or "❯ No, exit"


def _live_dialog(lines, phrases):
    """True if every phrase is in the bottom GENERIC_DIALOG_DEPTH lines and a choice line
    (numbered, or behind a selection cursor) is there too. Generic phrases, and every
    phrase when the agent is unknown (no input-box check applies), only match this way,
    so text merely quoted in a transcript does not."""
    bottom = lines[-GENERIC_DIALOG_DEPTH:]
    hay = '\n'.join(bottom).lower()
    return all(p.lower() in hay for p in phrases) and any(_CHOICE_RE.match(l) for l in bottom)


# ---------------------------------------------------------------------------
# escape-coded captures, used to tell placeholder and suggestion text from real input

_ESC_RE = re.compile(r'\x1b(?:\[([0-9;:?]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|.)', re.S)
# Grey foregrounds TUIs use for placeholders and suggestions (plus SGR 2, dim).
_GREY_256 = set(range(239, 247)) | {8}
_KEEP = set('❯›') | {chr(c) for c in range(0x2500, 0x2580)}   # prompt glyphs and box-drawing characters
_KEEP_AT_LINE_START = set('>')   # Gemini's prompt; elsewhere it is placeholder text ("<filepath>")


def strip_ansi(text):
    return _ESC_RE.sub('', text)


def input_view(text):
    """Return the plain text of an escape-coded capture (capture-pane -e), with every
    character drawn dim or in grey blanked out, except prompt glyphs and box drawing.
    Claude Code (prompt suggestions), Muse (placeholder) and Codex draw hints that way,
    and draw typed input in the normal colour. Use this for prompt detection only: it
    also blanks unrelated dim output elsewhere on the screen."""
    return ''.join(_mask(chunk, mask) for chunk, _, mask in _blanked(text))


def _segments(text):
    """Split an escape-coded capture into (plain chunk, drawn as a hint?) pairs."""
    dim = False
    fg = None
    pos = 0
    for m in _ESC_RE.finditer(text):
        chunk = text[pos:m.start()]
        pos = m.end()
        if chunk:
            yield chunk, _is_hint(dim, fg)
        if m.group(2) != 'm':
            continue
        params = [p for p in re.split('[;:]', m.group(1) or '0')]
        i = 0
        while i < len(params):
            p = params[i] or '0'
            if p == '0':
                dim, fg = False, None
            elif p == '2':
                dim = True
            elif p == '22':
                dim = False
            elif p == '39':
                fg = None
            elif p in ('38', '48') and i + 1 < len(params):
                if params[i + 1] == '5' and i + 2 < len(params):
                    if p == '38':
                        fg = ('256', params[i + 2])
                    i += 2
                elif params[i + 1] == '2':
                    if p == '38':
                        fg = ('rgb', tuple(params[i + 2:i + 5]))
                    i += 4
            elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
                fg = ('16', p)
            i += 1
    if pos < len(text):
        yield text[pos:], _is_hint(dim, fg)


def _is_hint(dim, fg):
    if dim:
        return True
    if fg is None:
        return False
    kind, v = fg
    if kind == '256':
        return v.isdigit() and int(v) in _GREY_256
    if kind == '16':
        return v == '90'
    return False


def _blanked(text):
    """Yield (plain chunk, drawn as a hint?, blank mask) for an escape-coded capture. The
    mask has one flag per character of the chunk: True where input_view() blanks it. A
    hint character is blanked unless it is a prompt glyph, box drawing, a line break, or a
    '>' at the start of a line (Gemini's prompt; elsewhere, as in Claude's 'Try "refactor
    <filepath>"', it is placeholder text). A line starts after a line break; it stays at
    its start until a character that is shown (not blanked) and not a blank, or a
    non-blank character inside the same hint run."""
    line_start = True
    for chunk, hint in _segments(text):
        mask = []
        run_start = line_start          # inside a hint run, any non-blank ends the line start
        for c in chunk:
            if c == '\n':
                mask.append(False)
                line_start = run_start = True
                continue
            blank = hint and not (c in _KEEP or (run_start and c in _KEEP_AT_LINE_START))
            mask.append(blank)
            if not c.isspace():
                run_start = False
                if not blank:
                    line_start = False
        yield chunk, hint, mask


def _mask(chunk, mask):
    return ''.join(' ' if b else c for c, b in zip(chunk, mask))


# ---------------------------------------------------------------------------
# the prompt as typed versus the screen as drawn

SUGGESTION_LABEL = '[ghost suggestion, not typed: {}]'
SCREEN_NOTE = ('last_lines is the screen as drawn, for diagnostics; it is not evidence of typed '
               'input. A grey/dim suggestion in the prompt box is shown as ' +
               SUGGESTION_LABEL.format('...') + '. Read prompt.input / prompt_blank for '
               'what is typed, and use pane_ready or tell_agent (which refuses to overwrite '
               'real input) before typing.')


def claude_prompt(raw):
    """Read Claude Code's (or Muse's) input box from an escape-coded capture.

    Returns {'input', 'suggestion', 'blank', 'lines'} or None if there is no ❯ box.

    'input' is the text on the input lines with Claude's own padding removed and nothing
    else: one separator character after the ❯, and on continuation lines the indent that
    lines them up under it (pos(❯) + 2 columns). Typed indentation, inner and trailing
    spaces, and the line structure (one line per box line, blank ones included) are kept.
    tmux trims blanks at the end of a captured line, so trailing spaces typed at a line
    end may already be gone; 'input' is what the screen shows, not a keystroke log.

    'suggestion' is the positively identified ghost text, or None: on each input line,
    the trailing run (blanks aside) of characters that input_view() would blank, i.e.
    drawn dim or grey and not a prompt glyph or box-drawing character. Blanks in the
    normal style just before the run stay in 'input'; dim/grey blanks belong to the run. If that run
    touches a dim/grey glyph that input_view() keeps (say a dim 'a > b'), it cannot be
    told apart from input, so nothing on that line is called a suggestion. Everything that
    is not a suggestion, including unfamiliar styles and hint-styled text with typed text
    after it, stays in 'input'. So 'blank' (input has only blanks) always agrees with
    claude_prompt_blank(input_view(raw)), and errs toward not blank.

    'lines' is the plain screen with each suggestion wrapped in SUGGESTION_LABEL; nothing
    outside the input box is changed."""
    plain, styled, masked = [], [], []
    for chunk, hint, mask in _blanked(raw):
        plain.append(chunk)
        styled.extend([hint] * len(chunk))
        masked.extend(mask)                  # what input_view() blanks
    plain = ''.join(plain)
    lines, rows, at = [], [], 0
    for line in plain.split('\n'):
        lines.append(line)
        rows.append((styled[at:at + len(line)], masked[at:at + len(line)]))
        at += len(line) + 1
    region = _claude_input_region(lines)
    if region is None:
        return None
    first, end = region
    glyph = lines[first].find('❯')
    typed, ghosts = [], []
    for i in range(first, end):
        line, (style, mask) = lines[i], rows[i]
        if i == first:                       # one separator after the ❯
            start = glyph + 1
            if start < len(line) and line[start].isspace():
                start += 1
        else:                                # continuation indent under the ❯ text
            start = 0
            while start < min(len(line), glyph + 2) and line[start].isspace():
                start += 1
        stop = len(line.rstrip())
        cut = stop
        while cut > start and (mask[cut - 1] or line[cut - 1].isspace()):
            cut -= 1
        # stopped on a dim/grey glyph that input_view() keeps: can't tell, so no suggestion
        ambiguous = cut > start and style[cut - 1]
        while cut < stop and line[cut].isspace() and not style[cut]:   # plain blanks stay typed
            cut += 1
        ghost = line[cut:stop].lstrip()
        if ghost and not ambiguous:
            ghosts.append(ghost)
            lead = stop - cut - len(ghost)
            lines[i] = line[:cut + lead] + SUGGESTION_LABEL.format(ghost) + line[stop:]
            typed.append(line[start:cut])
        else:
            typed.append(line[start:])
    text = '\n'.join(typed)
    return {'input': text, 'suggestion': ' '.join(ghosts) or None,
            'blank': text.strip() == '', 'lines': lines}


def screen_view(raw, agent):
    """Return (plain screen lines, prompt interpretation or None) for an agent's capture.
    Only agents with a ❯ input box (claude, muse) get an interpretation; the lines of
    other agents are the plain screen, unchanged."""
    if (agent or '').lower() in ('claude', 'muse'):
        prompt = claude_prompt(raw)
        if prompt is not None:
            return prompt.pop('lines'), prompt
    return strip_ansi(raw).split('\n'), None


def screen_hash(screen):
    return hashlib.sha256(screen.encode('utf-8', errors='replace')).hexdigest()


# ---------------------------------------------------------------------------
# TUI agent wrapper (TuiAgent / ClaudeTui / CodexTui / GeminiTui / MuseTui / GrokTui)

class TuiAgent:
    """A TUI agent in a tmux pane. Subclasses set `detect`, `glyph` (the prompt character
    that starts the input box) and `framed` (see input_box()). Without a glyph, send_text
    cannot see the box, so it presses Enter once and cannot verify the submission."""

    name = 'tui'
    detect = staticmethod(lambda screen: False)
    glyph = None
    framed = True       # see input_box()

    def __init__(self, target, capture=None, send_literal=None, send_key=None, sleep=time.sleep):
        self.target = target
        # pass the escape-coded capture through input_view() so placeholders read as blank
        self._capture = capture or (lambda t: input_view(tmux.capture_pane(t, escapes=True)))
        self._send_literal = send_literal or (lambda t, s: tmux.send_keys_literal(t, s, enter=False))
        self._send_key = send_key or (lambda t, k: tmux.send_keys(t, k, enter=False))
        self._sleep = sleep

    def screen(self):
        return self._capture(self.target)

    def is_prompt_blank(self):
        return self.detect(self.screen())

    def ensure_prompt_is_empty(self):
        """Check for an empty prompt without losing input (tui-agent.mts#ensurePromptIsEmpty).

        Return True if the prompt is blank. Otherwise type a space. If the prompt
        then reads as blank, the old text was a placeholder or suggestion, so return
        True. If not, someone has typed real input, so return False. The probe always
        deletes its space with a backspace.
        """
        if self.is_prompt_blank():
            return True
        self._send_literal(self.target, ' ')
        self._sleep(PROBE_SETTLE)
        now_blank = self.is_prompt_blank()
        if not now_blank:
            self._sleep(PROBE_RECHECK)
            now_blank = self.is_prompt_blank()
        self._send_key(self.target, 'BSpace')
        self._sleep(PROBE_CLEANUP)
        return now_blank

    def send_text(self, text, enter=True):
        """Type text literally and, with enter, submit it and check that it was submitted.

        1. Wait at least ENTER_DELAY, and until the text shows in the input box and the
           box stops changing (up to TYPED_TIMEOUT). If it never shows, a dialog or menu
           probably took the keystrokes: raise SubmitError without pressing Enter.
        2. Press Enter, then check the box after each SUBMIT_BACKOFF wait. A blank box
           means the text was submitted. If the text is still in the box, the Enter was
           lost (TUIs drop them under load): press Enter again. Anything else (no box, or
           a dialog's menu) never gets an Enter, since it could pick a menu item.
        3. Raise SubmitError if the text is still in the box at the end, or if the box
           never came back blank.

        Returns {'enter_presses': n, 'submit_verified': bool}.
        """
        text = text.replace('\r\n', '\n').replace('\r', '\n')   # a CR is Enter: it would submit part-way
        if fingerprint(text):
            # Leading blank lines carry nothing, and Codex's box ends at the first blank
            # line, so the text after them would never show in the box.
            text = _LEADING_BLANK_RE.sub('', text)
        self._send_literal(self.target, text)
        if not enter:
            return {'enter_presses': 0, 'submit_verified': False}
        if self.glyph is None or not fingerprint(text):
            self._sleep(ENTER_DELAY)
            self._send_key(self.target, 'Enter')
            return {'enter_presses': 1, 'submit_verified': False}
        typed = self._wait_until_typed(text)
        self._send_key(self.target, 'Enter')
        presses, state, screen = 1, None, ''
        for i, wait in enumerate(SUBMIT_BACKOFF):
            self._sleep(wait)
            screen = self.screen()
            state = self._submit_state(screen, text, typed)
            if state == 'sent':
                return {'enter_presses': presses, 'submit_verified': True}
            if state == 'pending' and i < len(SUBMIT_BACKOFF) - 1:
                self._send_key(self.target, 'Enter')
                presses += 1
        waited = sum(SUBMIT_BACKOFF)
        if state == 'pending':
            raise SubmitError('pending', screen,
                              f"{self.target}: the text is still in {self.name}'s input box after "
                              f"{presses} Enter presses over {waited:g}s; it was not submitted")
        raise SubmitError('unconfirmed', screen,
                          f"{self.target}: pressed Enter {presses} time(s) but {self.name}'s input box "
                          f"never came back blank within {waited:g}s (a dialog or menu may be open), so "
                          "Enter was not pressed again; the text may or may not have been submitted")

    def _submit_state(self, screen, text, typed=None):
        """'pending' if the text is still in the input box, 'sent' if the box is blank,
        else 'unknown' (no box, or something else in it, such as a dialog's menu).

        typed is the box as it was just before Enter. A lost Enter leaves the box as it
        was, so with typed given the box is 'pending' only if it still reads the same.
        A box that changed, such as a Codex menu whose selected option ('› 3. Skip until
        next version') holds the text, is 'unknown' and never gets another Enter."""
        box = input_box(screen, self.glyph, self.framed)
        if box is None:
            return 'unknown'
        if holds_text(box, text):
            return 'pending' if typed is None or _flat(box) == _flat(typed) else 'unknown'
        return 'sent' if box.strip() == '' else 'unknown'

    def _wait_until_typed(self, text):
        """Wait at least ENTER_DELAY, then until the input box shows the text and stays the
        same for one TYPED_POLL. Only the box is compared, so a spinner elsewhere on a busy
        agent's screen does not hold this up. Returns the box as it was before Enter."""
        self._sleep(ENTER_DELAY)
        waited, last, screen = ENTER_DELAY, None, ''
        while True:
            screen = self.screen()
            box = input_box(screen, self.glyph, self.framed)
            if holds_text(box, text):
                if box == last:
                    return box
                last = box
            else:
                last = None
            if waited >= TYPED_TIMEOUT:
                break
            self._sleep(TYPED_POLL)
            waited += TYPED_POLL
        if last is not None:        # in the box, but still being redrawn: go ahead
            return last
        raise SubmitError('not_typed', screen,
                          f"{self.target}: the typed text never showed up in {self.name}'s input box "
                          f"within {TYPED_TIMEOUT:g}s (a dialog or menu may have taken the keystrokes), "
                          "so Enter was not pressed")

    def new_conversation(self):
        """Send '/new' and submit it as send_text does, then wait 10s for the fresh session."""
        self.send_text('/new')
        self._sleep(NEW_CONVERSATION_WAIT)


class ClaudeTui(TuiAgent):
    name = 'claude'
    detect = staticmethod(claude_prompt_blank)
    glyph = '❯'


class CodexTui(TuiAgent):
    name = 'codex'
    detect = staticmethod(codex_prompt_blank)
    glyph = '›'
    framed = False


class GeminiTui(TuiAgent):
    name = 'gemini'
    detect = staticmethod(gemini_prompt_blank)
    glyph = '>'


class MuseTui(TuiAgent):
    """Muse Code. Enter submits, and /new or /clear starts a fresh session."""
    name = 'muse'
    detect = staticmethod(muse_prompt_blank)
    glyph = '❯'


class GrokTui(TuiAgent):
    """Grok. The ❯ input line sits inside a rounded box; Enter submits."""
    name = 'grok'
    detect = staticmethod(grok_prompt_blank)
    glyph = '❯'


def tui_for(agent, target, **kw):
    """Return the TUI wrapper for a detected agent name, or None if it is unsupported."""
    a = (agent or '').lower()
    if 'codex' in a:
        return CodexTui(target, **kw)
    if a == 'claude':
        return ClaudeTui(target, **kw)
    if a == 'gemini':
        return GeminiTui(target, **kw)
    if a == 'muse':
        return MuseTui(target, **kw)
    if a == 'grok':
        return GrokTui(target, **kw)
    return None


# ---------------------------------------------------------------------------
# agent detection

def match_agent(rules, command, args, title):
    """Return the first rule whose constraints all hold (local-status.mts#matchAgent)."""
    cmd_base = command.split('/')[-1] if command else command
    for rule in rules:
        m = rule.get('match', {})
        if m.get('command') and cmd_base != m['command']:
            continue
        if m.get('args_contains') and m['args_contains'] not in args:
            continue
        if m.get('title_contains') and m['title_contains'] not in title:
            continue
        return rule.get('name')
    return None


def _run(argv):
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, check=False)
    return r.stdout if r.returncode == 0 else ''


def _pane_rows(window_target, fmt):
    sep = '\t'
    out = _run(['tmux', 'list-panes', '-t', window_target, '-F', sep.join(fmt)])
    return [line.split(sep, len(fmt) - 1) for line in out.splitlines() if line.strip()]


def process_info(pid):
    line = _run(['ps', '-p', str(pid), '-o', 'comm=,args=']).strip()
    if ' ' not in line:
        return line, ''
    command, args = line.split(' ', 1)
    return command, args.strip()


def child_pids(pid):
    return [p for p in _run(['pgrep', '-P', str(pid)]).split() if p]


def detect_agent(target, rules=None, cdir=None):
    """Detect the agent in a window (tell-worker.mts#detectAgent). This applies the
    known-agents rules to every process on the first pane's tty. target may be
    'sess:win' or a pane."""
    rules = rules if rules is not None else load_known_agents(cdir)
    # list-panes on a window or pane target lists that window's panes; the
    # first row is the main pane, which is what scialect inspects.
    rows = _pane_rows(target, ['#{pane_tty}', '#{pane_current_command}', '#{pane_title}'])
    if not rows or len(rows[0]) < 3:
        return None
    tty, command, title = rows[0]
    if not tty or not command:
        return None
    all_procs = _run(['ps', '-t', tty, '-o', 'comm=,args='])
    cmd_base = command.split('/')[-1]
    for rule in rules:
        m = rule.get('match', {})
        if m.get('command') and cmd_base != m['command'] and m['command'] not in all_procs:
            continue
        if m.get('args_contains') and m['args_contains'] not in all_procs:
            continue
        if m.get('title_contains') and m['title_contains'] not in title:
            continue
        return rule.get('name')
    return None


def find_agent_in_window(session, window, rules=None, cdir=None):
    """Check each pane's own process and its children (local-status.mts#findAgentInWindow).
    Returns (agent or None, live cwd or None)."""
    rules = rules if rules is not None else load_known_agents(cdir)
    rows = _pane_rows(f"{session}:{window}",
                      ['#{pane_index}', '#{pane_pid}', '#{pane_current_command}',
                       '#{pane_title}', '#{pane_current_path}'])
    for row in rows:
        if len(row) < 5:
            continue
        _, pid, _, title, path = row
        command, args = process_info(pid)
        agent = match_agent(rules, command, args, title)
        if agent:
            return agent, path
        for child in child_pids(pid):
            command, args = process_info(child)
            agent = match_agent(rules, command, args, title)
            if agent:
                return agent, path
    if rows and len(rows[0]) >= 5:
        return None, rows[0][4]
    return None, None


# ---------------------------------------------------------------------------
# pane-exact detection of registered agents (the typing guard)

class AgentNotReady(RuntimeError):
    pass


class NotAnAgentPane(AgentNotReady):
    """The pane's foreground process is not a registered coding agent."""


class SubmitError(AgentNotReady):
    """Typed text could not be confirmed as submitted. `state` is 'not_typed' (it never
    showed in the input box; Enter was not pressed), 'pending' (still in the box after
    every retry) or 'unconfirmed' (the box never came back blank, e.g. a dialog opened).
    The message ends with the screen's last lines, so the caller sees what happened."""

    def __init__(self, state, screen, message):
        self.state = state
        self.last_lines = [l.rstrip() for l in screen.rstrip('\n').split('\n')][-8:]
        super().__init__(message + '. Last lines on screen:\n' + '\n'.join(self.last_lines))

class AgentBlocked(AgentNotReady):
    """The agent is on a first-run or other blocking screen, which typed text would answer."""

    def __init__(self, target, blocked):
        self.blocked = blocked
        super().__init__(f"{target}: agent is on the {blocked['kind']} screen; nothing sent "
                         f"({blocked['hint']})")


def foreground_processes(tty):
    """Return [(pid, stat, comm, args)] for every process on a tty. comm comes from /proc,
    so spaces in args cannot cut it short."""
    rows = []
    for line in _run(['ps', '-t', tty, '-o', 'pid=,stat=,args=']).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, stat = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ''
        try:
            with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                comm = f.read().strip()
        except OSError:
            comm = os.path.basename(args.split(None, 1)[0]) if args else ''
        rows.append((pid, stat, comm, args))
    return rows


def resolve_pane(target):
    """Return the exact pane a tmux target means, which is the pane send-keys would hit.
    The dict has 'pane_id', 'tty', 'dead', 'session', 'window_index' and 'pane_index'."""
    sep = '\t'
    fmt = sep.join(['#{pane_id}', '#{pane_tty}', '#{pane_dead}', '#{session_name}',
                    '#{window_index}', '#{pane_index}'])
    parts = tmux.display(target, fmt).split(sep)
    if len(parts) < 6 or not parts[0].startswith('%'):
        raise NotAnAgentPane(f"{target}: no such pane")
    return {'pane_id': parts[0], 'tty': parts[1], 'dead': parts[2] not in ('', '0'),
            'session': parts[3], 'window_index': parts[4], 'pane_index': parts[5]}


def pane_agent(target, procs=None):
    """Find the registered agent in the foreground of the exact pane `target` resolves to.
    Returns the resolve_pane() dict plus 'agent' (None if there is none) and 'agent_pid'."""
    try:
        info = resolve_pane(target)
    except tmux.TmuxError as e:
        raise NotAnAgentPane(f"{target}: {e.stderr or 'no such pane'}") from e
    agent, pid = None, None
    if not info['dead'] and info['tty']:
        agent, pid = registry.agent_from_processes(
            procs if procs is not None else foreground_processes(info['tty']))
    return {**info, 'agent': agent, 'agent_pid': pid}


def require_agent_pane(target, procs=None):
    """Like pane_agent(), but raise NotAnAgentPane unless a registered agent is in the
    foreground. Typing tools send to the returned pane_id, not the raw target."""
    info = pane_agent(target, procs=procs)
    if not info['agent']:
        raise NotAnAgentPane(
            f"{target} ({info['pane_id']}) is not running a registered coding agent "
            f"({', '.join(registry.AGENT_NAMES)}) in the foreground; refusing to type into it")
    return info


# ---------------------------------------------------------------------------
# high-level operations (used by the MCP tools and the CLI)


def pane_blocked(pane, agent, capture=None):
    """Return blocking_screen() for a pane's current screen."""
    raw = capture(pane) if capture else tmux.capture_pane(pane, escapes=True)
    return blocking_screen(strip_ansi(raw), agent)


def watch_startup(pane, agent, wait=STARTUP_WAIT, poll=STARTUP_POLL, capture=None,
                  sleep=time.sleep, clock=time.monotonic):
    """Watch a freshly started agent for up to `wait` seconds. Returns {'state', 'blocked',
    'last_lines'}: state is 'blocked' (a first-run screen, named in 'blocked'), 'ready'
    (blank prompt), 'starting' (neither yet) or 'exited' (the pane went away)."""
    capture = capture or (lambda t: tmux.capture_pane(t, escapes=True))
    det = detector_for(agent)
    start = clock()
    screen = ''
    while True:
        try:
            raw = capture(pane)
        except tmux.TmuxError:
            return {'state': 'exited', 'blocked': None, 'last_lines': _tail(screen.split('\n'))}
        screen = strip_ansi(raw)
        blocked = blocking_screen(screen, agent)
        if blocked:
            return {'state': 'blocked', 'blocked': blocked, 'last_lines': _tail(screen.split('\n'))}
        if det and det(input_view(raw)):
            return {'state': 'ready', 'blocked': None, 'last_lines': _tail(screen.split('\n'))}
        if clock() - start >= wait:
            return {'state': 'starting', 'blocked': None, 'last_lines': _tail(screen.split('\n'))}
        sleep(poll)


def agent_status(target, cdir=None):
    """Return the registered agent in the foreground of the exact pane, its prompt state and the pane info."""
    info = pane_agent(target)
    agent = info['agent']
    raw = tmux.capture_pane(info['pane_id'], escapes=True)
    screen = strip_ansi(raw)
    lines, prompt = screen_view(raw, agent)
    det = detector_for(agent)
    blocked = blocking_screen(screen, agent) if agent else None
    panes = tmux.list_panes(info['pane_id'])
    pane = next((p for p in panes if p.get('pane_id') == info['pane_id']), panes[0] if panes else {})
    return {
        'target': target,
        'pane_id': info['pane_id'],
        'agent': agent,
        'typeable': agent is not None,
        'blocked': blocked,
        'prompt_blank': (False if blocked else det(input_view(raw))) if det else None,
        'supported': det is not None,
        'current_command': pane.get('current_command'),
        'current_path': pane.get('current_path'),
        'screen_sha256': screen_hash(screen),
        'prompt': prompt,
        'last_lines': _tail(lines),
        'screen_note': SCREEN_NOTE,
    }


def _tail(lines, n=5):
    while lines and not lines[-1]:
        lines = lines[:-1]
    return lines[-n:]


def pane_ready(target, probe=False, agent=None, cdir=None):
    """Return True if the agent's prompt is empty. With probe=True, use the space probe
    (a space, then a backspace) to see past placeholder text. The probe only types
    into a pane that runs a registered agent."""
    if probe:
        info = require_agent_pane(target)
    else:
        info = pane_agent(target)
    agent = agent or info['agent']
    target = info['pane_id']
    blocked = pane_blocked(target, agent) if agent else None
    if blocked:
        # never probe a dialog: the space could pick one of its options
        return {'target': target, 'agent': agent, 'ready': False, 'probed': False, 'blocked': blocked}
    tui = tui_for(agent, target)
    if tui is None:
        return {'target': target, 'agent': agent, 'ready': None, 'blocked': None,
                'note': f"no prompt detector for agent {agent!r}"}
    ready = tui.ensure_prompt_is_empty() if probe else tui.is_prompt_blank()
    return {'target': target, 'agent': agent, 'ready': bool(ready), 'probed': probe, 'blocked': None}


def tell_agent(target, text, new_conversation=False, require_empty_prompt=True,
               agent=None, cdir=None):
    """Run tell-worker's send sequence on any pane.

    1. Optionally reach an empty prompt with the space probe, and refuse if that fails.
    2. Optionally send '/new', submit it and wait 10s.
    3. Type the text literally and submit it: press Enter once it shows in the input box,
       and again with backoff while it stays there (TuiAgent.send_text). Raises
       SubmitError, which the caller sees, if it was not submitted.
    """
    info = require_agent_pane(target)          # never type into a shell
    agent = (agent or info['agent']).lower()
    pane = info['pane_id']
    blocked = pane_blocked(pane, agent)        # even with require_empty_prompt=false
    if blocked:
        raise AgentBlocked(target, blocked)
    tui = tui_for(agent, pane)
    if tui is None:
        tui = TuiAgent(pane)
        if require_empty_prompt:
            raise AgentNotReady(f"no prompt detector for agent {agent!r}; "
                                "pass require_empty_prompt=false to send anyway")
    elif require_empty_prompt and not tui.ensure_prompt_is_empty():
        raise AgentNotReady(f"{target}: never reached an empty prompt (the user may be typing); nothing sent")
    if new_conversation:
        tui.new_conversation()
    sent = tui.send_text(text)
    return {'target': target, 'pane_id': pane, 'agent': agent, 'sent_chars': len(text),
            'new_conversation': bool(new_conversation), **sent}


def wait_for_idle(target, timeout=120.0, poll=1.0, settle=3.0, agent=None, cdir=None,
                  sleep=time.sleep, capture=None, clock=time.monotonic):
    """Block until the agent looks idle. The screen must stay unchanged for `settle`
    seconds and, when the agent has a detector, its prompt must be blank."""
    if capture is None:
        info = pane_agent(target)
        agent = agent or info['agent']
        pane = info['pane_id']
        capture = lambda: tmux.capture_pane(pane, escapes=True)  # noqa: E731
    det = detector_for(agent)
    start = clock()
    last_hash, stable_since = None, start
    screen = ''
    while True:
        raw = capture()
        screen = input_view(raw)
        h = screen_hash(screen)
        now = clock()
        if h != last_hash:
            last_hash, stable_since = h, now
        blocked = blocking_screen(strip_ansi(raw), agent)   # dialogs may be drawn in grey (Grok)
        if blocked and now - stable_since >= settle:
            return {'target': target, 'agent': agent, 'idle': False, 'blocked': blocked,
                    'waited_sec': round(now - start, 2), 'prompt_blank': False}
        blank = (det(screen) if det else True) and not blocked
        if blank and now - stable_since >= settle:
            return {'target': target, 'agent': agent, 'idle': True,
                    'waited_sec': round(now - start, 2), 'prompt_blank': det(screen) if det else None}
        if now - start >= timeout:
            return {'target': target, 'agent': agent, 'idle': False,
                    'waited_sec': round(now - start, 2), 'prompt_blank': det(screen) if det else None,
                    'last_lines': _tail(screen_view(raw, agent)[0]), 'screen_note': SCREEN_NOTE}
        sleep(poll)
