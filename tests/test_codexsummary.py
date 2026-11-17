"""tangentswarm #75: a running or findings Codex summary is never clean.

The replays use the real edit history of the Codex summary comments in the incident
(methodology#96 issuecomment-6934742474, tangentswarm#74 issuecomment-6934960674).
"""
import json

import pytest

from tangentswarm import advance as A, codexsummary as S, tick

BOT = {'login': 'chatgpt-codex-connector[bot]'}
H = 'a' * 40


def body(sha, status, any_f, unresolved):
    st = {'code_review': {'commit_sha': sha, 'has_any_findings': any_f,
                          'has_unresolved_findings': unresolved, 'status': status},
          'security_review': {'commit_sha': None, 'has_any_findings': None,
                              'has_unresolved_findings': None, 'status': None}}
    return ('<!-- codex-pull-request-review-summary -->\n**Codex Review** summary\n'
            f'<!-- codex-review-state:v1 {json.dumps(st, separators=(",", ":"))} -->')


def running(sha):
    return body(sha, 'running', None, None)


def done(sha, findings):
    return body(sha, 'completed', findings, findings)


def pr(head=H, comments=(), reviews=(), threads=()):
    return {'headRefOid': head, 'comments': {'nodes': [{'author': BOT, 'body': b} for b in comments]},
            'reviews': {'nodes': list(reviews)}, 'reviewThreads': {'nodes': list(threads)},
            'commits': {'nodes': [{'commit': {'committedDate': '2026-11-17T01:00:00Z'}}]}}


def results(p):
    """The three readers on one PR: they must agree."""
    return A.codex_decision(p)[0], 'clean' if A.codex_clean(p) else 'not clean', tick.codex_status(p)[0]


def agree(p, want):
    d, c, t = results(p)
    assert (d, t) == (want, want)
    assert c == ('clean' if want == 'clean' else 'not clean')


def test_parser():
    assert S.result('no state here', H) is None
    assert S.result('<!-- codex-review-state:v1 {bad json} -->', H) is None
    assert S.result(running(H), H) == 'pending'
    assert S.result(body(H, 'completed', None, None), H) == 'pending'  # null flags
    assert S.result(body(H, 'completed', False, None), H) == 'pending'
    assert S.result(body(H, None, False, False), H) == 'pending'  # no status
    assert S.result(done('b' * 40, False), H) == 'pending'  # another sha
    assert S.result(done(H, False), '') == 'pending'  # no head
    assert S.result(done(H, False), H) == 'clean'
    assert S.result(done(H, True), H) == 'findings'
    assert S.result(body(H, 'completed', False, True), H) == 'findings'
    assert S.result(body(H, 'completed', True, False), H) == 'findings'
    assert S.result(body(H, 'running', True, None), H) == 'pending'  # not completed yet


def test_a_running_summary_is_pending_not_a_pass():
    agree(pr(comments=[running(H)]), 'pending')


def test_a_summary_with_null_findings_is_pending():
    agree(pr(comments=[body(H, 'completed', None, None)]), 'pending')


def test_unresolved_findings_alone_are_findings():
    agree(pr(comments=[body(H, 'completed', False, True)]), 'findings')


def test_a_findings_summary_beats_every_clean_signal():
    zero = {'author': BOT, 'commit': {'oid': H}, 'comments': {'totalCount': 0}}
    agree(pr(comments=[done(H, True)], reviews=[zero]), 'findings')
    receipt = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{H[:10]}`"
    agree(pr(comments=[receipt, done(H, True)]), 'findings')
    agree(pr(comments=[done(H, False), done(H, True)]), 'findings')  # an older clean summary
    agree(pr(comments=[done(H, True), done(H, False)]), 'findings')


def test_a_completed_clean_summary_is_clean():
    agree(pr(comments=[done(H, False)]), 'clean')
    agree(pr(comments=[done('b' * 40, True), done(H, False)]), 'clean')  # findings on an old head


def test_a_running_summary_is_not_a_result_for_the_head():
    assert not tick.codex_reviewed_head(pr(comments=[running(H)]))
    assert tick.codex_reviewed_head(pr(comments=[done(H, False)]))
    assert tick.codex_reviewed_head(pr(comments=[done(H, True)]))


# Each step: (head on GitHub, summary body, what every reader must say).
METHODOLOGY_96 = [
    ('213d3b203db9262a051dd5ae793ed3c05eba6652', running('213d3b203db9262a051dd5ae793ed3c05eba6652'), 'pending'),
    ('213d3b203db9262a051dd5ae793ed3c05eba6652', running('213d3b203db9262a051dd5ae793ed3c05eba6652'), 'pending'),
    ('213d3b203db9262a051dd5ae793ed3c05eba6652', running('213d3b203db9262a051dd5ae793ed3c05eba6652'), 'pending'),
    ('213d3b203db9262a051dd5ae793ed3c05eba6652', done('213d3b203db9262a051dd5ae793ed3c05eba6652', True), 'findings'),
    ('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9', done('213d3b203db9262a051dd5ae793ed3c05eba6652', True), 'pending'),
    ('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9', running('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9'), 'pending'),
    ('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9', running('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9'), 'pending'),
    ('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9', done('c4648b9de7a3fc0dcbaba68bbffaf92627a30de9', False), 'clean'),
]

TANGENTSWARM_74 = [
    ('96bfaf9cc8077136183232e69b5462a61474ac82', running('96bfaf9cc8077136183232e69b5462a61474ac82'), 'pending'),
    ('96bfaf9cc8077136183232e69b5462a61474ac82', running('96bfaf9cc8077136183232e69b5462a61474ac82'), 'pending'),
    ('96bfaf9cc8077136183232e69b5462a61474ac82', done('96bfaf9cc8077136183232e69b5462a61474ac82', True), 'findings'),
    ('2b38afa040c50c9dff465166c00bae1c12a37cce', done('96bfaf9cc8077136183232e69b5462a61474ac82', True), 'pending'),
    ('2b38afa040c50c9dff465166c00bae1c12a37cce', running('2b38afa040c50c9dff465166c00bae1c12a37cce'), 'pending'),
    ('2b38afa040c50c9dff465166c00bae1c12a37cce', done('2b38afa040c50c9dff465166c00bae1c12a37cce', True), 'findings'),
    ('09e032583cef69f55229c5d76de73402a738c378', running('09e032583cef69f55229c5d76de73402a738c378'), 'pending'),
    ('09e032583cef69f55229c5d76de73402a738c378', done('09e032583cef69f55229c5d76de73402a738c378', False), 'clean'),
]


@pytest.mark.parametrize('steps', [METHODOLOGY_96, TANGENTSWARM_74], ids=['methodology-96', 'tangentswarm-74'])
def test_replay_the_incident_edit_sequences(steps):
    """Codex edits one summary comment in place; at no running step may a reader pass."""
    for head, summary, want in steps:
        agree(pr(head=head, comments=[summary]), want)
