"""swarm-stop-hook (tangentswarm #28): a finished, pushed worker PR is marked ready, gets
"@codex review" and moves to Auto Review at once, not at the next 15-minute tick."""
import io
import json
import subprocess

import pytest

from tangentswarm import board, slots, stophook, tick


def run(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def wt(tmp_path, monkeypatch):
    """A worker worktree ~/ver/platform.feat (on branch feat, pushed to a bare origin)."""
    monkeypatch.setattr(slots, 'WORKTREE_PARENT', tmp_path)
    monkeypatch.setattr(tick, 'STATE_DIR', tmp_path / 'tick-state')
    origin = tmp_path / 'origin.git'
    run(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(origin))
    w = tmp_path / 'platform.feat'
    run(tmp_path, 'clone', '-q', str(origin), str(w))
    run(w, 'config', 'user.email', 't@example.com')
    run(w, 'config', 'user.name', 't')
    (w / 'a.txt').write_text('1\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'base')
    run(w, 'push', '-q', 'origin', 'HEAD:main')
    run(w, 'checkout', '-q', '-b', 'feat')
    (w / 'b.txt').write_text('feat\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'feat')
    run(w, 'push', '-q', 'origin', 'feat')
    return w


def pr_node(n, branch, col, draft=True):
    return {'id': f'ITEM{n}', 'fieldValueByName': {'name': col, 'optionId': board.OPTION[col]},
            'content': {'__typename': 'PullRequest', 'number': n, 'state': 'OPEN', 'isDraft': draft,
                        'headRefName': branch, 'repository': {'nameWithOwner': stophook.REPO}}}


class G(dict):
    def __init__(self, head, col=board.IN_PROGRESS, draft=True, codex=None, findings=False, branch='feat'):
        super().__init__()
        from boardfake import answer
        self.moves = []
        comments = [{'author': {'login': 'chatgpt-codex-connector'},
                     'body': f"Reviewed commit {head[:8]}. Didn't find any major issues."}] if codex else []
        threads = [{'isResolved': False, 'isOutdated': False,
                    'comments': {'nodes': [{'author': {'login': 'chatgpt-codex-connector'}}]}}] if findings else []
        full = {'number': 7, 'headRefOid': head, 'isDraft': draft, 'comments': {'nodes': comments},
                'reviewThreads': {'nodes': threads}, 'reviews': {'nodes': []},
                'commits': {'nodes': [{'commit': {'committedDate': '2026-11-15T05:00:00Z'}}]}}
        self.update(gql=lambda q, **v: answer([pr_node(7, branch, col, draft)]),
                    fetch=lambda n, refresh=False: full, move=lambda item, col: self.moves.append((item, col)))


def handoff(wt, g, **kw):
    calls = []
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda cwd, head: kw.get('clean', True),
                        sleep=lambda s: None)
    return line, calls


def test_a_finished_pushed_draft_is_marked_ready_asked_for_codex_and_moved(wt):
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head)
    line, calls = handoff(wt, g)
    assert calls == [['gh', 'pr', 'ready', '7', '-R', stophook.REPO],
                     ['gh', 'pr', 'comment', '7', '-R', stophook.REPO, '--body', '@codex review']]
    assert g.moves == [('ITEM7', 'Auto Review')]
    assert line.endswith(f'#7 head {head[:7]} pushed. Marked the pull request ready, posted "@codex review", '
                         'and moved the card to Auto Review.')
    assert stophook.run(wt, g=g, sh=calls.append) is None and len(calls) == 2  # once per head
    assert tick.State(tick.STATE_DIR / 'state.json').done(f'codex-request:7@{head}')  # swarm-tick will not post again


def test_a_codex_clean_head_runs_the_minbot_check(wt, monkeypatch):
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, codex=True)
    ran = []
    calls = []
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda cwd, head: True, sleep=lambda s: None,
                        minbot=lambda n: ran.append(n) or 'MinBot-pass #7 at abc: pass comment, Auto Review -> Ready')
    assert calls == [['gh', 'pr', 'ready', '7', '-R', stophook.REPO]]  # no "@codex review"
    assert g.moves == [('ITEM7', 'Auto Review')] and ran == [7]
    assert line.endswith(f'#7 head {head[:7]} is already Codex-clean. Marked the pull request ready, and ran the '
                         'MinBot check: MinBot-pass #7 at abc: pass comment, Auto Review -> Ready')


