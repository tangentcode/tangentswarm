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
| `issue-in-pr-column` | An open issue is in In Progress, Auto Review, Ready, Staging, Human Review, Done, Canceled, or Parked | An issue never goes in a pull-request column. Done holds closed issues |
| `issue-in-needs-input` | An open issue is in Needs Input. The finding says where it goes: with no open pull request, Triage or Discuss; with one, the pull request goes in Needs Input and the issue in Active | #82: "an issue in Needs Input with no PR belongs in Triage (simple question for him) or Discuss; when the issue has a PR, the PR is what goes in Needs Input" |
| `issue-not-active` | An issue (not `work2`) in Inbox, Discuss, Triage, or Backlog has an open pull request in a flow column | #82: "An issue should move to Active as soon as its PR is created" |
| `open-pr-in-done` | An open pull request is in Done | Done holds pull requests that are merged to main |
| `open-pr-in-canceled` | An open pull request is in Canceled | Canceled holds closed pull requests |
| `not-draft` | A pull request in In Progress, Needs Input, or Parked is not a draft | These columns hold draft pull requests |
| `draft` | A pull request in Auto Review, Ready, Staging, or Human Review is a draft | Auto Review holds pull requests that are marked ready |
| `no-live-claude` | A pull request in In Progress has no live Claude in a worktree on its head branch (same repo name; the owner is not compared, so a moved repo or a stale origin still counts; a worktree in a rebase counts on the branch it rebases) | In Progress: a worker works on each one now |
| `human-review-over-two` | Human Review of one board holds more than two pull requests | Never more than two |
| `active-work2` | An Active issue has the `work2` label | Active holds issues without `work2` |
| `active-pr-count` | An Active issue does not have exactly one open pull request in a flow column or in Deferred that closes it | Exactly one open linked pull request in a flow column |
| `flow-pr-active-count` | A pull request in a flow column does not belong to exactly one Active issue (no closing link, or zero or two of its non-`work2` issues in Active). A pull request that closes only `work2` issues is correct. An issue in Deferred owns its pull request. When an issue card of the pull request is in a wrong column, the finding is on that issue card, not here | A pull request in a flow column belongs to exactly one Active issue |
| `duplicate-claudes` | Two or more live Claudes run in one git worktree | One Claude per worktree |
| `unit-source` | A swarm user unit (`swarm*.service`, enabled or active), or its running process, does not import tangentswarm from `~/ver/tangentswarm.flow-live`. One finding for each unit. When `systemctl --user` cannot be read, the unit findings are kept, as for a board that is not read | Every swarm service runs from flow-live (methodology #84; `swarm-unit-check`) |

The flow columns are In Progress, Auto Review, Needs Input, Ready, Staging, and Human
Review.

Deferred (#82) is a valid column on Project 4. It is a temporary holding column for
tickets from older workflow versions. A card in Deferred never causes a finding. A pull
request in Deferred still counts as the pull request of its issue. Example: platform #471
in Active with pull request #486 in Deferred is not an `active-pr-count` finding. On other
boards, Deferred is an `unknown-column` finding.

Live Claudes come from `/proc`: our processes that `registry.classify_process` calls
`claude`, and that are not zombies. A Claude that another Claude started (a descendant) is
part of that Claude. The worktree of a Claude is the nearest directory above its working
directory that has a `.git`. The repo and branch come from the `.git` files (the `origin`
URL and `HEAD`), with no git process.

These do not cause a finding:

- An Active issue with no pull request while its launch is in flight (the watchdog's
  `launching_issues`: launching, unstarted, or named in the last 10 minutes).
- An In Progress pull request whose Claude is starting now (an `unstarted` record).
- An In Progress pull request that waits for a Claude (#101): a conflicting pull request
  goes to In Progress at once, and its feedback waits in the revive queue (`queued`) or in
  an `unstarted` record until a worker slot or memory frees.
- A finding that goes away before its grace ends.

The snapshot has open items only. So the check does not see closed issues in Triage or
Backlog, or merged pull requests outside Done. The watchdog sweep handles Done.

Not checked yet: "assigned to the swarm" for Active issues (the card is on Project 4), the
Ready rules about the Codex review and the MinBot pass comment, and the Staging merge rules.
Add a rule to `sanity.check`, its row here, and a test.

## What it does with a finding

1. A new finding waits `SANITY_GRACE` (default 600 s). A card move and its draft change,
   or a Claude start, are not one atomic step.
2. One report per pass (#82). If a pass has six new findings, it opens one incident
   ticket that lists all six, on the methodology board, Project 6 (`alerts.incident`: at
   most `SWARM_INCIDENTS_PER_HOUR` an hour). A capped or failed ticket is tried again on
   the next pass.
3. Then the pass wakes swarmbot once (`alerts.alert`), with one message that lists all the
   new findings and names the ticket.
4. Each finding is reported once (#82). The state in
   `~/.local/state/swarm-flow/sanity.json` is keyed per finding (rule, board, and item).
   A later pass reports only the findings that are new. When a finding goes away, it is
   removed. If it comes back later, it is new again and is reported again.

If the read of a board fails (a GitHub limit), the other checks still run. The stored
findings of that board stay as they are (they are not resolved and filed again), and the
check runs again after `SANITY_RETRY`. A pass that fails as a whole also runs again after
`SANITY_RETRY`.

A dry run (`swarm-sanity`, or a dry watchdog) prints what it would do. It does not write
the state, wake swarmbot, or file a ticket.
