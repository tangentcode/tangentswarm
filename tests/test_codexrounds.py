"""Codex rounds with no cap (tangentswarm #40 item 7), and the hr-gate re-run on an edit of a
Staging PR (item 5)."""
import json

from tangentswarm import board, codexrounds as CR, tick, webhook as W

from test_webhook import Fake, codex_pr


def test_every_round_is_posted_and_logged_with_no_cap():
    calls, st = [], tick.State(tick.STATE_DIR / 'state.json')
    for k in range(1, 13):
        assert CR.request(7, f'{k:040d}', 'test', sh=calls.append, state=st) == k
    assert len(calls) == 12 and calls[0] == ['gh', 'pr', 'comment', '7', '-R', CR.REPO, '--body', '@codex review']
    assert st.done(f"codex-request:7@{12:040d}")
    rows = CR.rounds(7)
    assert len(rows) == 12 and json.loads(json.loads(rows[-1]['notes'])[0])['round'] == 12


def test_the_report_lists_the_prs_with_most_rounds_and_what_round_1_missed():
    sh = lambda argv: None  # noqa: E731
    CR.request(5, 'a' * 40, 't', sh=sh)
    CR.result(5, 'a' * 40, 'clean', 'Codex clean')
    for h in ('b', 'c', 'd'):
        CR.request(6, h * 40, 't', sh=sh)
    CR.result(6, 'b' * 40, 'findings', '2 open Codex thread(s)')
    CR.result(6, 'c' * 40, 'findings', '1 open Codex thread(s)')
    CR.result(6, 'd' * 40, 'clean', 'Codex clean')
    rep = CR.report(days=7)
    assert [r['pr'] for r in rep] == [6, 5] and rep[0]['rounds'] == 3
    assert rep[0]['results'][0] == (1, 'b' * 40, 'findings', '2 open Codex thread(s)')
    assert rep[0]['results'][2][2] == 'clean'
    assert CR.report(days=7, now=__import__('time').time() + 30 * 86400) == []


def test_the_webhook_handoff_logs_a_round_and_codex_results_are_logged():
    ex = Fake(col=board.AUTO_REVIEW, full=dict(codex_pr(), isDraft=False))
    ex.run(W.Action('handoff', 9, 'new head', dict(head='a' * 40, push=True)))
    assert ['gh', 'pr', 'comment', '9', '-R', W.REPO, '--body', '@codex review'] in ex.calls
    assert len(CR.rounds(9)) == 1
    ex = Fake(col=board.AUTO_REVIEW, full=codex_pr(findings=True))
    ex.run(W.Action('codex', 9))
    from tangentswarm import eventdb
    res = eventdb.store().rows("stage=? AND pr=9", (CR.RESULT,))
    assert len(res) == 1 and json.loads(res[0]['decisions']) == ['findings']


def test_an_edit_of_a_staging_pr_reruns_hr_gate_at_once():
    p = {'action': 'edited', 'repository': {'full_name': W.REPO}, 'sender': {'login': 'bob'},
         'pull_request': {'number': 7, 'draft': False, 'base': {'ref': 'main'}}}
    assert [a.kind for a in W.classify('pull_request', p)] == ['edited']
    ex = Fake(col=board.STAGING)
    ex.run(W.Action('edited', 7, 'PR body edited'))
    assert ex.queued == [['--apply', '7']] and ex.cascades == []
    ex = Fake(col=board.READY)
    ex.run(W.Action('edited', 7, 'PR body edited'))
    assert ex.queued == [] and ex.cascades == [1]