@pytest.mark.parametrize('case, want', [
    ('dirty', 'not finished: uncommitted changes'),
    ('unpushed', 'is not pushed (origin has '),
])
def test_an_unfinished_worktree_is_left_alone(wt, case, want):
    if case == 'dirty':
        (wt / 'b.txt').write_text('changed\n')
    else:
        (wt / 'c.txt').write_text('c\n')
        run(wt, 'add', '.')
        run(wt, 'commit', '-qm', 'more')
    line, calls = handoff(wt, G(run(wt, 'rev-parse', 'HEAD')))
    assert want in line and calls == []


def test_open_codex_findings_and_a_main_conflict_are_left_to_the_tick(wt):
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, findings=True)
    g['fetch'](7)['reviews']['nodes'].append({'author': {'login': 'chatgpt-codex-connector'},
                                              'commit': {'oid': head}, 'comments': {'totalCount': 1}})
    line, calls = handoff(wt, g)
    assert 'Codex findings are open on this head' in line and calls == [] and g.moves == []
    (wt / 'c.txt').write_text('c\n')
    run(wt, 'add', '.')
    run(wt, 'commit', '-qm', 'next')
    run(wt, 'push', '-q', 'origin', 'feat')
    head = run(wt, 'rev-parse', 'HEAD')
    line, calls = handoff(wt, G(head), clean=False)
    assert 'does not merge cleanly onto origin/main' in line and calls == []


def test_a_card_outside_in_progress_and_auto_review_is_not_touched(wt):
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, col=board.READY, draft=False)
    line, calls = handoff(wt, g)
    assert 'the card is in Ready: nothing to do' in line and calls == [] and g.moves == []


def test_a_real_merge_check_against_origin_main(wt):
    head = run(wt, 'rev-parse', 'HEAD')
    assert stophook.merges_onto_main(wt, head)
    run(wt, 'checkout', '-q', 'main')
    (wt / 'b.txt').write_text('main\n')
    run(wt, 'add', '.')
    run(wt, 'commit', '-qm', 'clash')
    run(wt, 'push', '-q', 'origin', 'HEAD:main')
    run(wt, 'checkout', '-q', 'feat')
    assert not stophook.merges_onto_main(wt, head)


def test_the_hook_only_queues_a_spool_request(wt, tmp_path):
    from tangentswarm import spool
    rc = stophook.hook(io.StringIO(json.dumps({'cwd': str(wt), 'transcript_path': '/t.jsonl'})))
    reqs = list(spool.SPOOL.glob('*.req.json'))
    assert rc == 0 and len(reqs) == 1
    assert json.loads(reqs[0].read_text())['op'] == 'stophook'
    assert json.loads(reqs[0].read_text())['transcript'] == '/t.jsonl'
    assert stophook.hook(io.StringIO(json.dumps({'cwd': str(tmp_path)}))) == 0
    assert len(list(spool.SPOOL.glob('*.req.json'))) == 1  # not a worker worktree: nothing queued


def test_install_adds_the_hook_once_and_keeps_other_keys(tmp_path):
    w = tmp_path / 'platform.x'
    (w / '.claude').mkdir(parents=True)
    (w / '.claude' / 'settings.local.json').write_text('{"disabledMcpjsonServers": ["j-mcp"]}')
    assert stophook.install(w, command='/bin/swarm-stop-hook') == 'added stop hook'
    assert stophook.install(w, command='/bin/swarm-stop-hook') == 'present'
    d = json.loads((w / '.claude' / 'settings.local.json').read_text())
    assert d['disabledMcpjsonServers'] == ['j-mcp']
    assert d['hooks']['Stop'] == [{'hooks': [{'type': 'command', 'command': '/bin/swarm-stop-hook'}]}]
    (w / '.claude' / 'settings.local.json').write_text('not json')
    assert stophook.install(w, command='/bin/x').startswith('not changed')


