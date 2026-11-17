"""Two hr-gate conditions that let a broken PR into Human Review (platform #479).

Condition 7a (screenshots). hr-gate's IMG_RE took any markdown image, so a bare relative
name such as ![x](pr479-attr-gui.png) passed. It never renders on GitHub. Now every image
in the body must be an absolute https URL that resolves:

  https://github.com/user-attachments/assets/<id>    issued by GitHub's own upload. In a
                                                      private repo only a signed-in reader
                                                      can open it, so the form is checked,
                                                      not fetched (no user token is used).
  swarm-shots branch of the PR's repo, as
    https://github.com/<repo>/blob/swarm-shots/<path>?raw=true
    https://github.com/<repo>/raw/swarm-shots/<path>
    https://raw.githubusercontent.com/<repo>/swarm-shots/<path>
                                                      the file must exist on swarm-shots
                                                      (contents API, app quota)
  any other https URL                                 GET answers 200 with an image/* type

Condition 6 (deployed + served). It was best-effort and always passed. For a PR that
touches served files (web/, app/, api/), ENTRY (into Staging or Human Review, and the
instant promote) now needs the static .deploy-sha to contain the head. A re-gate of a card
already in Human Review (a merge-only re-gate) stays lenient. Entry also fails when
origin/work itself is broken (work_problem): it holds conflict markers, or its latest
Redeploy run failed. Work commit 0d249b69 (#447) reached work with markers at 1:19 AM ET,
its Redeploy failed, and the gate still passed.

Condition 6 checks EVERY staging site (check_served_sites). Each site serves the platform
work branch, so a PR with served files reaches all of them. Platform #493 entered Human
Review while one staging site still served 95f38a27, which did not contain its head
37ceb8f4; the gate had read only the one site that site_for picked.

tangentswarm #116 (Michal 2026-11-17 11:14 AM ET): each PR has ONE priority staging site.
Its milestone picks it ([staging] priority in the config: {milestone word: site key}; the
default is PTW -> ptw), else [staging] priority_default (default ptw). Condition 6 and every
column move wait only for that site. The other sites roll out in the background: their lag
is named in the reason, but it never fails the check.

tangentswarm #130 (Michal 2026-11-17 12:21 PM ET): a PR body never says where the PR stands.
Platform #528 sat in Human Review while its body said "Not on staging yet: work... serves
a60245158, which does not have this branch." Merged or not into work, on staging or not, and
the sha a site serves go stale; the board column is the one place that says them.
status_lines() finds such sentences and body_problem() flags them. Before hr-gate --apply,
check_status() holds the PR, the swarm tells the worker to remove them, and an incident is
opened. The swarm never edits a PR body (Michal via MethBot on PR #133, option A).
"""
import re
import urllib.request

SHOTS_BRANCH = 'swarm-shots'

# The one rule every coder gets (launch line, gate relays). Keep it one line.
SHOTS_RULE = ("Screenshots in the PR body must be absolute https URLs that render on GitHub: drag the "
              "image into a GitHub comment box and paste the github.com/user-attachments/assets/... URL, "
              f"or push it to the {SHOTS_BRANCH} branch and use https://github.com/<owner>/<repo>/blob/"
              f"{SHOTS_BRANCH}/<path>?raw=true. Never a relative file name such as ![x](shot.png); "
              "hr-gate fails it.")
# Methodology #88 / tangentswarm #64: a verification worker wrote into the human's real staging
# tree (methodology #87). Every brief that sends a worker to staging carries this rule.
SCRATCH_RULE = ("A worker that does verification or takes screenshots must use a throwaway user or a "
                "scratch root. Do not sign in as the human, and never write, change, or delete files in "
                "the real staging tree of the human.")
# tangentswarm #130: every worker gets this with SHOTS_RULE. Keep it one line.
STATUS_RULE = ("Never write in the PR body where the PR stands: not whether it is merged into work, not "
               "whether it is on staging, and not which commit a site serves. The board column shows that. "
               "The where-to-see or 'Check it on staging' section says only how to see the change (the URL "
               "and the steps), never whether it is deployed. The swarm removes such text and logs an incident.")
SERVED_RE = re.compile(r'^(web|api|app)/')

