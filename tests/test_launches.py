"""swarm-launch: live memory limits, no held-launch list, one launch per pass, the pressure flag."""
import pytest

from tangentswarm import launches as L, tick

MB, GB = L.MB, 1024 ** 3




def show(current, high='4831838208', mx='5368709120'):
    return lambda s: f"MemoryCurrent={current}\nMemoryHigh={high}\nMemoryMax={mx}\n"


def meminfo(avail_kb):
    return lambda: f"MemTotal: 16000000 kB\nMemFree: 100 kB\nMemAvailable: {avail_kb} kB\n"


def mem(current, high=4608 * MB, mx=5120 * MB, avail=4 * GB):
    return dict(current=current, high=high, max=mx, avail=avail)


def test_memory_reads_the_live_limits_not_a_fixed_size():
    m = L.memory(show=show(3347681280), meminfo=meminfo(4000000), loadavg=lambda: (0.5, 0.4, 0.3), cpus=lambda: 4)
    assert m == dict(current=3347681280, high=4831838208, max=5368709120, avail=4000000 * 1024,
                     load=[0.5, 0.4, 0.3], cpus=4)
    assert L.headroom(m) == 4831838208 - 3347681280


def test_memory_handles_unset_limits():
    m = L.memory(show=show(100, high='infinity', mx='infinity'), meminfo=meminfo(4000000), loadavg=lambda: (0, 0, 0))
    assert m['high'] is None and m['max'] is None and L.headroom(m) is None and L.admits(m)
    m = L.memory(show=show(100, high='infinity', mx=str(1000 * MB)), meminfo=meminfo(4000000))
    assert L.headroom(m) == 1000 * MB - 100


def test_memory_refuses_unreadable_slice():
    with pytest.raises(RuntimeError):
        L.memory(show=lambda s: "MemoryCurrent=[not set]\n", meminfo=meminfo(1))


def test_admits_needs_cpu_load_under_the_per_cpu_limit():
    """tangentswarm #44: starts are decided on the live CPU load too (the 5-minute average)."""
    assert L.admits(dict(mem(3000 * MB), load=[9.0, 5.9, 3.0], cpus=4))
    assert not L.admits(dict(mem(3000 * MB), load=[1.0, 6.0, 3.0], cpus=4))
    assert not L.cpu_admits(dict(load=[0, 2.0, 0], cpus=1)) and L.cpu_admits({})


def test_admits_needs_slice_headroom_and_host_memory():
    assert L.admits(mem(3000 * MB))
    assert not L.admits(mem(4000 * MB))                       # 608 MB under MemoryHigh
    assert not L.admits(mem(1000 * MB, avail=int(1.4 * GB)))  # host short
    assert L.admits(mem(3000 * MB, high=None))                # falls back to MemoryMax


class Starter:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, branch, window=None):
        self.calls.append((branch, window))
        return self.results.pop(0)


def test_a_memory_hold_writes_no_list():
    """tangentswarm #40 (Michal 2026-11-15 6:44 PM ET): the held-launch list is retired."""
    st = Starter((3, dict(held=True, window='mn-1-a', pane='%1')))
    assert L.launch('a', pr=1, brief='/b/1.txt', start=st)[0] == 'held'
    assert not hasattr(L, 'read_queue') and not hasattr(L, 'hold') and not hasattr(L, 'QUEUE')


def test_launch_sends_the_brief_line():
    sent = []
    st = Starter((0, dict(window='mn-1-a', pane='%1', unit='u1')))
    what, out = L.launch('a', 'mn-1-a', 1, '/b/1.txt', start=st, send=lambda *a: sent.append(a))
    assert what == 'launched' and sent == [('%1', 'u1', '/b/1.txt')]


def test_launch_reports_a_failed_brief_but_counts_as_launched():
    def boom(*a):
        raise RuntimeError('no ready Claude prompt')
    what, out = L.launch('a', brief='/b', start=Starter((0, dict(pane='%1', unit='u'))), send=boom)
    assert what == 'launched' and 'no ready' in out['brief_error']


def test_brief_line_is_one_line():
    assert '\n' not in L.brief_line('/b/486.txt') and '/b/486.txt' in L.brief_line('/b/486.txt')