def transcript(tmp_path, text):
    p = tmp_path / 't.jsonl'
    p.write_text('\n'.join(json.dumps(d) for d in [
        {'type': 'user', 'message': {'role': 'user', 'content': 'go'}},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
    ]) + '\n')
    return p


def test_a_question_moves_an_unfinished_card_to_needs_input(wt, tmp_path):
    (wt / 'b.txt').write_text('half done\n')  # not finished
    g = G(run(wt, 'rev-parse', 'HEAD'))
    g['OPT'] = {'In Progress': 'x', 'Auto Review': 'y'}  # hr-gate has no Needs Input name
    g['project_id'], g['STATUS_FIELD'] = (lambda: 'PROJ'), 'FIELD'
    calls = []
    t = transcript(tmp_path, 'Two options here.\n\nShould the badge use the old colors or the new ones?')
    line = stophook.run(wt, g=g, sh=calls.append, transcript=str(t))
    assert calls[0] == ['gh', 'project', 'item-edit', '--id', 'ITEM7', '--project-id', 'PROJ', '--field-id', 'FIELD',
                        '--single-select-option-id', board.OPTION[board.NEEDS_INPUT]]
    assert calls[1][:4] == ['gh', 'issue', 'comment', '7'] and stophook.QUESTION_MARK in calls[1][-1]
    assert 'stopped with a question' in line and line.endswith('not marked ready.')
    again = stophook.run(wt, g=g, sh=calls.append, transcript=str(t))
    assert 'already handed to Michal' in again and len(calls) == 2  # once per question


def test_no_question_means_no_needs_input(wt, tmp_path):
    (wt / 'b.txt').write_text('half done\n')
    calls = []
    t = transcript(tmp_path, 'Done with step 1. Running the tests next.')
    assert stophook.run(wt, g=G('x' * 40), sh=calls.append, transcript=str(t)).endswith('uncommitted changes')
    assert calls == [] and stophook.last_question(str(t)) is None
    assert stophook.last_question(str(transcript(tmp_path, 'Which one do you want?**'))) is not None


def test_a_push_after_codex_findings_is_handed_off_for_a_new_review(wt):
    """MethBot 2026-11-15 3:05 AM ET: #518, #524 and #525 pushed a fix for a Codex finding but
    did not resolve the thread; the hook said "left to mn-workflow", which planned nothing."""
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, findings=True)  # an open Codex thread, and no Codex result on this head yet
    calls = []
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda c, h: True, sleep=lambda s: None)
    assert calls == [['gh', 'pr', 'ready', '7', '-R', stophook.REPO],
                     ['gh', 'pr', 'comment', '7', '-R', stophook.REPO, '--body', '@codex review']]
    assert g.moves == [('ITEM7', 'Auto Review')] and 'Codex re-reviews this head' in line


def test_findings_on_a_head_codex_already_reviewed_stay_with_mn_workflow(wt):
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, findings=True)
    g['fetch'](7)['reviews']['nodes'].append({'author': {'login': 'chatgpt-codex-connector'},
                                              'commit': {'oid': head}, 'comments': {'totalCount': 1}})
    calls = []
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda c, h: True, sleep=lambda s: None)
    assert calls == [] and g.moves == [] and 'left to mn-workflow' in line


