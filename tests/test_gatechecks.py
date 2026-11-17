"""gatechecks: screenshots must render (7a); entry needs staging to serve the head (6)."""
import urllib.error

import pytest

from tangentswarm import gatechecks as G

REPO = 'owner/repo'
ATTACH = 'https://github.com/user-attachments/assets/0c5b998d-7a0c-4e80-bc87-a74779e4169f'
BLOB = f'https://github.com/{REPO}/blob/swarm-shots/shots/pr1-a.png?raw=true'
RAW = f'https://raw.githubusercontent.com/{REPO}/swarm-shots/shots/pr1-b.png'


def never(*a):
    raise AssertionError('must not be called')


def test_image_urls_reads_markdown_and_img_tags_in_order():
    body = f'x ![a]({ATTACH}) <img width=3 src="{RAW}"> ![b](<{BLOB}> "t") ![a]({ATTACH})'
    assert G.image_urls(body) == [ATTACH, RAW, BLOB]


def test_a_relative_name_fails_like_platform_479():
    ok, why = G.check_images('![x](pr479-attr-gui.png)', REPO, exists=never, probe=never)
    assert not ok and 'not an absolute https URL' in why


def test_no_image_fails():
    assert G.check_images('just text, a .png word', REPO, exists=never, probe=never) == (False, 'no image in body')


def test_user_attachments_pass_on_form_without_a_fetch():
    assert G.check_images(f'![a]({ATTACH})', REPO, exists=never, probe=never)[0]
    ok, why = G.check_images('![a](https://github.com/user-attachments/assets/x)', REPO,
                             exists=never, probe=lambda u: (False, 'probe: 404'))
    assert not ok and 'probe: 404' in why        # not the GitHub-issued form: it is probed


def test_swarm_shots_urls_must_exist_on_the_branch():
    seen = []
    ok, _ = G.check_images(f'![a]({BLOB}) ![b]({RAW})', REPO, exists=lambda p: seen.append(p) or True, probe=never)
    assert ok and seen == ['shots/pr1-a.png', 'shots/pr1-b.png']
    ok, why = G.check_images(f'![a]({BLOB})', REPO, exists=lambda p: False, probe=never)
    assert not ok and 'not on the swarm-shots branch' in why
    assert G.shots_path(f'https://github.com/{REPO}/raw/swarm-shots/s/x.png', REPO) == 's/x.png'
    assert G.shots_path(f'https://github.com/other/repo/raw/swarm-shots/s/x.png', REPO) is None


def test_other_https_hosts_must_load_as_an_image():
    class R:
        def __init__(self, kind, status=200):
            self.headers, self.status = {'Content-Type': kind}, status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    assert G.probe_image('https://x/y.png', opener=lambda req, timeout: R('image/png'))[0]
    ok, why = G.probe_image('https://x/y.png', opener=lambda req, timeout: R('text/html'))
    assert not ok and 'not an image' in why

    def gone(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, 'nf', {}, None)
    ok, why = G.probe_image('https://x/y.png', opener=gone)
    assert not ok and '404' in why


def test_served_check_is_strict_on_entry_and_lenient_on_a_regate():
    web = ['web/a.ts', 'README.md']
    assert G.check_served(['README.md'], True, None, never)[0]
    assert G.check_served(web, True, 'abc12345', lambda s: True)[0]
    ok, why = G.check_served(web, True, 'abc12345', lambda s: False)
    assert not ok and 'LACKS the head' in why
    ok, why = G.check_served(web, False, 'abc12345', lambda s: False)
    assert ok and why.startswith('lenient re-gate')
    assert not G.check_served(['api/x.py'], True, None, never, err='cannot read')[0]
    assert G.check_served(['app/x'], False, None, never, err='cannot read')[0]