_MD = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_TAG = re.compile(r'<img\s[^>]*?src\s*=\s*["\']?([^"\'\s>]+)', re.I)
_ATTACH = re.compile(r'https://github\.com/user-attachments/assets/[0-9a-fA-F-]{16,}/?')


def image_urls(body):
    """Image URLs in a PR body (markdown and <img>), in order, without repeats. The brief
    section is not evidence: its SHOTS_RULE example `![x](shot.png)` made every launched UI
    PR look screenshotted (Codex on PR #26), so it is stripped first, as hr-gate does."""
    from .brief import strip_brief
    body = strip_brief(body)
    found = sorted([(m.start(), m.group(1)) for m in _MD.finditer(body or '')] +
                   [(m.start(), m.group(1)) for m in _TAG.finditer(body or '')])
    out = []
    for _, u in found:
        if u not in out:
            out.append(u)
    return out


def shots_path(url, repo):
    """The file path if url is a swarm-shots URL of repo, else None."""
    r = re.escape(repo)
    b = re.escape(SHOTS_BRANCH)
    for pat in (rf'https://github\.com/{r}/blob/{b}/([^?#]+)\?raw=true$',
                rf'https://github\.com/{r}/raw/{b}/([^?#]+)$',
                rf'https://raw\.githubusercontent\.com/{r}/{b}/([^?#]+)$'):
        m = re.match(pat, url)
        if m:
            return m.group(1)
    return None


def probe_image(url, timeout=15, opener=urllib.request.urlopen):
    """(ok, why) for a public https image URL: GET answers 200 with an image/* content type."""
    req = urllib.request.Request(url, headers={'User-Agent': 'hr-gate', 'Range': 'bytes=0-0'})
    try:
        with opener(req, timeout=timeout) as r:
            kind = (r.headers.get('Content-Type') or '').split(';')[0].strip()
            code = getattr(r, 'status', 200)
    except Exception as e:  # HTTPError, URLError, timeouts
        return False, f"{url[:80]} does not load ({getattr(e, 'code', None) or type(e).__name__})"
    if code not in (200, 206) or not kind.startswith('image/'):
        return False, f"{url[:80]} is not an image ({code} {kind or 'no type'})"
    return True, ''


UI_RE = re.compile(r'^web/|\.(css|html|mts|tsx?|jsx?|svg)$')
NO_SHOTS_PREFIXES = ('api/', 'docs/', '.github/', 'scripts/', 'tests/', 'test/')
CODE_ONLY_RE = re.compile(r'code[- ]only|tooling[- ]only|no deployable surface|nothing (?:to )?deploy', re.I)


WHERE_RE = re.compile(r"where[- ]to[- ]see|check it on staging|served on staging", re.I)  # as hr-gate 7b


def body_problem(paths, body):
    """Why the PR body no longer meets the body conditions (7a: an image when the PR needs
    screenshots; 7b: a where-to-see line), or None. Cheap (no image probe): the promotion
    fast path runs it, because a body edit after the gate pass could remove the evidence
    (Codex on PR #26). The brief section never counts."""
    from .brief import strip_brief
    body = strip_brief(body)
    if needs_screenshots(paths, body) and not image_urls(body):
        return 'no image in body (edited after the gate pass)'
    if not WHERE_RE.search(body):
        return 'no where-to-see line in body (edited after the gate pass)'
    found = status_lines(body)
    if found:
        return f"the body states where the PR stands (tangentswarm #130): {found[0][:120]}"
    return None


