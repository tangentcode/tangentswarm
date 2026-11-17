"""Meth Lab (tangentswarm #115): the swarm flow against a fake GitHub, the replay of recorded
deliveries, and the guard of the live sandbox settings. No network: every GitHub read and
write goes to methlab.FakeGitHub, and an unknown one is an error."""
import json
import subprocess
import urllib.request

import pytest

from tangentswarm import board, eventdb, methlab, sanity, webhook as W

P6 = 'tangentcode/tangentswarm'
PLATFORM = W.REPO


@pytest.fixture(autouse=True)
def real_drafts(monkeypatch):
    """conftest stubs make_draft; here the real one runs, and its gh call goes to the fake."""
    monkeypatch.setattr(board, 'make_draft', board.REAL_MAKE_DRAFT)


def lab(repo=P6, worker=True):
    return methlab.FakeGitHub(repo, worker=worker)


def step(fake, s):
    return [a.kind for a in fake.deliver(s)]


# -- the full flow on a fake board ----------------------------------------------------------

def test_a_project_6_pr_walks_the_whole_flow_from_open_to_done():
    f = lab()
    assert step(f, f.open_issue(115)) == ['card']
    assert f.col(115) == board.TRIAGE
    assert step(f, f.open_pr(125, closes=[115])) == ['card']
    assert f.col(125) == board.IN_PROGRESS
    assert step(f, f.push(125)) == ['skip']                       # a draft in work: the Stop hook's
    assert step(f, f.ready(125)) == ['handoff']
    head1 = f.pr(125)['headRefOid']
    assert f.col(125) == board.AUTO_REVIEW and f.asks(125) == [head1]
    assert step(f, f.codex_review(125, findings=True)) == ['codex']
    assert f.col(125) == board.IN_PROGRESS and f.pr(125)['isDraft']
    assert 'ROOT-CAUSE RULE' in f.relays[-1][1] and f.relays[-1][0] == 125
    assert step(f, f.push(125)) == ['skip']                       # the fix, pushed as a draft
    f.resolve_threads(125)
    assert step(f, f.ready(125)) == ['handoff']
    head2 = f.pr(125)['headRefOid']
    assert f.col(125) == board.AUTO_REVIEW and f.asks(125) == [head1, head2]
    assert step(f, f.codex_review(125)) == ['codex']
    assert ['swarm-advance', '--minbot', '125', '--repo', P6] in f.queued
    assert any(q[:2] == ['swarm-advance', '--promote-board'] for q in f.queued)
    assert step(f, f.comment(125, f'MERGE-READY {head2}')) == ['p6merge']
    assert f.pr(125)['state'] == 'MERGED' and f.merges[-1] == (P6, 125, head2, 'comment')
    assert step(f, f.merge(125)) == ['merged', 'p6cleanup', 'deploy']
    assert f.deploys == [(f.pr(125)['mergeCommit'], 125)]
    assert f.col(125) == board.DONE and f.col(115) == board.DONE
    assert f.issues[(P6, 115)]['state'] == 'CLOSED'
    assert f.errors() == [] and f.wakes == []


def test_a_merge_ready_for_an_old_head_does_not_merge():
    f = lab()
    f.deliver(f.open_pr(7, draft=False))
    old = f.pr(7)['headRefOid']
    f.deliver(f.push(7))
    assert step(f, f.comment(7, f'MERGE-READY {old}')) == ['p6merge']
    assert f.pr(7)['state'] == 'OPEN' and 'not merged' in f.history[-1]['notes'][-1]


def test_platform_ci_red_goes_back_to_the_worker_and_green_in_auto_review_runs_minbot():
    f = lab(PLATFORM)
    f.deliver(f.open_pr(540, draft=False))
    assert f.col(540) == board.AUTO_REVIEW
    assert step(f, f.ci(540, 'failure')) == ['ci']
    assert f.col(540) == board.IN_PROGRESS and 'CI is not green' in f.relays[-1][1]
    f.deliver(f.push(540))  # a draft now: left to the Stop hook
    f.deliver(f.ready(540))
    assert f.col(540) == board.AUTO_REVIEW
    assert step(f, f.ci(540, 'success')) == ['ci']
    assert f.queued[-1] == ['swarm-advance', '--minbot', '540']
    assert f.errors() == []