def test_served_accepts_the_vn_tip_condition_9_picked():
    """platform #447: work 95f38a27 has ptw/cursor-item-end-v2 06347b83, not head 0a8b0f7a."""
    web = ['web/ptw/cursor.ts']
    inside = {('06347b83', '95f38a27')}
    contains = lambda anc, desc: (anc, desc) in inside
    ok, why = G.check_served(web, True, '95f38a27', lambda s: False,
                             tips=[('ptw/cursor-item-end-v2', '06347b83')], contains=contains)
    assert ok and 'contains ptw/cursor-item-end-v2 06347b83' in why
    ok, why = G.check_served(web, True, '95f38a27', lambda s: False,
                             tips=[('ptw/cursor-item-end-v1', 'deadbeef')], contains=contains)
    assert not ok and 'LACKS the head' in why


def test_a_broken_work_branch_fails_entry():
    files = ['web/js/ptw/ChessTrackParser.mts', 'web/js/ptw/MenuTrackParser.mts', 'web/tc/AnimCompiler.mts',
             'web/tc/MnAnimPlugin.playhead.test.mts']
    why = G.work_problem(files)
    assert why == ('origin/work has conflict markers in web/js/ptw/ChessTrackParser.mts, '
                   'web/js/ptw/MenuTrackParser.mts, web/tc/AnimCompiler.mts and 1 more')
    failed = {'sha': '0d249b69d06d', 'status': 'completed', 'conclusion': 'failure'}
    assert G.work_problem([], failed) == 'the latest work Redeploy run failed (work 0d249b69)'
    assert G.work_problem([], dict(failed, conclusion='skipped')) is None
    assert G.work_problem([], dict(failed, conclusion='success')) is None
    assert G.work_problem([], None) is None
    web = ['web/a.ts']
    ok, why = G.check_served(web, True, 'abc12345', lambda s: True, work_bad='origin/work has conflict markers in x')
    assert not ok and why.startswith('origin/work has conflict markers')
    assert G.check_served(web, False, 'abc12345', lambda s: True, work_bad='w')[1] == 'lenient re-gate: w'
    assert G.check_served(['README.md'], True, None, never, work_bad='w')[0]   # no served files


def test_every_staging_site_must_serve_the_head():
    """platform #493: tangentcode and minavo served 37ceb8f4, ptw still served 95f38a27."""
    web = ['web/tc/DbItemList.mts', 'api/tcode/db_bp.py']
    has = lambda s: s != '95f38a27'
    sites = [('work.example.test', '4fe539ad', None), ('minavo.example.test', '4fe539ad', None),
             ('ptw.example.test', '95f38a27', None)]
    ok, why = G.check_served_sites(web, True, sites, has)
    assert not ok
    assert why == ('1 of 3 staging sites do not serve it: ptw.example.test: static .deploy-sha 95f38a27 '
                   'LACKS the head: staging does not serve it yet')
    ok, why = G.check_served_sites(web, False, sites, has)
    assert ok and why.startswith('lenient re-gate: 1 of 3 staging sites') and 'ptw.example.test' in why
    ok, why = G.check_served_sites(web, True, sites[:2], has)
    assert ok and why.startswith('all 2 staging sites serve it: work.example.test: static .deploy-sha 4fe539ad')


def test_every_site_accepts_the_vn_tip_and_an_unreadable_site_fails_entry():
    web = ['web/ptw/cursor.ts']
    sites = [('a', 'aaaaaaaa', None), ('b', 'bbbbbbbb', None)]
    contains = lambda tip, desc: (tip, desc) in {('06347b83', 'aaaaaaaa'), ('06347b83', 'bbbbbbbb')}
    assert G.check_served_sites(web, True, sites, lambda s: False, tips=[('x-v2', '06347b83')], contains=contains)[0]
    ok, why = G.check_served_sites(web, True, sites + [('c', None, 'cannot read /x')], lambda s: True)
    assert not ok and 'c: cannot read the static .deploy-sha (cannot read /x)' in why
    assert G.check_served_sites(['README.md'], True, [], never)[0]
    assert not G.check_served_sites(web, True, [], never)[0]
    assert G.check_served_sites(web, True, sites, never, work_bad='origin/work has markers') == \
        (False, 'origin/work has markers')


def pass_comment(body, login='tangentcode-minbot'):
    return {'author': {'login': login}, 'body': body}


