# The sanity check (tangentswarm #43)

The sanity check is a deterministic check, with no LLM. It looks at the swarm boards and
at the live Claude processes. It reports each place where they break a rule. The code is
`tangentswarm/sanity.py`. The command is `swarm-sanity`.

## When it runs

The watchdog thread of `swarm-promoted` runs it (Michal, issue #43: "check on board-change
events, at server startup, and once an hour as a backstop. No 5-minute polling."):

- At startup: the first watchdog pass.
- On a board change:
  - The webhook gets a `projects_v2_item` event and touches the poke file
    (`~/.local/state/swarm-flow/sanity-poke`).
  - Or the Status or draft flag of a card in the shared Project 4 snapshot changes. The
    watchdog reads that snapshot each minute anyway, so this is not a new GitHub read.
- Once an hour (`SANITY_EVERY`, default 3600 s), as a backstop.
- When the grace of a pending finding ends, or when a capped incident ticket can be tried
  again (`SANITY_RETRY`, default 600 s).

## What it checks

Project 4 is read from the shared board snapshot. It has the open pull requests and the
open issues, so every rule runs there. The other boards (Project 6) are read with
`boards.open_prs`. That read has open pull requests only, so the issue rules and the
Active rules do not run there.

| Rule id | Finding | Rule in workflow.md |
|---|---|---|
| `no-status` | An open card has an empty Status | Each card must have a Status. Never leave the Status empty |
| `unknown-column` | A column that the Board columns table does not name. One finding for each column | The Board columns table |
| `pr-in-issue-column` | An open pull request is in Inbox, Discuss, Triage, Backlog, or Active | Issues and pull requests use different columns |
| `issue-in-pr-column` | An open issue is in In Progress, Auto Review, Needs Input, Ready, Staging, Human Review, Done, Canceled, or Parked | An issue never goes in a pull-request column. Done holds closed issues |
| `open-pr-in-done` | An open pull request is in Done | Done holds pull requests that are merged to main |
| `open-pr-in-canceled` | An open pull request is in Canceled | Canceled holds closed pull requests |
| `not-draft` | A pull request in In Progress, Needs Input, or Parked is not a draft | These columns hold draft pull requests |
| `draft` | A pull request in Auto Review, Ready, Staging, or Human Review is a draft | Auto Review holds pull requests that are marked ready |
| `no-live-claude` | A pull request in In Progress has no live Claude in a worktree on its head branch (same repo) | In Progress: a worker works on each one now |
| `human-review-over-two` | Human Review of one board holds more than two pull requests | Never more than two |
| `active-work2` | An Active issue has the `work2` label | Active holds issues without `work2` |
| `active-pr-count` | An Active issue does not have exactly one open pull request in a flow column that closes it | Exactly one open linked pull request in a flow column |
| `flow-pr-active-count` | A pull request in a flow column does not belong to exactly one Active issue (no closing link, or zero or two of its non-`work2` issues in Active). A pull request that closes only `work2` issues is correct | A pull request in a flow column belongs to exactly one Active issue |
| `duplicate-claudes` | Two or more live Claudes run in one git worktree | One Claude per worktree |

The flow columns are In Progress, Auto Review, Needs Input, Ready, Staging, and Human
Review.

Live Claudes come from `/proc`: our processes that `registry.classify_process` calls
`claude`, and that are not zombies. A Claude that another Claude started (a descendant) is
part of that Claude. The worktree of a Claude is the nearest directory above its working
directory that has a `.git`. The repo and branch come from the `.git` files (the `origin`
URL and `HEAD`), with no git process.

These do not cause a finding:

- An Active issue with no pull request while its launch is in flight (the watchdog's
  `launching_issues`: launching, unstarted, or named in the last 10 minutes).
- An In Progress pull request whose Claude is starting now (an `unstarted` record).
- A finding that goes away before its grace ends.

The snapshot has open items only. So the check does not see closed issues in Triage or
Backlog, or merged pull requests outside Done. The watchdog sweep handles Done.

Not checked yet: "assigned to the swarm" for Active issues (the card is on Project 4), the
Ready rules about the Codex review and the MinBot pass comment, and the Staging merge rules.
Add a rule to `sanity.check`, its row here, and a test.

## What it does with a finding

1. A new finding waits `SANITY_GRACE` (default 600 s). A card move and its draft change,
   or a Claude start, are not one atomic step.
2. If it is still there, the pass wakes swarmbot once (`alerts.alert`). There is one wake
   for all the new findings of that pass.
3. It opens one incident ticket for each finding on the methodology board, Project 6
   (`alerts.incident`: at most `SWARM_INCIDENTS_PER_HOUR` an hour). A capped or failed
   ticket is tried again on the next pass.
4. The finding stays in `~/.local/state/swarm-flow/sanity.json` while it is there. The
   same finding is not told or filed again. When it goes away, it is removed. If it
   comes back later, it is a new incident.

If the read of a board fails (a GitHub limit), the other checks still run. The stored
findings of that board stay as they are (they are not resolved and filed again), and the
check runs again after `SANITY_RETRY`. A pass that fails as a whole also runs again after
`SANITY_RETRY`.

A dry run (`swarm-sanity`, or a dry watchdog) prints what it would do. It does not write
the state, wake swarmbot, or file a ticket.