def test_install_allows_the_linked_node_modules(tmp_path):
    """MethBot: workers stopped at a permission prompt for node_modules, a symlink to the
    main checkout outside the worktree."""
    main_nm = tmp_path / 'platform' / 'node_modules'
    main_nm.mkdir(parents=True)
    w = tmp_path / 'platform.x'
    w.mkdir()
    (w / 'node_modules').symlink_to(main_nm)
    (w / 'inside').mkdir()
    (w / 'local-link').symlink_to(w / 'inside')  # inside the worktree: not added
    assert stophook.install(w, command='/bin/h').startswith('added stop hook; allowed ')
    d = json.loads((w / '.claude' / 'settings.local.json').read_text())
    assert d['permissions']['additionalDirectories'] == [str(main_nm.resolve())]
    assert stophook.install(w, command='/bin/h') == 'present'


def test_no_handoff_while_a_dev_server_runs_in_the_worktree(wt):
    """MethBot 2026-11-15 4:31 AM ET: the worker must stop its dev servers before the handoff."""
    head = run(wt, 'rev-parse', 'HEAD')
    g, calls, told = G(head), [], []
    left = [[(4242, 'python tcode-server.py --port 5391')]]
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda c, h: True, sleep=lambda s: None,
                        procs=lambda cwd: left[0], tell=lambda n, text: told.append((n, text)))
    assert calls == [] and g.moves == [] and 'asked the worker to stop them; no handoff yet' in line
    assert told[0][0] == 7 and 'pid 4242 (python tcode-server.py --port 5391)' in told[0][1]
    left[0] = []  # the worker stopped it; its next Stop hands off
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda c, h: True, sleep=lambda s: None,
                        procs=lambda cwd: left[0], tell=lambda n, text: told.append((n, text)))
    assert 'moved the card to Auto Review' in line and len(told) == 1


def test_a_question_with_a_clean_pushed_head_goes_to_needs_input_never_ready(wt, tmp_path):
    """platform #541 (2026-11-15): step 1 done, clean pushed head, a question for Michal. The
    hook marked it ready, asked Codex, and the MinBot pass moved it to Ready."""
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, codex=True)  # even Codex-clean
    g['OPT'] = {'In Progress': 'x'}
    g['project_id'], g['STATUS_FIELD'] = (lambda: 'PROJ'), 'FIELD'
    calls, ran = [], []
    t = transcript(tmp_path, 'Step 1 is done and pushed (partial scope). Step 2 waits for your decision.')
    line = stophook.run(wt, g=g, sh=calls.append, merges=lambda c, h: True, sleep=lambda s: None,
                        minbot=lambda n: ran.append(n), transcript=str(t))
    assert 'Moved the card to Needs Input' in line and ran == []
    assert not any(c[:3] == ['gh', 'pr', 'ready'] or '@codex review' in c for c in calls)
    assert calls[0][-1] == board.OPTION[board.NEEDS_INPUT]


def test_two_hook_runs_for_one_worktree_never_overlap(wt, monkeypatch):
    import threading
    import time as T
    inside, overlap = [], []

    def slow(*a, **k):
        if inside:
            overlap.append(1)
        inside.append(1)
        T.sleep(0.2)
        inside.pop()
    monkeypatch.setattr(stophook, '_run', slow)
    ts = [threading.Thread(target=stophook.run, args=(wt,)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert overlap == []


def test_a_pending_needs_input_report_holds_the_handoff(wt):
    # Codex on PR #54: the Stop hook ran before the queued report and a Codex-clean head went to Ready
    from tangentswarm import agent_report
    head = run(wt, 'rev-parse', 'HEAD')
    g = G(head, codex=True)
    ran, calls = [], []

    def go():
        return stophook.run(wt, g=g, sh=calls.append, merges=lambda cwd, head: True, sleep=lambda s: None,
                            minbot=lambda n: ran.append(n) or 'pass')
    agent_report.accept('needs-input', 7, 'mn-7-feat', question='Which?', submit=lambda *a, **k: {'queued': 'x'})
    line = go()
    assert 'reported needs-input or blocked' in line and calls == [] and g.moves == [] and ran == []
    agent_report.log('needs-input handled', 7, 'mn-7-feat')
    go()  # not marked seen: once the report is handled, the next Stop hands off
    assert ran == [7]
