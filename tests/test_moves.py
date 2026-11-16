"""The move log: Ready -> Human Review waits, per Staging floor (Michal 2026-11-14 7:16 PM ET)."""
from tangentswarm import moves as M


def test_first_seen_in_ready_is_kept_until_human_review(tmp_path):
    M.note_ready([5, 6], now=1000)
    M.note_ready([6, 7], now=1600)          # 5 left Ready: it keeps its time
    seen = M._load(M.SEEN)
    assert seen == {'5': 1000, '6': 1000, '7': 1600}
    M.note_ready([], now=1000 + M.KEEP + 1)  # forgotten after 14 days
    assert M._load(M.SEEN) == {'7': 1600}


def test_a_move_line_carries_the_wait_and_the_floor(tmp_path):
    M.note_ready([5], now=1000)
    line = M.log_move(5, 'Staging', 'Human Review', 'a' * 40, note='cheap check', floor=1, now=4600)
    f = line.split('\t')
    assert f[0] == '1970-01-01T01:16:40Z' and f[2:] == ['#5', 'Staging', 'Human Review', 'aaaaaaaa', '3600', '1',
                                                       'cheap check']
    assert M.LOG.read_text() == line + '\n'
    assert M.log_move(9, 'Ready', 'Staging', now=5000).split('\t')[6] == ''  # never seen in Ready


def test_waits_per_floor(tmp_path, capsys):
    M.note_ready([1, 2, 3], now=0)
    M.log_move(1, 'Staging', 'Human Review', floor=1, now=600)
    M.log_move(2, 'Ready', 'Staging', floor=2, now=700)
    M.log_move(2, 'Staging', 'Human Review', floor=2, now=1200)
    M.log_move(3, 'Staging', 'Human Review', floor=2, now=2400)
    assert M.waits() == {'1': [600], '2': [1200, 2400]}
    M.main([])
    assert capsys.readouterr().out == ('floor 1: 1 PR(s) Ready -> Human Review, median 10 min, max 10 min\n'
                                       'floor 2: 2 PR(s) Ready -> Human Review, median 40 min, max 40 min\n')


def test_the_log_never_breaks_a_move(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'LOG', tmp_path / 'dir-is-a-file')
    (tmp_path / 'dir-is-a-file').mkdir()
    assert M.log_move(5, 'Staging', 'Human Review') is None
