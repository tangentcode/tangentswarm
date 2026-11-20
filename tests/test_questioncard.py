"""tangentswarm #156: the Needs Input question goes on the PR card, stands on its own, names a
conflict with main, and a brief-only PR body gets a summary. No network: gh and GraphQL are fakes."""
import pytest

from tangentswarm import brief, questioncard as qc
from tangentswarm.stophook import QUESTION_MARK

PR = dict(title='Badge colors', body='', mergeable='MERGEABLE', issues={588: 'Old badge colors'})
Q = ('The badge can use the old colors or the new ones. Old: no change for users. New: matches the '
     'design doc, but the screenshots change. I pick new.')


def test_the_comment_stands_on_its_own():
    b = qc.body(QUESTION_MARK, 589, Q, PR, [588])
    assert b.startswith(QUESTION_MARK)
    assert '**What this PR is for:** PR #589: Badge colors. It fixes issue #588 (Old badge colors).' in b
    assert '> The badge can use the old colors' in b and 'I pick new.' in b
    assert 'issue #588 and PR #589' in b and 'move the card back to In Progress' in b
    assert 'conflict' not in b.lower()
    assert '**What this PR is for:** PR #7.' in qc.body(QUESTION_MARK, 7, Q, {}, [])  # a failed read still says the PR


def test_a_conflict_with_main_is_named_with_who_fixes_it():
    b = qc.body(QUESTION_MARK, 589, Q, PR, [588], conflict=True)
    assert '**Merge conflict:** PR #589 also conflicts with main. The worker will fix the conflict after your answer' in b


def test_a_blocker_uses_the_same_shape():
    b = qc.body('M', 12, 'staging is down', PR, [], kind='blocker', how='reported with report_status')
    assert '**The worker is blocked on #12** (the card is in Needs Input; reported with report_status).' in b
    assert '**The blocker:**\n\n> staging is down' in b and 'this question is on PR #12' in b


def test_targets_are_the_issue_and_the_pr_each_with_its_own_key():
    assert qc.targets(589, [588, 590]) == [('issue', 588, ''), ('pr', 589, ':pr589')]
    assert qc.targets(12, []) == [('pr', 12, '')]
    assert qc.comment_argv('pr', 12, 'o/r', 'x') == ['gh', 'pr', 'comment', '12', '-R', 'o/r', '--body', 'x']


@pytest.mark.parametrize('q, pointer', [
    ('see the issue', True), ('See #588.', True), ('Please read the brief', True),
    ('see https://github.com/tangentcode/platform/issues/588', True), ('Look at the ticket comments', True),
    ('Which port, 80 or 8080?', False), (Q, False),
    ('See: ' + 'the badge can use old or new colors; old changes nothing, new changes shots. ' * 3, False),
])
def test_a_question_that_only_points_elsewhere(q, pointer):
    assert qc.pointer_only(q) is pointer


@pytest.mark.parametrize('body, only', [
    ('', True), ('Fixes #588', True), ('Fixes #588\n\n' + brief.section('the brief'), True),
    ('Closes tangentcode/platform#588.\n' + brief.section('b'), True),
    ('Fixes #588\n\nThe badge gets new colors.\n\n' + brief.section('b'), False),
    (qc.SUMMARY_MARK + '\n## Summary\n\nx\nFixes #588\n' + brief.section('b'), False),
])
def test_brief_only_bodies(body, only):
    assert qc.brief_only(body) is only


def test_a_brief_only_body_gets_a_summary_at_the_top():
    ran = []
    body = 'Fixes #588\n\n' + brief.section('the brief')
    assert qc.summarize(589, dict(PR, body=body), [588], 'o/r', ran.append) == 'wrote'
    (argv,) = ran
    assert argv[:5] == ['gh', 'api', '-X', 'PATCH', 'repos/o/r/pulls/589'] and argv[5] == '-f'
    new = argv[6][len('body='):]
    assert new.startswith(qc.SUMMARY_MARK + '\n## Summary\n\nPR #589: Badge colors. It fixes issue #588')
    assert 'The worker has a question for Michal' in new and new.endswith(body)
    assert brief.has_brief(new) and not qc.brief_only(new)  # once only
    ran.clear()
    assert qc.summarize(589, dict(PR, body='Fixes #588\n\nReal text.'), [588], 'o/r', ran.append) is None
    assert qc.summarize(589, {}, [588], 'o/r', ran.append) is None and ran == []  # unread body: no edit


def test_conflicts():
    never = lambda wt, head: pytest.fail('no git when GitHub knows')  # noqa: E731
    assert qc.conflicts({'mergeable': 'CONFLICTING'}, 'wt', 'a' * 40, never) is True
    assert qc.conflicts({'mergeable': 'MERGEABLE'}, 'wt', 'a' * 40, never) is False
    assert qc.conflicts({'mergeable': 'UNKNOWN'}, 'wt', 'a' * 40, lambda wt, h: False) is True
    assert qc.conflicts({}, 'wt', 'a' * 40, lambda wt, h: True) is False
    assert qc.conflicts({}, 'wt', 'uncommitted changes', never) is False  # no pushed head: unknown

    def boom(wt, head):
        raise OSError('no git')
    assert qc.conflicts({'mergeable': 'UNKNOWN'}, 'wt', 'a' * 40, boom) is False


def test_info_reads_the_pr_and_never_raises():
    seen = []

    def gql(q, **v):
        seen.append(v)
        return {'repository': {'pullRequest': {'title': 'T', 'body': 'B', 'mergeable': 'CONFLICTING',
                'closingIssuesReferences': {'nodes': [{'number': 588, 'title': 'I'}]}}}}
    assert qc.info({'gql': gql}, 589, 'o/r') == dict(title='T', body='B', mergeable='CONFLICTING', issues={588: 'I'})
    assert seen == [dict(owner='o', name='r', n=589)]

    def down(q, **v):
        raise RuntimeError('502')
    assert qc.info({'gql': down}, 589, 'o/r') == {} and qc.info({}, 589, 'o/r') == {}
