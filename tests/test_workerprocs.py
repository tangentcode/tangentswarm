"""Leftover worker processes (MethBot 2026-11-15 4:31 AM ET: #532's dev server ran 1h45m
after the work was done, park refused, and only a person could stop it)."""
import os

from tangentswarm import workerprocs as W


def fake_proc(tmp_path, wt, rows):
    proc = tmp_path / 'proc'
    for pid, cwd, comm, cmd in rows:
        d = proc / str(pid)
        d.mkdir(parents=True)
        os.symlink(cwd, d / 'cwd')
        (d / 'comm').write_text(comm + '\n')
        (d / 'cmdline').write_text('\0'.join(cmd.split(' ')))
    return proc


def test_leftovers_are_the_worktree_processes_except_shells_claude_and_mcp(tmp_path):
    wt = str(tmp_path / 'platform.x')
    proc = fake_proc(tmp_path, wt, [
        (100, wt, 'bash', 'bash'),
        (101, wt, 'claude', '/home/u/.npm-global/bin/claude --resume s'),
        (102, wt, 'python3', '/home/u/bin/swarm-worker-mcp'),
        (103, wt, 'python', '.venv/bin/python tcode-server.py --port 5391'),
        (104, wt + '/web', 'node', 'node node_modules/.bin/vite --port 5173'),
        (105, '/elsewhere', 'python', 'python other.py'),
    ])
    assert W.leftovers(wt, proc=proc) == [(103, '.venv/bin/python tcode-server.py --port 5391'),
                                           (104, 'node node_modules/.bin/vite --port 5173')]


def test_the_stop_text_names_the_pids_on_one_line():
    t = W.stop_text(532, [(2790171, '.venv/bin/python tcode-server.py --port 5391')])
    assert 'pid 2790171 (.venv/bin/python tcode-server.py --port 5391)' in t and '\n' not in t
