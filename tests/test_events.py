"""tangentswarm #31: GitHub events start mn-workflow and the MinBot check at once, instead
of waiting for the 5-minute mn-workflow timer and the Codex poll."""
import json

from tangentswarm import events as E


def append(*recs):
    with open(E.EVENTS, 'a') as f:
        for r in recs:
            f.write(json.dumps(r) + '\n')


REVIEW = {'event': 'pull_request_review', 'repo': E.REPO, 'pr': 514, 'reviewer': 'chatgpt-codex-connector[bot]',
          'state': 'commented'}
COMMENT = {'event': 'issue_comment', 'repo': E.REPO, 'issue': 520, 'is_pr': True,
           'commenter': 'chatgpt-codex-connector[bot]', 'body': '<!-- codex-review-state ... -->'}


class Sys:
    def __init__(self, active=False):
        self.active, self.calls = active, []

    def __call__(self, argv, **k):
        self.calls.append(argv)

        class R:
            returncode, stderr = 0, ''
            stdout = 'active\n' if (self.active and 'is-active' in argv) else 'inactive\n'
        return R()


def test_a_codex_review_starts_mn_workflow_and_queues_the_minbot_check():
    append({'event': 'pull_request', 'repo': E.REPO, 'pr': 1})  # before the reader starts: not replayed
    E.new_records()  # the first read starts at the end
    append(REVIEW, COMMENT, {'event': 'pull_request_review', 'repo': 'other/repo', 'pr': 9, 'reviewer': 'x'})
    queued = []
    sysd = Sys()
    ev = E.Events(run=sysd, enqueue=lambda argv, by='': queued.append(argv[1:]))
    lines = ev.poll()
    assert ['systemctl', '--user', 'start', '--no-block', E.MN_UNIT] in sysd.calls
    assert queued == [['--minbot', '514'], ['--minbot', '520']]
    assert lines[0] == ('pull_request_review on platform#514 by chatgpt-codex-connector[bot]: '
                        f'started {E.MN_UNIT}')
    assert ev.poll() == [] and not E.pending()  # the offset moved past them


def test_a_running_pass_is_started_again_when_it_ends():
    E.new_records()
    append({'event': 'pull_request_review', 'repo': E.REPO, 'pr': 7, 'reviewer': 'tangentstorm'})
    sysd = Sys(active=True)
    ev = E.Events(run=sysd, enqueue=lambda *a, **k: None)
    assert ev.poll()[0].endswith('is running; it starts again when this pass ends')
    assert ev.mn_pending and not any('start' in c for c in sysd.calls if c[2] == 'start')
    sysd.active = False
    assert ev.poll() == ['pull_request_review on platform#7 by tangentstorm: started mn-workflow.service']
    assert ev.mn_pending is None


def test_a_human_review_starts_mn_workflow_but_no_minbot_check():
    assert [k for k, _, _ in E.actions({'event': 'pull_request_review', 'repo': E.REPO, 'pr': 3,
                                        'reviewer': 'tangentstorm'})] == ['mn']
    assert E.actions({'event': 'issue_comment', 'repo': E.REPO, 'issue': 3, 'is_pr': True,
                      'commenter': 'tangentstorm'}) == []


def test_a_half_written_line_waits_and_a_rotated_file_is_read_from_the_start():
    E.new_records()
    with open(E.EVENTS, 'a') as f:
        f.write(json.dumps(REVIEW)[:20])  # no newline yet
    assert E.new_records() == []
    with open(E.EVENTS, 'a') as f:
        f.write(json.dumps(REVIEW)[20:] + '\n')
    assert E.new_records() == [REVIEW]
    E.EVENTS.rename(E.EVENTS.with_name('old.jsonl'))  # rotated: a new file (another inode)
    E.EVENTS.write_text(json.dumps(COMMENT) + '\n' + json.dumps(REVIEW) + '\n')
    assert E.new_records() == [COMMENT, REVIEW]
