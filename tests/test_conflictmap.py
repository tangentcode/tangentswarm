"""The conflict map: merge-tree answers kept per commit SHA pair."""
from tangentswarm import conflictmap as C
from tangentswarm import flow

A, B, D, H = 'a' * 40, 'b' * 40, 'd' * 40, 'e' * 40


def test_a_pair_is_computed_once_in_either_order_and_survives_a_restart():
    m, calls = C.ConflictMap(), []
    assert m.clean(A, B, lambda: calls.append(1) or False) is False
    assert m.clean(B, A, lambda: calls.append(1) or True) is False      # same pair, cached
    assert calls == [1] and (m.hits, m.misses) == (1, 1)
    m.save()
    m2 = C.ConflictMap()
    assert m2.clean(A, B, lambda: (_ for _ in ()).throw(AssertionError('cached'))) is False
    assert m2.conflicts_of(A) == [B] and m2.conflicts_of(D) == []


def test_refs_are_never_cached():
    m, calls = C.ConflictMap(), []
    m.clean('refs/heads/main', A, lambda: calls.append(1) or True)
    m.clean('refs/heads/main', A, lambda: calls.append(1) or True)
    assert calls == [1, 1] and m.data == {}


def test_stack_answers_are_kept_per_main_bases_and_head():
    m, calls = C.ConflictMap(), []
    fn = lambda: calls.append(1) or '#3'
    assert m.stack(A, [('#2', B), ('#3', D)], H, fn) == '#3'
    assert m.stack(A, [('#2', B), ('#3', D)], H, fn) == '#3'
    assert m.stack(A, [('#2', B)], H, lambda: calls.append(1) or None) is None
    assert m.stack(A, [('#2', B)], H, lambda: calls.append(1) or '#2') is None   # cached None
    assert calls == [1, 1]


def test_the_map_keeps_the_newest_entries_only(tmp_path):
    m = C.ConflictMap(path=tmp_path / 'm.json', max_entries=2)
    for x in ('1', '2', '3'):
        m.clean(x * 40, A, lambda: True)
    m.save()
    assert list(C.ConflictMap(path=tmp_path / 'm.json').data) == ['p:' + ':'.join(sorted(('2' * 40, A))),
                                                                  'p:' + ':'.join(sorted(('3' * 40, A)))]


def test_flow_uses_the_map_for_its_merge_tree_checks(monkeypatch):
    seen = []
    f = flow.Flow(dry_run=True, out=lambda line: None,
                  g={'merge_tree_clean': lambda d, a, b: seen.append((a, b)) or True})

    class S:
        dir = '/x'
    assert f.clean(S, A, B) and f.clean(S, B, A) and seen == [(A, B)]
