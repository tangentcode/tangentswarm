"""Needs Input on the PR card (tangentswarm #156): the question goes on the PR (the card Michal
opens) as well as on the issue, it stands on its own, and a brief-only PR body gets a summary.

Michal 2026-11-19 12:23 PM ET, on platform #589: "it's in needs input, but I don't see any
questions on it ... it should not be a needs input without a lot of clarification."

  body()          the question comment: what the PR is for, the worker's question (the choice,
                  the options with what each changes, the worker's pick), a main-conflict
                  note that names who fixes it, and how to answer.
  targets()       where it goes: the first linked issue and the PR (only the PR when there is
                  no issue). Each target has its own once-key, so a retry posts only the
                  missing one.
  summarize()     a PR body that is only "Fixes #N" and the swarm brief gets a short plain
                  summary at the top.
  pointer_only()  a question that only says "see the issue" (refused by agent_report.check).
"""
import re

from . import brief

SUMMARY_MARK = '<!-- swarm-summary -->'
INFO_Q = ('query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n)'
          '{title body mergeable closingIssuesReferences(first:5){nodes{number title}}}}}')
POINTER_RE = re.compile(
    r'^\W*(?:please\s+)?(?:see|read|check|look\s+at|refer\s+to)\s+(?:the\s+)?'
    r'(?:issue|ticket|pr|brief|comment|spec|#\d+|https?://\S+)', re.I)
POINTER_MAX = 160  # chars: a longer question has room for its own options
CLOSES_RE = re.compile(r'(?im)^\s*(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+(?:[\w.-]+/[\w.-]+)?#\d+\s*\.?\s*$')
HOW = ('Ask a question that stands on its own: what this PR is for, the choice in plain English, '
       'the options with what each one changes, and your pick. Never only "see the issue".')


def pointer_only(q):
    """True when q only points elsewhere ("see the issue", "see #588") and asks nothing itself."""
    q = ' '.join((q or '').split())
    return len(q) < POINTER_MAX and bool(POINTER_RE.match(q))


def info(g, n, repo):
    """{title, body, mergeable, issues: {number: title}} of PR n, or {} when the read fails."""
    owner, name = repo.split('/')
    try:
        d = g['gql'](INFO_Q, owner=owner, name=name, n=int(n))
    except Exception:
        return {}
    pr = ((d or {}).get('repository') or {}).get('pullRequest') or {}
    if not pr:
        return {}
    return dict(title=pr.get('title') or '', body=pr.get('body') or '', mergeable=pr.get('mergeable') or '',
                issues={i['number']: i.get('title') or '' for i in
                        ((pr.get('closingIssuesReferences') or {}).get('nodes') or []) if i.get('number')})


def conflicts(pr, wt=None, head=None, merges=None):
    """True when the PR conflicts with main: GitHub says CONFLICTING, or (GitHub not sure yet)
    the pushed head does not merge onto origin/main. False when unknown."""
    m = (pr or {}).get('mergeable') or ''
    if m == 'CONFLICTING':
        return True
    if m == 'MERGEABLE' or not merges or not wt or len(head or '') != 40:
        return False
    try:
        return not merges(wt, head)
    except Exception:
        return False


def purpose(n, pr, issues=()):
    """One plain line: what PR n is for (its title and the issues it fixes)."""
    titles = (pr or {}).get('issues') or {}
    title = ((pr or {}).get('title') or '').strip().rstrip('.')
    line = f"PR #{n}" + (f": {title}." if title else '.')
    fixed = [f"#{i}" + (f" ({titles[i].strip().rstrip('.')})" if titles.get(i) else '') for i in issues]
    if fixed:
        line += f" It fixes issue {', '.join(fixed)}."
    return line


def quote(text):
    return '\n'.join('> ' + line for line in (text or '').strip()[-3000:].splitlines())


def body(mark, n, text, pr=None, issues=(), conflict=False, kind='question', how=''):
    """The stand-alone comment for a question (kind 'question') or a blocker (kind 'blocker')."""
    head = 'Question from the worker' if kind == 'question' else 'The worker is blocked'
    where = f"issue #{issues[0]} and PR #{n}" if issues else f"PR #{n}"
    parts = [mark, f"**{head} on #{n}** (the card is in Needs Input{'; ' + how if how else ''}).",
             f"**What this PR is for:** {purpose(n, pr, issues)}",
             f"**The {kind}:**\n\n{quote(text)}"]
    if conflict:
        parts.append(f"**Merge conflict:** PR #{n} also conflicts with main. The worker will fix the conflict "
                     "after your answer: it rebases on main and pushes again. You do not need to fix it.")
    parts.append(f"To answer: reply in a comment here (this question is on {where}), then move the card "
                 "back to In Progress. The worker gets your answer and goes on.")
    return '\n\n'.join(parts)


def targets(n, issues):
    """[(gh noun, number, once-key suffix)]: the first linked issue and the PR, or the PR only.
    The first target keeps the plain key (the one used before #156), so an old post is not repeated."""
    if issues:
        return [('issue', issues[0], ''), ('pr', n, f':pr{n}')]
    return [('pr', n, '')]


def comment_argv(noun, number, repo, text):
    return ['gh', noun, 'comment', str(number), '-R', repo, '--body', text]


def brief_only(pr_body):
    """True when the PR body says nothing beyond "Fixes #N" lines and the swarm brief."""
    rest = CLOSES_RE.sub('', brief.strip_brief(pr_body or ''))
    return not rest.strip() and SUMMARY_MARK not in (pr_body or '')


def summary(n, pr, issues, kind='question'):
    return (f"{SUMMARY_MARK}\n## Summary\n\n{purpose(n, pr, issues)} The worker has a {kind} for Michal "
            "before it can finish: see the Needs Input comment on this PR. The worker's brief is below.\n")


def with_summary(pr_body, text):
    return text + '\n' + (pr_body or '').lstrip('\n')


def summarize(n, pr, issues, repo, sh, kind='question'):
    """Put a summary at the top of PR n's body when the body is only the brief. Returns what it
    did ('wrote', or None for no change). sh(argv) runs gh."""
    if not pr or not brief_only(pr.get('body')):
        return None
    new = with_summary(pr.get('body'), summary(n, pr, issues, kind))
    sh(['gh', 'api', '-X', 'PATCH', f"repos/{repo}/pulls/{int(n)}", '-f', f"body={new}"])
    return 'wrote'
