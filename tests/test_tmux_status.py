"""Tests for swarm-tmux-status (tangentswarm #49): classification, views and zoom."""
import io

from tangentswarm import tmux, tmux_status as ts

BAR = '─' * 80

BUSY = '\n'.join([
    '● I read the brief.',
    '● Bash(pytest -q)',
    '  ⎿  Running… (16s · timeout 10m)',
    '',
    '✢ Lollygagging… (1m 51s · ↓ 3.3k tokens)',
    '',
    BAR, '❯ ', BAR,
    '  ⏵⏵ auto mode on · 1 shell · esc to interrupt · ↓ to manage',
    '', '',
])

IDLE = '\n'.join([
    '● Bash(pytest -q)',
    '  ⎿  12 passed',
    '● Done. The PR is ready; I said "esc to interrupt" in my notes.',
    '',
    BAR, '❯ ', BAR,
    '  ⏵⏵ auto mode on (shift+tab to cycle)',
])

SHELL = '\n'.join([
    '  ⏵⏵ auto mode on (shift+tab to cycle) · PR #447 · esc to interrupt',
    'Resume this session with:',
    'claude --resume b7d383bb',
    'memnar@tangentcode:~/ver/platform.x (x)',
    '$ ',
    '', '',
])


# -- classification -----------------------------------------------------------------------------

def test_busy_claude_is_working():
    assert ts.classify('claude', BUSY) == 'working'


def test_idle_claude_is_idle_even_with_the_busy_words_in_its_transcript():
    assert ts.classify('claude', IDLE) == 'idle'


def test_old_spinner_line_counts_as_busy():
    screen = '\n'.join(['● Reading', '', '✻ Thinking… (12s · esc to interrupt)', '', BAR, '❯ ', BAR, ''])
    assert ts.classify('claude', screen) == 'working'


def test_shell_is_shell_even_with_old_claude_text_on_screen():
    assert ts.classify('bash', SHELL) == 'shell'
    assert ts.classify('zsh', '% ') == 'shell'


def test_node_is_claude_only_with_claude_input_box():
    assert ts.classify('node', BUSY) == 'working'
    assert ts.classify('node', IDLE) == 'idle'
    assert ts.classify('node', 'listening on :3000\n') == 'program'


def test_other_program():
    assert ts.classify('python', '19:44 handled event\n') == 'program'


def test_last_activity_claude_is_last_bullet_entry():
    assert ts.last_activity(BUSY, 'working') == 'Bash(pytest -q)'
    assert ts.last_activity(IDLE, 'idle').startswith('Done. The PR is ready')


def test_last_activity_shell_skips_the_prompt():
    assert ts.last_activity(SHELL, 'shell') == 'claude --resume b7d383bb'
    assert ts.last_activity('$ \n', 'shell') == ''


def test_last_activity_program_is_last_line():
    assert ts.last_activity('a\nb line\n\n', 'program') == 'b line'


def test_ticket_from_window_name():
    assert ts.ticket('mn-458-jt-viewmat') == 'mn #458'
    assert ts.ticket('swarm-50-tmux-status') == 'swarm #50'
    assert ts.ticket('ptw-7') == 'ptw #7'
    assert ts.ticket('events') == ''
    assert ts.ticket('claude-swarm-shells') == ''


def pane(widx, name, pid, cmd, pidx=0, active=True, dead=False):
    return dict(window_index=widx, window_name=name, pane_id=pid, current_command=cmd,
                pane_index=pidx, active=active, dead=dead, current_path='/w')


def test_window_rows_one_per_window_busiest_pane_wins():
    panes = [pane(1, 'mn-1-a', '%1', 'bash', 0, active=True),
             pane(1, 'mn-1-a', '%2', 'claude', 1, active=False),
             pane(2, 'mn-2-b', '%3', 'bash'),
             pane(3, 'mn-3-c', '%4', 'claude'),
             pane(4, 'gone', '%5', 'bash', dead=True)]
    rows = ts.window_rows(panes, {'%1': SHELL, '%2': BUSY, '%3': SHELL, '%4': IDLE})
    assert [(w.index, w.cls, w.pane, w.panes) for w in rows] == [
        (1, 'working', '%2', 2), (2, 'shell', '%3', 1), (3, 'idle', '%4', 1)]
    assert rows[0].ticket == 'mn #1' and rows[0].activity == 'Bash(pytest -q)'


def test_window_rows_tie_goes_to_active_pane():
    panes = [pane(1, 'w', '%1', 'bash', 0, active=False), pane(1, 'w', '%2', 'bash', 1, active=True)]
    (row,) = ts.window_rows(panes, {})
    assert row.pane == '%2'


def test_read_windows_uses_tmux(monkeypatch):
    monkeypatch.setattr(tmux, 'list_panes', lambda target: [pane(1, 'mn-1-a', '%1', 'claude')])
    monkeypatch.setattr(tmux, 'capture_pane', lambda target, **kw: '\x1b[1m' + BUSY)
    (row,) = ts.read_windows('agents')
    assert row.cls == 'working'


# -- views --------------------------------------------------------------------------------------

def W(index, cls, name=None, activity='doing x', command='claude'):
    return ts.Window(index=index, name=name or f'mn-{index}-t', pane=f'%{index}', command=command,
                     cls=cls, activity=activity)