# tangentswarm #130: sentences that say where the PR stands. Two kinds:
#   _STATE: deploy or merge state ("not on staging yet", "merged into work", "is live on
#           staging").
#   _SERVES: a site and the commit it serves ("work.example.com serves a6024515",
#           ".deploy-sha is a6024515", "the .deploy-sha lacks the head"). A bare mention of
#           the marker ("open /.deploy-sha") is not status.
# There are no wording exemptions: a false match only costs the worker one rewording.
_W = r'(?:origin/)?(?:work|staging)\b'
_STATE = re.compile(
    r"\bnot\s+(?:yet\s+|currently\s+)?(?:(?:merged|deployed|live|served|pushed|landed)\s+)?(?:on|in|into|to|onto)\s+" + _W
    + r"|\b(?:is|are|was|were|been|now|already|got|went|currently|isn't|aren't|wasn't)\s+"
      r"(?:now\s+|already\s+|also\s+|not\s+|yet\s+|currently\s+)?(?:(?:merged|deployed|live|up|served|pushed|landed|running)\s+)?"
      r"(?:on|in|into|to|onto)\s+" + _W
    + r"|\b(?:merged|deployed|landed|pushed|shipped|rolled\s+out|went\s+live)\s+(?:in|into|to|onto|on)\s+" + _W
    + r"|\b(?:on|in)\s+(?:work|staging)\s+(?:now|yet|already)\b"
    + r"|\b(?:does\s+not|doesn't|do\s+not|don't)\s+(?:yet\s+)?(?:have|contain|include|serve)\s+"
      r"(?:this|my|our)\s+(?:branch|change|changes|pr|head|fix|commit)\b",
    re.I)
_SERVES = re.compile(
    r"\bserv(?:es|ing|ed)\s+(?:commit\s+|sha\s+|head\s+)?`?[0-9a-f]{7,40}\b"
    r"|\b[0-9a-f]{7,40}`?\s+(?:is|was)\s+(?:now\s+)?(?:served|deployed|live)\b"
    r"|(?:\.deploy-sha|\bdeploy[- ]sha)`?\s*(?:(?:is|was|=|:|reads|says|shows)\s*)?(?:(?:now|still)\s+)?"
    r"(?:(?:is|was|=|:|reads|says|shows)\s*)?`?[0-9a-f]{7,40}\b"
    r"|(?:\.deploy-sha|\bdeploy[- ]sha)`?\s+(?:(?:now|still)\s+)?(?:has|lacks|contains|includes|"
    r"does\s+not\s+(?:have|contain|include)|doesn't\s+(?:have|contain|include))\s+"
    r"(?:the|this|my|our)\s+(?:head|branch|change|changes|pr|fix|commit)\b",
    re.I)
_BLOCK = re.compile(r'^\s*(?:[-*+]\s|\d+[.)]\s|#|>|\||<)')  # a line that starts a new Markdown block
_QUOTE = re.compile(r'^\s*(?:>\s?)+')
_FENCE = re.compile(r'^\s*(?:(?:[-*+]|\d+[.)])\s+)?(`{3,}|~{3,})(.*)$')  # list items may indent it
# A sentence ends at . ! or ? (closing inline marks may follow: "**Not on staging yet.**"), or
# at a line break before a capital letter. A line that goes on in lower case ("is deployed\n
# to staging") is the same sentence: Markdown renders soft line breaks as one paragraph.
_CLOSERS = '*_~`\'")\\]'
_SENT_END = re.compile(r'(?:(?<=[.!?])|(?<=[.!?][' + _CLOSERS + r'])|(?<=[.!?][' + _CLOSERS + r']{2})'
                       r'|(?<=[.!?][' + _CLOSERS + r']{3}))\s+(?=\S)|(?<![,;:(\-])[ \t]*\n\s*(?=[A-Z])')


def is_status(sentence):
    """True when one sentence says where the PR stands (tangentswarm #130)."""
    sentence = ' '.join(sentence.split())  # a soft line break is a space
    sentence = re.sub(r'(?<!\w)[_*~]+|[_*~]+(?!\w)', ' ', sentence)  # _Merged_ is Merged; snake_case stays
    return bool(_SERVES.search(sentence) or _STATE.search(sentence))


def _paragraphs(text):
    """The Markdown paragraphs and list items of text, each one string. Code fences (also
    inside a blockquote) are not read: a fence closes only on a run of the same character,
    as long or longer, with nothing after it (CommonMark)."""
    paras, para, fence = [], [], None
    for line in text.split('\n'):
        m = _FENCE.match(_QUOTE.sub('', line))
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
            continue
        if m and not (m.group(1)[0] == '`' and '`' in m.group(2)):  # a backtick info string has no backtick
            fence = m.group(1)
        elif line.strip() and not _BLOCK.match(line):
            para.append(line)
            continue
        if para:
            paras.append('\n'.join(para))
        para = [line] if (not m and line.strip()) else []
    if para:
        paras.append('\n'.join(para))
    return paras


