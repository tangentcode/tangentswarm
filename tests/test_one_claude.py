"""One Claude per worktree (tangentswarm #40 item 8): every launcher refuses a second one."""
import os

import pytest

from tangentswarm import launches, slots, workerprocs, worktrees
from tangentswarm.worktrees import WorktreeError

from test_slots import FakeHost, FakeSP, wt  # noqa: F401
from test_worktrees import Recorder, claude_bin, w  # noqa: F401


def fake_proc(tmp_path, procs):
    root = tmp_path / 'proc'
    root.mkdir()
    for pid, (comm, cmd, cwd, state) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        (d / 'cmdline').write_bytes(cmd.replace(' ', '\0').encode())
        (d / 'stat').write_text(f"{pid} ({comm}) {state} 1 0")
        os.symlink(cwd, d / 'cwd')
    return root


def test_claudes_in_finds_a_live_claude_in_the_worktree_or_below(tmp_path):
    wtp = str(tmp_path / 'platform.feat')
    root = fake_proc(tmp_path, {10: ('claude', 'claude --resume x', wtp + '/web', 'S'),
                                11: ('claude', 'claude', '/elsewhere', 'S'),
                                12: ('claude', 'claude', wtp, 'Z'),
                                13: ('python', 'python -m pytest', wtp, 'S')})
    assert workerprocs.claudes_in(wtp, proc=root) == [(10, 'claude --resume x')]
    why = workerprocs.refuse_second_claude(wtp, find=lambda w: [(10, 'claude')])
    assert 'one Claude per worktree' in why and 'pid 10' in why
    assert workerprocs.refuse_second_claude(wtp, find=lambda w: []) is None


def test_swarm_launch_refuses_before_running_swarm_start_scoped(monkeypatch):
    ran = []
    monkeypatch.setattr(launches.subprocess, 'run', lambda *a, **k: ran.append(a))
    rc, out = launches.start_scoped('feat/x', refuse=lambda p: f'a Claude already runs in {p}')
    assert rc != 0 and out['refused'].endswith('platform.feat-x') and ran == []
    assert launches.launch('feat/x', start=lambda b, w: (rc, out))[0] == 'refused'


def test_start_agent_refuses_a_claude_started_outside_tmux(w, claude_bin):  # noqa: F811
    rec = Recorder()
    with pytest.raises(WorktreeError, match='one Claude per worktree'):
        worktrees.spawn_agent('claude', 'proj', 'feat', run=rec,
                              refuse=lambda p: 'a Claude already runs in x: one Claude per worktree')
    assert rec.calls == []
    worktrees.spawn_agent('claude', 'proj', 'feat', run=rec, refuse=lambda p: None)
    assert len(rec.calls) == 1


def test_revive_refuses_when_another_claude_runs_in_the_worktree(wt):  # noqa: F811
    slots.save({'parked': {'7': dict(session='sid-7', worktree=wt)}, 'rework': {}})
    sp, host, out = FakeSP(wt, live={'pane': '%50', 'agent': None}), FakeHost(), []
    rc = slots.revive('#7', 'go', out=out.append, sp=sp, host=host, claudes=[],
                      refuse=lambda p: f'a Claude already runs in {p}')
    assert rc == 1 and host.typed == [] and 'already runs' in out[-1]