def test_michal_sends_a_pr_back_from_human_review_with_his_comment():
    f = lab()
    f.deliver(f.open_pr(9, draft=False))
    f.cards[(P6, 9)]['col'] = board.HUMAN_REVIEW
    f.deliver(f.comment(9, 'please rename it'))                   # log only on Project 6
    assert step(f, f.move(9, board.IN_PROGRESS)) == ['cascade', 'reject']
    assert f.pr(9)['isDraft'] and 'please rename it' in f.relays[-1][1]
    assert f.errors() == []


def test_a_card_move_by_a_bot_is_not_michals_decision():
    f = lab()
    f.deliver(f.open_pr(9, draft=False))
    f.cards[(P6, 9)]['col'] = board.HUMAN_REVIEW
    assert step(f, f.move(9, board.IN_PROGRESS, by='tangentcode-minbot[bot]')) == ['skip']
    assert f.relays == []


def test_codex_findings_with_no_live_worker_never_wake_on_project_6_but_wake_on_platform():
    f = lab(worker=False)
    f.deliver(f.open_pr(3, draft=False))
    f.deliver(f.codex_review(3, findings=True))
    assert f.wakes == [] and f.col(3) == board.AUTO_REVIEW       # never demoted with no worker
    g = lab(PLATFORM, worker=False)
    g.deliver(g.open_pr(3, draft=False))
    g.deliver(g.codex_review(3, findings=True))
    assert len(g.wakes) == 1 and g.col(3) == board.AUTO_REVIEW


def test_a_comment_on_a_triage_issue_wakes_swarmbot():
    f = lab(PLATFORM)
    f.deliver(f.open_issue(600))
    assert step(f, f.comment(600, 'more detail', user='someone')) == ['issue_comment']
    assert len(f.wakes) == 1 and 'TRIAGE' in f.wakes[0]['why']


def test_a_closed_pr_goes_to_canceled():
    f = lab()
    f.deliver(f.open_pr(4))
    assert step(f, f.close(4)) == ['closed']
    assert f.col(4) == board.CANCELED


def test_an_unknown_github_call_is_an_error_never_a_silent_pass():
    f = lab()
    with pytest.raises(methlab.FakeGitHubError):
        f.gql('query{viewer{login}}')
    with pytest.raises(methlab.FakeGitHubError):
        f.sh(['gh', 'release', 'create', 'v1'])
    # inside a delivery the Executor reports it (and wakes, as live): it shows in errors()
    f.deliver(f.open_pr(5))
    f.gql = lambda q, **v: (_ for _ in ()).throw(methlab.FakeGitHubError('new read'))
    f.deliver(f.ready(5))
    assert any('new read' in e for e in f.errors())


def test_the_fake_never_runs_a_real_command(monkeypatch):
    from tangentswarm import proc, tick
    boom = lambda *a, **k: pytest.fail(f'a real command ran: {a}')  # noqa: E731
    monkeypatch.setattr(tick, 'sh', boom)
    monkeypatch.setattr(proc, 'sh', boom)
    monkeypatch.setattr(subprocess, 'run', boom)
    monkeypatch.setattr(subprocess, 'Popen', boom)
    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    from tangentswarm import deploy
    monkeypatch.setattr(deploy, 'start', boom)  # the merge's deploy goes to the fake
    f = lab()
    f.deliver(f.open_issue(1))
    f.deliver(f.open_pr(8, closes=[1]))
    f.deliver(f.ready(8))
    f.deliver(f.codex_review(8))
    f.deliver(f.merge(8))
    assert f.errors() == [] and f.col(8) == board.DONE and f.col(1) == board.DONE and len(f.deploys) == 1