def status_lines(body):
    """The sentences of a PR body that say where the PR stands. The brief section and code
    fences never count. The swarm never edits the body (Michal via MethBot on PR #133,
    option A): it only flags them."""
    from .brief import strip_brief
    return [' '.join(x.split()) for p in _paragraphs(strip_brief(body)) for x in _SENT_END.split(p) if is_status(x)]


def status_text(n, found):
    """The relay that tells PR n's worker to remove the status sentences."""
    said = '; '.join(f'"{g[:160]}"' for g in found[:5])
    return (f"Coordinator: #{n} is held before Human Review: its PR body says where the PR stands: {said}. "
            f"{STATUS_RULE} Remove or reword those sentences in the PR body. Do not change code; "
            "push nothing, edit only the PR body.")


def check_status(n, body, repo, head='', dry=False, incident=None):
    """(ok, why) for the body rule of tangentswarm #130. Not ok when the body states where
    the PR stands: the caller holds hr-gate --apply and tells the worker. One incident per
    head. The body is never edited."""
    found = status_lines(body)
    if not found:
        return True, ''
    said = '; '.join(f'"{g[:160]}"' for g in found[:5])
    why = f"the body states where the PR stands (tangentswarm #130): {said}"
    if dry:
        return False, why
    if incident is None:
        from . import alerts
        incident = alerts.incident
    try:
        incident(f"pr-status:{repo}#{n}@{(head or '')[:12]}", f"{repo}#{n} PR body stated where the PR stands",
                 f"The body of {repo}#{n} said: {said}. Status text goes stale and contradicts the board.",
                 why="A worker wrote deploy or merge state into the PR body.",
                 fix="The swarm held hr-gate --apply and told the worker to remove it.",
                 prevent="Workers get STATUS_RULE: the body says only how to see the change (tangentswarm #130).")
    except Exception:
        pass  # a failed ticket must not change the verdict
    return False, why


def needs_screenshots(paths, body=''):
    """True when condition 7a applies: the PR changes a visible UI file. api-only and
    docs-only PRs (and tooling: .github/, scripts/, tests/) are exempt, and so is a body
    that says code-only (Michal 2026-11-15: #519 was demoted for screenshots it did not
    need a worker for)."""
    if CODE_ONLY_RE.search(body or '') and not any(p.startswith(('web/', 'app/')) for p in paths):
        return False
    return any(UI_RE.search(p) and not p.startswith(NO_SHOTS_PREFIXES) and not p.endswith('.md')
               for p in paths)


def check_images(body, repo, exists, probe=probe_image):
    """(ok, why) for condition 7a. exists(path) -> bool says if a file is on swarm-shots."""
    urls = image_urls(body)
    if not urls:
        return False, 'no image in body'
    bad = []
    for u in urls:
        if not u.lower().startswith('https://'):
            bad.append(f"{u[:60]} is not an absolute https URL (a relative name never renders)")
        elif _ATTACH.fullmatch(u):
            continue
        elif shots_path(u, repo) is not None:
            if not exists(shots_path(u, repo)):
                bad.append(f"{u[:80]} is not on the {SHOTS_BRANCH} branch")
        else:
            ok, why = probe(u)
            if not ok:
                bad.append(why)
    if bad:
        return False, f"{len(bad)} of {len(urls)} image(s) do not render: " + '; '.join(bad[:3])
    return True, f"{len(urls)} image(s), all absolute https and resolving"


MARKER_GREP = ['grep', '-l', '--all-match', '-e', '^<<<<<<< ', '-e', '^>>>>>>> ']  # git grep args


def work_problem(marker_files, redeploy=None):
    """Why origin/work is broken, or None. marker_files: files with both <<<<<<< and >>>>>>>
    lines (git <MARKER_GREP> origin/work). redeploy: the latest work Redeploy run
    {sha, status, conclusion} or None. Only a FAILED run counts (skipped is the webhook mode)."""
    if marker_files:
        more = f" and {len(marker_files) - 3} more" if len(marker_files) > 3 else ''
        return f"origin/work has conflict markers in {', '.join(marker_files[:3])}{more}"
    if redeploy and (redeploy.get('conclusion') or '').lower() in ('failure', 'timed_out', 'startup_failure'):
        return f"the latest work Redeploy run failed (work {(redeploy.get('sha') or '')[:8]})"
    return None