WINDOWS = [W(1, 'shell', command='bash'), W(2, 'working'), W(3, 'idle'), W(4, 'working'),
           W(5, 'program', name='events', command='python')]


def view(**kw):
    v = ts.View(**kw)
    v.set_windows(list(WINDOWS))
    return v


def test_default_view_shows_only_working():
    v = view()
    assert [w.index for w in v.visible()] == [2, 4]
    text = '\n'.join(line for line, _ in v.render(120, 40))
    assert 'mn-2-t' in text and 'mn-4-t' in text and 'mn-1-t' not in text and 'mn-3-t' not in text
    assert '2 working, 1 idle, 1 shell, 1 program - working only' in text


def test_toggle_shows_all_windows_and_back():
    v = view()
    v.key('a')
    assert [w.index for w in v.visible()] == [1, 2, 3, 4, 5]
    text = '\n'.join(line for line, _ in v.render(120, 40))
    assert 'all windows' in text and 'idle' in text and 'python' in text
    v.key('a')
    assert [w.index for w in v.visible()] == [2, 4]


def test_toggle_keeps_selection_when_it_stays_visible():
    v = view()
    v.key('down')
    assert v.selected == 4
    v.key('a')
    assert v.selected == 4
    v.key('up')
    assert v.selected == 3
    v.key('a')   # idle window 3 is hidden: the selection moves to a visible row
    assert v.selected == 2


def test_empty_default_view_says_how_to_show_all():
    v = ts.View()
    v.set_windows([W(1, 'shell')])
    assert v.visible() == []
    assert any('press a to show all' in line for line, _ in v.render(80, 10))
    assert v.key('enter') is None and v.zoomed is None


def test_selected_row_is_marked():
    v = view()
    v.key('j')
    sel = [line for line, s in v.render(120, 40) if s]
    assert len(sel) == 1 and 'mn-4-t' in sel[0]


def test_lines_fit_the_width():
    v = view(show_all=True)
    v.windows[0].activity = 'x' * 500
    assert all(len(line) <= 60 for line, _ in v.render(60, 40))


def test_quit():
    assert view().key('q') == 'quit'


# -- zoom ---------------------------------------------------------------------------------------

def test_zoom_into_selected_window_and_back():
    v = view()
    v.key('down')
    assert v.key('enter') == 'zoom'
    assert v.zoomed.index == 4
    v.zoom_lines = [f'line {i}' for i in range(100)]
    out = v.render(80, 11)
    assert 'zoom: agents:4 mn-4-t' in out[0][0]
    assert [line for line, _ in out[1:]] == [f'line {i}' for i in range(90, 100)]
    assert v.key('q') is None      # q leaves the zoom, it does not quit
    assert v.zoomed is None and v.zoom_lines == []
    assert v.key('q') == 'quit'


def test_zoom_survives_refresh():
    v = view()
    v.key('z')
    v.set_windows([W(2, 'idle', activity='new'), W(4, 'working')])
    assert v.zoomed.index == 2 and v.zoomed.cls == 'idle'


def test_read_zoom_reports_a_missing_pane(monkeypatch):
    def boom(target, **kw):
        raise tmux.TmuxError(['tmux'], 1, "can't find pane")
    monkeypatch.setattr(tmux, 'capture_pane', boom)
    assert ts.read_zoom('%9') == ["(cannot read %9: can't find pane)"]


def test_read_zoom_asks_for_history(monkeypatch):
    seen = {}

    def cap(target, history_lines=None, **kw):
        seen['h'] = history_lines
        return 'a\nb\n\n'
    monkeypatch.setattr(tmux, 'capture_pane', cap)
    assert ts.read_zoom('%1') == ['a', 'b']
    assert seen['h'] == ts.ZOOM_HISTORY


# -- command line -------------------------------------------------------------------------------

def test_main_once_prints_working_only_by_default():
    out = io.StringIO()
    assert ts.main(['--once'], reader=lambda s: list(WINDOWS), out=out) == 0
    text = out.getvalue()
    assert 'mn-2-t' in text and 'mn-1-t' not in text and 'enter zoom' not in text


def test_main_once_all():
    out = io.StringIO()
    ts.main(['--once', '--all'], reader=lambda s: list(WINDOWS), out=out)
    assert 'mn-1-t' in out.getvalue() and 'events' in out.getvalue()


def test_main_zoom_prints_window_output():
    out = io.StringIO()
    rc = ts.main(['--zoom', '3'], reader=lambda s: list(WINDOWS),
                 zoom_reader=lambda p: [f'{p} out {i}' for i in range(300)], out=out)
    lines = out.getvalue().splitlines()
    assert rc == 0 and 'zoom: agents:3' in lines[0] and len(lines) == 301 and lines[-1] == '%3 out 299'


def test_main_zoom_unknown_window():
    assert ts.main(['--zoom', '99'], reader=lambda s: list(WINDOWS), out=io.StringIO()) == 2


def test_main_missing_session():
    def reader(s):
        raise tmux.TmuxError(['tmux'], 1, "can't find session: nope")
    assert ts.main(['--once', '--session', 'nope'], reader=reader, out=io.StringIO()) == 2