def test_the_executor_reads_ci_through_the_injected_reader(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda head, repo=None: pytest.fail('the live CI read ran'))
    f = lab()
    f.deliver(f.open_pr(6, draft=False))
    f.deliver(f.ci(6, 'success'))
    assert f.errors() == [] and f.queued[-1][:3] == ['swarm-advance', '--minbot', '6']


def test_the_pr_replay_reads_ci_through_the_injected_reader(monkeypatch):
    from tangentswarm import advance
    monkeypatch.setattr(advance, 'check_runs', lambda head, repo=None: pytest.fail('the live CI read ran'))
    f = lab(PLATFORM)
    f.deliver(f.open_pr(11, draft=False))
    f.ci_runs[f.pr(11)['headRefOid']] = [{'name': 't', 'status': 'completed', 'conclusion': 'failure'}]
    acts, _ = W.replay(11, f.executor())
    assert [a.kind for a in acts] == ['ci']


# -- classify has no side effect in a replay -------------------------------------------------

def test_classify_with_poke_off_never_pokes_the_sanity_check():
    p = {'action': 'edited', 'projects_v2_item': {'project_node_id': 'other'},
         'changes': {'field_value': {'field_node_id': 'f'}}}
    W.classify('projects_v2_item', p, poke=False)
    assert not sanity.poke_path().exists()
    W.classify('projects_v2_item', p)
    assert sanity.poked_at() > 0


# -- the replay of recorded deliveries ------------------------------------------------------

def _record(store, delivery, event, payload, decisions, handled=None, sig_ok=True, reason=None):
    raw = json.dumps(payload).encode()
    store.add_received(delivery, event, payload, {}, raw, sig_ok, reason=reason, decisions=decisions)
    if handled is not None:
        store.add('handled', delivery, event, payload, decisions=handled)


def recorded_db(tmp_path):
    db = tmp_path / 'rec.sqlite'
    s = eventdb.EventStore(db)
    f = lab()
    _, opened = f.open_pr(1)
    _record(s, 'd1', 'pull_request', opened, ['card'], handled=['card'])
    _, push = f.push(1)
    _record(s, 'd2', 'pull_request', push, ['skip'], handled=['handoff'])     # the rules changed since
    _record(s, 'd3', 'push', {'repository': {'full_name': P6}}, ['log-only'], reason='log-only')
    _record(s, 'd4', 'pull_request', opened, [], sig_ok=False, reason='bad signature')
    _record(s, 'd1', 'pull_request', opened, ['card'])                        # a redelivery
    return db


def test_recorded_reads_the_good_deliveries_once_with_the_live_decision(tmp_path):
    got = methlab.recorded(recorded_db(tmp_path))
    assert [(d['delivery'], d['recorded']) for d in got] == [('d1', ['card']), ('d2', ['handoff']),
                                                             ('d3', ['log-only'])]
    assert got[0]['payload']['pull_request']['number'] == 1
    assert [d['delivery'] for d in methlab.recorded(recorded_db(tmp_path / 'x'), limit=1)] == ['d3']


def test_recorded_opens_the_db_read_only(tmp_path):
    db = recorded_db(tmp_path)
    before = db.stat().st_mtime_ns, eventdb.EventStore(db).count()
    methlab.recorded(db)
    assert eventdb.EventStore(db).count() == before[1]


def test_replay_runs_the_current_rules_and_diff_lists_only_the_changes(tmp_path):
    done = methlab.replay(methlab.recorded(recorded_db(tmp_path)))
    assert [d['now'] for d in done] == [['card'], ['skip'], ['log-only']]
    assert [d['delivery'] for d in methlab.diff(done)] == ['d2']
    assert not sanity.poke_path().exists()


def test_replay_reports_a_classify_crash_as_the_live_server_does(monkeypatch):
    monkeypatch.setattr(W, '_classify', lambda e, p: 1 / 0)
    done = methlab.replay([{'delivery': 'x', 'event': 'issues', 'payload': {}, 'recorded': ['card']}])
    assert done[0]['now'] == ['classify failed: ZeroDivisionError']