def test_minbot_pass_comment_must_name_the_exact_head():
    head = '39144fac99549b3b2e7e10db7d945da62d342648'
    old = pass_comment('### MinBot pass\n\nCommit: `49cf308210a01c8161614d55485f12cf8ca9a5ff`\n\nCodex clean')
    new = pass_comment(f'### MinBot pass\n\nCommit: `{head}`\n\nCodex clean on 39144fac99')
    assert G.minbot_pass([old], [('head', head)]) == (False, 'no MinBot pass comment names head 39144fac')
    assert G.minbot_pass([old, new], [('head', head)]) == (True, 'MinBot pass comment names head 39144fac')
    short = pass_comment('### MinBot pass\n\nCommit: `b1849519fd`\n\nExact-head pass on b1849519fd')
    assert G.minbot_pass([short], [('head', 'b1849519fd' + '0' * 30)])[0]
    assert G.minbot_pass([pass_comment('### MinBot pass on 39144fac99')], [('head', head)])[0]


def test_minbot_pass_accepts_the_vn_tip_but_not_other_authors_or_titles():
    tip, head = '06347b83' + 'a' * 32, '0a8b0f7a' + 'b' * 32
    c = pass_comment(f'### MinBot pass\n\nCommit: `{tip}`')
    assert not G.minbot_pass([c], [('head', head)])[0]
    assert G.minbot_pass([c], [('head', head), ('ptw/x-v2', tip)]) == (True, 'MinBot pass comment names ptw/x-v2 06347b83')
    assert G.minbot_pass([pass_comment(f'### MinBot pass\n\nCommit: `{head}`', login='tangentcode-minbot[bot]')],
                         [('head', head)])[0]
    assert not G.minbot_pass([pass_comment(f'### MinBot pass\n\nCommit: `{head}`', login='someone')], [('head', head)])[0]
    assert not G.minbot_pass([pass_comment(f'### Evidence\n\nCommit: `{head}`')], [('head', head)])[0]
    assert not G.minbot_pass([pass_comment('### MinBot pass\n\nCommit: `0a8b0f7`')], [('head', head)])[0]  # 7 chars


def test_the_brief_example_image_is_not_screenshot_evidence():
    """Codex on PR #26: the brief's SHOTS_RULE example made every launched UI PR look done."""
    from tangentswarm import brief, slots
    from tangentswarm.gatechecks import SHOTS_RULE, image_urls
    body = 'Fixes #5\n\n' + brief.section('Task #5\n' + SHOTS_RULE)
    assert '](' in SHOTS_RULE and image_urls(body) == []
    assert image_urls(body + '\n![after](https://example.test/a.png)') == ['https://example.test/a.png']


# --- tangentswarm #130: the PR body never says where the PR stands -------------------

PR528 = 'Not on staging yet: work.pawntakesworld.com serves a60245158, which does not have this branch.'


@pytest.mark.parametrize('text', [
    PR528,
    'Merged into work at 3:10 PM.',
    'It is live on staging now.',
    'The fix is deployed to staging.',
    'This is not in work yet.',
    'ptw.example.com serves `abcdef12`.',
    'Staging is serving commit 0123abcd.',
    'Commit 0123abcd is deployed.',
    'The static .deploy-sha is a6024515.',
    'The deploy SHA is still `0123abcd`.',
    'The .deploy-sha lacks the head.',
    'Work does not contain this branch.',
])
def test_status_text_is_found(text):
    assert G.is_status(text)


@pytest.mark.parametrize('text', [
    'Check it on staging: open https://work.pawntakesworld.com/lobby and click Add.',
    'Where to see: /lobby on staging, then press Enter.',
    'Served on staging at /lobby.',
    'Once merged into work, open /lobby.',
    'After it is deployed to staging, click Save.',
    'This PR changes the work queue.',
    'A branch that is not merged is skipped.',
    # Codex on PR #133: a mention of the marker, or a sentence that describes how things work
    'Where-to-see: open https://work.example.com/.deploy-sha and verify it is plain text.',
    'This PR changes how the deploy SHA is generated.',
    'This PR changes how code is deployed to staging.',
    'A branch that is merged into work is skipped.',
    'The gate checks whether the PR is on staging.',
])
def test_how_to_see_text_is_not_status(text):
    assert not G.is_status(text)