def check_served(paths, entering, static_sha, has_head, err=None, tips=(), contains=None, work_bad=None):
    """(ok, why) for condition 6. has_head(sha) -> bool: the deployed commit contains the head.
    entering: the card is not already in Human Review (strict). Else lenient (a re-gate).
    tips: [(name, sha)] the stack tips condition 9 picked (a -vN branch). The staging pull
    merges that tip into work, not the plain head, so a deployed commit that contains the
    tip serves the PR. contains(tip_sha, static_sha) -> bool checks it.
    work_bad: work_problem()'s reason; it fails entry before any SHA check."""
    if not any(SERVED_RE.search(p) for p in paths):
        return True, 'n/a (no served files: web/, api/, app/)'
    if work_bad:
        return (False, work_bad) if entering else (True, 'lenient re-gate: ' + work_bad)
    if err or not static_sha:
        why = f"cannot read the static .deploy-sha ({err or 'empty'})"
        return (False, why) if entering else (True, 'lenient re-gate: ' + why)
    if has_head(static_sha):
        return True, f"static .deploy-sha {static_sha[:8]} contains the head"
    for name, sha in tips:
        if sha and contains and contains(sha, static_sha):
            return True, f"static .deploy-sha {static_sha[:8]} contains {name} {sha[:8]} (the tip condition 9 picked)"
    why = f"static .deploy-sha {static_sha[:8]} LACKS the head: staging does not serve it yet"
    return (False, why) if entering else (True, 'lenient re-gate: ' + why)


PRIORITY_DEFAULT = 'ptw'           # site key; [staging] priority_default overrides it
PRIORITY_MILESTONES = {'PTW': 'ptw'}  # milestone word -> site key; [staging] priority overrides it


def _milestone_match(word, title):
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', title or '', re.I) is not None


def priority_site(milestones=(), cfg=None):
    """The site key of a PR's priority staging site (tangentswarm #116). milestones: the
    titles of the PR's milestone and of the issues it closes. The first title that holds a
    configured milestone word (case-insensitive, a whole word) picks its site; else the
    configured default."""
    if cfg is None:
        from . import config
        cfg = config.get('staging', default={}) or {}
    table = cfg.get('priority') if isinstance(cfg.get('priority'), dict) else PRIORITY_MILESTONES
    for title in milestones or ():
        for word, key in table.items():
            if _milestone_match(word, title):
                return key
    return cfg.get('priority_default') or PRIORITY_DEFAULT


def priority_host(milestones=(), cfg=None):
    """The host of the priority staging site ([staging] sites maps key -> host), or None when
    the config names no such site (then every site is checked, as before #116)."""
    if cfg is None:
        from . import config
        cfg = config.get('staging', default={}) or {}
    return (cfg.get('sites') or {}).get(priority_site(milestones, cfg))


_DEFAULT = object()


def check_served_sites(paths, entering, sites, has_head, tips=(), contains=None, work_bad=None, priority=_DEFAULT):
    """(ok, why) for condition 6. sites: [(host, static_sha, err)].

    tangentswarm #116: only the PR's priority site (host `priority`; by default the configured
    default priority site, see priority_host) must serve the head (or the -vN tip condition 9
    picked). The other sites roll out in the background: a lag there is named after
    "rolling out", never a failure. When the priority host is not among `sites` (or the
    config names none), EVERY site must serve it (platform #493, the rule before #116).
    A re-gate (not entering) stays lenient, and names the sites that lag."""
    if not any(SERVED_RE.search(p) for p in paths):
        return True, 'n/a (no served files: web/, api/, app/)'
    if work_bad:
        return (False, work_bad) if entering else (True, 'lenient re-gate: ' + work_bad)
    if not sites:
        return (False, 'no staging site to check') if entering else (True, 'lenient re-gate: no staging site')
    if priority is _DEFAULT:
        try:
            priority = priority_host()
        except Exception:
            priority = None
    lag = 'lenient re-gate: '
    res = [(host, *check_served(paths, entering, sha, has_head, err, tips, contains)) for host, sha, err in sites]
    first = [r for r in res if r[0] == priority] if priority else []
    rest = [r for r in res if r not in first] if first else []
    must = first or res
    bad = [f"{h}: {w[len(lag):] if w.startswith(lag) else w}" for h, ok, w in must if not ok or w.startswith(lag)]
    behind = [h for h, ok, w in rest if not ok or w.startswith(lag)]
    roll = f"; rolling out (not blocking): {', '.join(behind)}" if behind else ''
    if bad:
        why = (f"the priority site does not serve it: {bad[0]}" if first else
               f"{len(bad)} of {len(res)} staging sites do not serve it: " + '; '.join(bad)) + roll
        return (False, why) if entering else (True, lag + why)
    if first:
        h, _, w = first[0]
        return True, f"the priority site serves it: {h}: {w}" + roll
    return True, f"all {len(res)} staging sites serve it: " + '; '.join(f"{h}: {w}" for h, _, w in res)