def test_the_replay_command_exits_1_on_a_change_and_0_when_all_match(tmp_path):
    out = []
    assert methlab.main(['replay', '--db', str(recorded_db(tmp_path))], out=out.append) == 1
    assert out == ["CHANGED 1 x pull_request/synchronize: recorded ['handoff'] now ['skip'] (last: d2)",
                   'METHLAB replay: 3 deliveries, 1 changed']
    out = []
    methlab.main(['replay', '--db', str(recorded_db(tmp_path / 'y')), '--each'], out=out.append)
    assert out[0].startswith('CHANGED d2 pull_request/synchronize')
    db = tmp_path / 'same.sqlite'
    f = lab()
    _record(eventdb.EventStore(db), 'e1', *f.open_pr(2), ['card'], handled=['card'])
    out = []
    assert methlab.main(['replay', '--db', str(db), '--all'], out=out.append) == 0
    assert out == ['same e1 pull_request/opened: recorded [\'card\'] now [\'card\']',
                   'METHLAB replay: 1 deliveries, 0 changed']


# -- the live sandbox guard -----------------------------------------------------------------

def good(**kw):
    return dict(dict(org='methlab-test', repo='methlab-test/sandbox', project_number=1, port=8782,
                     secret_files=['~/.config/tangentswarm/methlab-secret'], state_dir='~/methlab-state'), **kw)


@pytest.mark.parametrize('state_dir', [
    '~/.local/state/swarm-flow',            # the production webhook inbox, seen file, log and DB
    '~/.local/state/swarm-flow/methlab',    # inside it
    '~/.local/state',                       # above it
    '~',                                    # the home directory
    '~/.cache/methlab',                     # the board snapshot and gate queue
    '~/relay',                              # the relay events
    '~/bin',                                # the live swarm tools
    '~/.config/tangentswarm/methlab',       # the private config and secrets
])
def test_a_state_dir_that_overlaps_production_is_refused(state_dir):
    probs = methlab.live_problems(good(state_dir=state_dir))
    assert any('state_dir' in p for p in probs), probs
    with pytest.raises(ValueError):
        methlab.live_env(good(state_dir=state_dir))