def test_strip_status_keeps_bullets_fences_and_the_brief():
    brief = '<!-- swarm-brief:start -->\nNot on staging yet.\n<!-- swarm-brief:end -->\n'
    body = ('Fixes #1\n\n- First. Not on staging yet.\n- Merged into work.\n\n```\nserves abcdef12\n```\n'
            'Check it on staging: open /x.\n' + brief)
    new, gone = G.strip_status(body)
    assert gone == ['Not on staging yet.', 'Merged into work.']
    assert new == 'Fixes #1\n\n- First.\n\n```\nserves abcdef12\n```\nCheck it on staging: open /x.\n' + brief
    assert G.strip_status(new) == (new, [])
    assert G.status_lines(brief) == []


def test_body_problem_flags_status_text():
    why = G.body_problem(['api/x.py'], 'Where to see: /x on staging.\n' + PR528)
    assert why and '#130' in why
    assert G.body_problem(['api/x.py'], 'Where to see: /x on staging.') is None


def test_scrub_status_edits_the_body_and_opens_one_incident():
    patched, tickets = [], []
    ok, why = G.scrub_status(528, 'Fixes #1\n' + PR528 + '\nCheck it on staging: /x.\n', REPO, head='h' * 40,
                             patch=lambda n, b: patched.append((n, b)), incident=lambda *a, **k: tickets.append(a))
    assert ok and 'removed 1' in why
    assert patched == [(528, 'Fixes #1\nCheck it on staging: /x.\n')]
    assert len(tickets) == 1 and tickets[0][0] == f'pr-status:{REPO}#528@' + 'h' * 12


def test_scrub_status_leaves_a_clean_body_alone_and_dry_run_changes_nothing():
    never = lambda *a, **k: pytest.fail('no edit or ticket')  # noqa: E731
    assert G.scrub_status(1, 'Check it on staging: /x.', REPO, patch=never, incident=never) == (True, '')
    ok, why = G.scrub_status(1, PR528, REPO, dry=True, patch=never, incident=never)
    assert ok and why.startswith('remove 1 status')


def test_scrub_status_fails_when_the_edit_fails_and_a_failed_ticket_does_not():
    def boom(*a, **k):
        raise RuntimeError('gh down')
    ok, why = G.scrub_status(1, PR528, REPO, patch=boom, incident=lambda *a, **k: pytest.fail('no ticket'))
    assert not ok and 'edit failed' in why
    assert G.scrub_status(1, PR528, REPO, patch=lambda n, b: None, incident=boom)[0]


def test_every_worker_instruction_carries_the_status_rule():
    from tangentswarm import flow, launcher, launches
    assert G.STATUS_RULE.isascii() and '\n' not in G.STATUS_RULE
    for text in (launches.brief_line('/b/1.txt'), launcher.brief_text(1, 't', 'b', 2), flow.shots_text(7)):
        assert G.STATUS_RULE in text


FOUR = '`' * 4
THREE = '`' * 3


@pytest.mark.parametrize('body', [
    # Codex on PR #133: an inner fence of another length or character does not close the outer one
    f'{FOUR}markdown\n{THREE}python\nx = 1\n{THREE}\nMerged into work.\n{FOUR}\n',
    f'~~~\n{THREE}\nMerged into work.\n~~~\n',
    f'{THREE}\n~~~\nMerged into work.\n{THREE}\n',
    f'{THREE}\n{THREE} not a close\nMerged into work.\n{THREE}\n',
    f'- step one\n  {THREE}\n  Merged into work.\n  {THREE}\n',
])
def test_text_inside_a_fence_is_kept(body):
    assert G.strip_status(body) == (body, [])


def test_text_after_a_closed_fence_is_scrubbed():
    body = f'{FOUR}\nx\n{FOUR}\nMerged into work.\n{THREE}js\ny\n{THREE}\nNot on staging yet.\n'
    assert G.strip_status(body)[1] == ['Merged into work.', 'Not on staging yet.']