def make_tick(prs, dry=True, data=None):
    t = tick.Tick.__new__(tick.Tick)
    t.dry, t.verbose, t.out, t.did, t.notes = dry, False, print, [], []
    t.prs, t.panes = prs, []

    class S:
        pass
    t.state = S()
    t.state.data = data if data is not None else {}
    return t


def scaffold_tick(monkeypatch, tmp_path, subject, brief_text='brief'):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path)
    if brief_text:
        (tmp_path / '7.txt').write_text(brief_text)
    monkeypatch.setattr(tick, 'git', lambda path, *a: subject)
    pr = dict(number=7, state='OPEN', draft=True, branch='b', head='h' * 40, col='In Progress', parked=False,
              rank=1, item='i', title='t')
    shell = dict(id='%9', window='mn-7-b', claude=False, cls='shell', pr=7, parked=False, path='/w',
                 wt=dict(top='/w', branch='b', head='h' * 40, dirty=False, ahead=0))
    t = make_tick({7: pr})
    t.panes = [shell]
    return t


def test_an_idle_scaffold_shell_is_reported_not_queued(monkeypatch, tmp_path):
    """No held list (#40): the cascade's start retry starts it when capacity is free."""
    t = scaffold_tick(monkeypatch, tmp_path, 'Start #440')
    t.dry = False
    t.no_claude(7, t.prs[7], t.panes)
    assert t.did == [] and "the cascade's start retry starts it" in t.notes[0][1]


def test_shell_with_real_commits_or_no_brief_is_still_judgment(monkeypatch, tmp_path):
    t = scaffold_tick(monkeypatch, tmp_path, 'Fix the title record button')
    t.no_claude(7, t.prs[7], t.panes)
    assert 'Claude exited' in t.notes[0][1]
    (tmp_path / '7.txt').unlink()
    t = scaffold_tick(monkeypatch, tmp_path, 'Start #440', brief_text=None)
    t.no_claude(7, t.prs[7], t.panes)
    assert 'Claude exited' in t.notes[0][1]


def test_brief_line_tells_the_coder_to_use_absolute_image_urls():
    line = L.brief_line('/b/486.txt')
    assert 'absolute https URLs' in line and 'user-attachments' in line and 'swarm-shots' in line
    assert 'Never a relative file name' in line


def test_rows_of_the_retired_held_list_become_unstarted_records_once(tmp_path):
    """Codex on PR #46: a launch held before the upgrade kept its retry only in that file."""
    import json as _json
    from tangentswarm import slots
    old = tmp_path / 'swarm-held.json'
    old.write_text(_json.dumps([{'branch': 'b486', 'pr': 486, 'brief': '/b/486.txt'}, {'branch': 'nopr'}]))
    assert L.migrate_held(old) == [486] and not old.exists()
    rec = slots.load()['unstarted']['486']
    assert rec['branch'] == 'b486' and rec['brief'] == '/b/486.txt'
    assert L.migrate_held(old) == []  # no file: nothing


def test_the_scaffold_fallback_restores_the_retry_record(monkeypatch, tmp_path):
    from tangentswarm import slots
    t = scaffold_tick(monkeypatch, tmp_path, 'Start #440')
    t.dry = False
    t.no_claude(7, t.prs[7], t.panes)
    assert slots.load()['unstarted']['7']['branch'] == 'b'


def test_the_migration_reads_the_old_swarm_held_path(tmp_path, monkeypatch):
    """Codex on PR #46: the old code wrote the list to $SWARM_HELD when it was set."""
    import json as _json
    from tangentswarm import slots
    old = tmp_path / 'custom-held.json'
    old.write_text(_json.dumps([{'branch': 'b9', 'pr': 9, 'brief': '/b/9.txt'}]))
    monkeypatch.setattr(L, 'OLD_HELD', None)
    monkeypatch.setenv('SWARM_HELD', str(old))
    assert L.old_held() == old
    assert L.migrate_held() == [9] and not old.exists()
    assert slots.load()['unstarted']['9']['branch'] == 'b9'
    monkeypatch.delenv('SWARM_HELD')
    assert L.old_held() == L.HOME / 'var' / 'swarm-held.json'