def test_the_sandbox_process_has_no_path_outside_its_state_dir(tmp_path_factory):
    """Codex on PR #125: the sandbox wrote the production sanity poke, and could share the
    production inbox. Every module-level path of every tangentswarm module, imported with the
    live-env environment (env -i), is inside state_dir; so are the sanity poke and the DB."""
    import os
    import pwd
    import sys as _sys
    st = tmp_path_factory.mktemp('methlab')  # outside tmp_path, where conftest puts the "production" files
    env = methlab.live_env(good(state_dir=str(st)))
    env['PYTHONPATH'] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = r"""
import importlib, json, os, pkgutil, sys
from pathlib import Path
import tangentswarm
out = []
for m in pkgutil.iter_modules(tangentswarm.__path__):
    if m.ispkg:
        continue
    try:
        mod = importlib.import_module('tangentswarm.' + m.name)
    except Exception:
        continue
    for k, v in vars(mod).items():
        if isinstance(v, (str, Path)) and str(v).startswith('/') and chr(10) not in str(v) and len(str(v)) < 400:
            out.append((m.name, k, str(v)))
from tangentswarm import sanity, eventdb, gatelock, promoted
out += [('sanity', 'poke_path()', str(sanity.poke_path())), ('eventdb', 'PATH', str(eventdb.PATH)),
        ('gatelock', 'QUEUE_DIR', str(gatelock.QUEUE_DIR)), ('promoted', 'BIN', str(promoted.BIN))]
print(json.dumps(out))
"""
    r = subprocess.run([_sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    found = json.loads(r.stdout.splitlines()[-1])
    assert len(found) > 20
    prod = [os.environ['HOME'], pwd.getpwuid(os.getuid()).pw_dir, '/tmp']
    src = env['PYTHONPATH'].rstrip('/') + '/'  # the package's own source files are not state
    bad = [f for f in found if not f[1].startswith('__') and not f[2].startswith(src)
           and not f[2].startswith(str(st.resolve()))
           and any(f[2] == h or f[2].startswith(h.rstrip('/') + '/') for h in prod)]
    assert bad == []
    poke = dict(((m, k), v) for m, k, v in found)[('sanity', 'poke_path()')]
    assert poke.startswith(str(st.resolve()))


def test_safe_live_settings_have_no_problem_and_give_their_own_paths(tmp_path_factory):
    st = tmp_path_factory.mktemp('sandbox') / 'methlab-state'
    assert methlab.live_problems(good(state_dir=str(st))) == []
    env = methlab.live_env(good(state_dir=str(st)))
    assert env['SWARM_WEBHOOK_PORT'] == '8782'
    assert env['SWARM_EVENTS_DB'].endswith('methlab-state/events.sqlite')
    assert '/.local/state/swarm-flow' not in ''.join(env.values())  # never the live state dir
    assert env['HOME'].endswith('methlab-state/home') and env['GH_CONFIG_DIR'].endswith('methlab-state/gh')
    assert env['SWARM_WATCH_STATE'].endswith('methlab-state/watch.json')
    assert env['SWARM_DEPLOY_STATE'].endswith('methlab-state/deploy-state')
    assert env['SWARM_FLOW_LIVE'].endswith('methlab-state/no-flow-live')
    assert env['SWARM_WAKE_URL_FILE'].endswith('no-wake-url')


@pytest.mark.parametrize('change, problem', [
    (dict(org='tangentcode', repo='tangentcode/sandbox'), 'production org'),
    (dict(repo='tangentcode/tangentswarm'), 'production org'),
    (dict(repo='other-org/sandbox'), 'not in the Meth Lab org'),
    (dict(port=W.PORT), 'production webhook port'),
    (dict(codex=True), 'do not connect Codex'),
    (dict(org=''), 'org is not set'),
    (dict(state_dir=''), 'state_dir is not set'),
])
def test_live_settings_that_could_touch_production_are_refused(change, problem, tmp_path_factory):
    change.setdefault('state_dir', str(tmp_path_factory.mktemp('sandbox')))
    probs = methlab.live_problems(good(**change))
    assert any(problem in p for p in probs), probs
    with pytest.raises(ValueError):
        methlab.live_env(good(**change))


def test_the_production_webhook_secret_is_refused(tmp_path, monkeypatch):
    from tangentswarm import config
    secret = tmp_path / 'prod-secret'
    monkeypatch.setattr(config, 'files', lambda *k: [secret] if k == ('webhook', 'secret_files') else [])
    assert any('production webhook secret' in p for p in methlab.live_problems(good(secret_files=[str(secret)])))


def test_the_live_commands_refuse_missing_settings():
    out = []
    assert methlab.main(['live-check'], out=out.append) == 1     # the example config has no [methlab]
    assert out[-1].startswith('METHLAB live settings: ') and 'problem' in out[-1]
    out = []
    assert methlab.main(['live-env'], out=out.append) == 1
    assert 'not safe' in out[0]


def test_a_platform_merge_wakes_swarm_promoted_closes_the_issue_and_runs_the_sweeps():
    f = lab(PLATFORM)
    f.deliver(f.open_issue(700))
    f.deliver(f.open_pr(701, draft=False, closes=[700]))
    assert step(f, f.merge(701)) == ['merged']
    assert f.col(701) == board.DONE and f.col(700) == board.DONE and f.cascades == 1
    assert any('swarm-promoted woken' in n for n in f.history[-1]['notes'])
    assert [c[1][1] for c in f.calls if c[0] == 'sh' and 'active-gate' in c[1][0]] == ['--sweep', '--promote-missing']
    assert f.errors() == []


def test_groups_puts_the_largest_change_first():
    def d(i, ev):
        return {'delivery': f'd{i}', 'event': ev, 'payload': {'action': 'x'}, 'recorded': ['a'], 'now': ['b']}
    got = methlab.groups([d(1, 'issues'), d(2, 'push'), d(3, 'push')])
    assert [(k[0], [x['delivery'] for x in v]) for k, v in got] == [('push/x', ['d2', 'd3']), ('issues/x', ['d1'])]