@pytest.mark.parametrize('text', [
    # Codex on PR #133: the condition exemption covers served-commit matches too
    'Where-to-see: once .deploy-sha contains the head, open /lobby.',
    'If staging serves 0123abcd or newer, open /lobby.',
    'When the deploy SHA is 0123abcd, reload /lobby.',
])
def test_a_condition_before_a_served_commit_is_kept(text):
    assert not G.is_status(text) and G.strip_status(text) == (text, [])


@pytest.mark.parametrize('body, new, gone', [
    # Codex on PR #133: a sentence wrapped over soft line breaks is one sentence
    ('Where-to-see: once the PR is\nmerged into work, open /lobby.\n',
     'Where-to-see: once the PR is\nmerged into work, open /lobby.\n', []),
    ('Intro.\nThe fix is deployed\nto staging.\nCheck it on staging: /x.\n',
     'Intro.\nCheck it on staging: /x.\n', ['The fix is deployed to staging.']),
    ('- First. The fix is deployed\n  to staging.\n- Next.\n', '- First.\n- Next.\n',
     ['The fix is deployed to staging.']),
    # a line break before a capital letter still ends a sentence, so "Fixes #1" stays
    ('Fixes #1\nNot on staging yet.\nCheck it on staging: /x.\n', 'Fixes #1\nCheck it on staging: /x.\n',
     ['Not on staging yet.']),
    # a heading or a new list item never joins the line before it
    ('## Notes\nMerged into work.\n', '## Notes\n', ['Merged into work.']),
    ('- is it\n- merged into work. Yes.\n', '- is it\n- Yes.\n', ['merged into work.']),
])
def test_soft_line_breaks(body, new, gone):
    assert G.strip_status(body) == (new, gone)


@pytest.mark.parametrize('body, new, gone', [
    # Codex on PR #133: a sentence may end inside closing inline marks
    ('**Not on staging yet.** Where-to-see: open /lobby.', 'Where-to-see: open /lobby.', ['**Not on staging yet.**']),
    ('_Merged into work._ Check it on staging: /x.', 'Check it on staging: /x.', ['_Merged into work._']),
    ('(Merged into work.) Check it on staging: /x.', 'Check it on staging: /x.', ['(Merged into work.)']),
    ('"Not on staging yet." Check it on staging: /x.', 'Check it on staging: /x.', ['"Not on staging yet."']),
    ('Intro. ~~Merged into work.~~ Check it on staging: /x.', 'Intro. Check it on staging: /x.',
     ['~~Merged into work.~~']),
])
def test_sentence_ends_after_closing_marks(body, new, gone):
    assert G.strip_status(body) == (new, gone)


@pytest.mark.parametrize('body', [
    # Codex on PR #133: a fence inside a blockquote, at any quote depth
    '> ```text\n> Merged into work.\n> ```\nCheck it on staging: /x.\n',
    '> > ~~~\n> > Not on staging yet.\n> > ~~~\n',
])
def test_a_fence_in_a_blockquote_is_not_read(body):
    assert G.strip_status(body) == (body, []) and G.status_lines(body) == []


@pytest.mark.parametrize('body, left', [
    # a cut here could break the Markdown: the text stays and is reported
    ('> Not on staging yet.\n', ['> Not on staging yet.']),
    ('| site | state |\n| --- | --- |\n| ptw | Merged into work. |\n', ['| ptw | Merged into work.']),
    ('<p>Not on staging yet.</p>\n', ['<p>Not on staging yet.</p>']),
    ('**Intro. Not on staging yet.** Check it on staging: /x.\n', ['Not on staging yet.**']),
    ('`a. Merged into work.` b.\n', ['Merged into work.`']),
])
def test_status_that_cannot_be_cut_safely_is_left_and_reported(body, left):
    assert G.strip_status(body) == (body, [])
    assert G.status_lines(body) == left


def test_scrub_status_with_only_left_text_opens_an_incident_and_does_not_edit():
    tickets = []
    ok, why = G.scrub_status(1, '> Not on staging yet.\n', REPO, patch=lambda *a: pytest.fail('no edit'),
                             incident=lambda *a, **k: tickets.append(k))
    assert ok and why.startswith('Left 1') and 'by hand' in tickets[0]['fix']