# Condition 4 (Michal 2026-11-14 7:22 PM ET): the MinBot-passed label is retired. The pass is
# a MinBot comment that names the commit it passed: "### MinBot pass" with "Commit: `<sha>`"
# (minbot evidence --title "MinBot pass" --commit SHA), or "### MinBot pass on <sha>".
MINBOT_LOGIN = 'tangentcode-minbot'
_PASS_TITLE = re.compile(r'^\s*###\s*MinBot pass\b')
_HEX = re.compile(r'\b[0-9a-f]{8,40}\b')


def minbot_pass(comments, shas):
    """(ok, why) for condition 4. shas: [(label, sha)] such as [('head', H)] or the -vN tip.
    Passes when a MinBot pass comment names one of them (8 or more hex characters of it)."""
    for c in reversed(list(comments or [])):
        login = ((c.get('author') or {}).get('login') or '').replace('[bot]', '')
        body = c.get('body') or ''
        if login != MINBOT_LOGIN or not _PASS_TITLE.match(body):
            continue
        named = _HEX.findall(body)
        for label, sha in shas:
            if sha and any(sha.startswith(t) for t in named):
                return True, f"MinBot pass comment names {label} {sha[:8]}"
    want = ', '.join(f"{label} {sha[:8]}" for label, sha in shas if sha)
    return False, f"no MinBot pass comment names {want}"


HUMAN = 'tangentstorm'


def later_requests(pr):
    """Condition 8 (as hr-gate evaluate): [(kind, time)] of each tangentstorm comment, review
    with a body, review comment or review-thread comment newer than the head's commit date,
    except "@codex review" and a MinBot pass. [] when none, or when the read has no commit
    date (then hr-gate's own row decides). tangentswarm #136 (Codex on PR #138): Ready does
    not prove it, so the fast Ready -> Staging move checks it from the fresh PR read."""
    from datetime import datetime

    def ts(s):
        try:
            return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        except ValueError:
            return None

    def human(node):
        return ((node or {}).get('author') or {}).get('login') == HUMAN

    def codex_only(body):
        b = (body or '').strip()
        return b.lower() == '@codex review' or b.startswith('### MinBot pass on ')
    commits = (pr.get('commits') or {}).get('nodes') or []
    head = ts(((commits[-1] if commits else {}).get('commit') or {}).get('committedDate') or '')
    if head is None:
        return None
    out = set()

    def check(kind, node, when_key='createdAt', need_body=False):
        when = ts(node.get(when_key) or '')
        body = node.get('body')
        if not human(node) or when is None or when <= head or codex_only(body):
            return
        if need_body and not (body or '').strip():
            return
        out.add((kind, when))
    for c in (pr.get('comments') or {}).get('nodes') or []:
        check('comment', c)
    for r in (pr.get('reviews') or {}).get('nodes') or []:
        check('review', r, 'submittedAt', need_body=True)
        for rc in (r.get('comments') or {}).get('nodes') or []:
            check('review comment', rc)
    for t in (pr.get('reviewThreads') or {}).get('nodes') or []:
        for rc in (t.get('comments') or {}).get('nodes') or []:
            check('review comment', rc)
    return sorted(out, key=lambda x: x[1], reverse=True)
