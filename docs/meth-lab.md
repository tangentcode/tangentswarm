# Meth Lab: the test environment for the swarm flow

tangentswarm #115. Michal approved it on 2026-11-17 at 11:07 AM ET: "let's go ahead and make
the test environment for the methodology, and that way we can call it meth lab".

Meth Lab has two parts:

1. The everyday check. It uses no GitHub, no network and no Codex. It runs in the normal test
   suite and as a read-only check before a deploy.
2. The live sandbox. It is a separate Free GitHub organization with a private test repository
   and board. Use it only on demand, for a live end-to-end check.

The code is `tangentswarm/methlab.py`. The tests are `tests/test_methlab.py`.

## 1. The everyday check

### The fake GitHub

`methlab.FakeGitHub` keeps GitHub state in memory: PRs, issues, board cards, Codex reviews,
CI runs and comments. The real `webhook.classify()` and the real `webhook.Executor` run
against it. Each step changes the fake state the way GitHub would, and gives the webhook
delivery that GitHub would send:

| step | delivery |
| --- | --- |
| `open_issue(n)` | `issues` opened |
| `open_pr(n, draft=, closes=)` | `pull_request` opened |
| `push(n)` | `pull_request` synchronize (a new head) |
| `ready(n)` | `pull_request` ready_for_review |
| `codex_review(n, findings=)` | `pull_request_review` submitted by Codex on the head |
| `ci(n, conclusion)` | `check_suite` completed on the head |
| `comment(n, body, user=)` | `issue_comment` created |
| `move(n, column, by=)` | `projects_v2_item` edited (a card move) |
| `merge(n)`, `close(n)` | `pull_request` closed |

`fake.deliver(step)` runs the delivery through the rules. Then a test checks the result:

- `fake.col(n)`: the card column.
- `fake.pr(n)`: the PR state (head, draft, comments).
- `fake.relays`, `fake.queued`, `fake.cascades`, `fake.wakes`, `fake.merges`: what the swarm did.
- `fake.asks(n)`: the heads that got an "@codex review" comment.
- `fake.history` and `fake.errors()`: each delivery, its actions, notes and errors.

Example: a Project 6 PR from open to Done.

```python
f = methlab.FakeGitHub('tangentcode/tangentswarm')
f.deliver(f.open_issue(115))                 # Triage card
f.deliver(f.open_pr(125, closes=[115]))      # In Progress card
f.deliver(f.ready(125))                      # Auto Review, "@codex review" on the head
f.deliver(f.codex_review(125, findings=True))  # relay to the worker, back to In Progress
...
```

When the flow makes a GitHub read or a gh command that the fake does not know, the fake
raises `FakeGitHubError`. The Executor reports it, so it shows in `fake.errors()`. A new
GitHub call in the flow is never a silent pass. Add an answer to `FakeGitHub.gql()` or
`FakeGitHub.sh()` for it.

The full run writes the swarm's own state files (the event DB and the seen files). Run it
only under the test isolation of `tests/conftest.py`, never against the live state.

### The replay of recorded deliveries

The live server keeps each delivery in the event DB
(`~/.local/state/swarm-flow/events.sqlite`): the raw body and the decision it took.
`swarm-methlab replay` reads those deliveries and runs each one through the current
`classify()`. It has no side effect: the DB is opened read-only, and the sanity poke of a
board event is off. It prints each kind of change once, with a count:

    swarm-methlab replay --hours 24          # the deliveries of the last 24 hours
    swarm-methlab replay --each              # one line per changed delivery
    swarm-methlab replay --all --limit 50    # the last 50 deliveries, changed or not
    swarm-methlab replay --db PATH           # another event DB

The exit code is 0 when every decision is the same, and 1 when one or more changed.

Use it before a deploy: run it in the new checkout with `--hours` set to the time since the
last deploy. A change is not always a bug. It can be a rule change that you made on purpose.
Read each line and confirm it.

## 2. The live sandbox

Use the live sandbox only to check that real GitHub webhooks, the real board and real merges
still behave the way the swarm expects. Do not leave it running.

### Rules

- Use a separate Free organization. Cost: $0 on the Free plan.
- Keep the test repository private. Do not turn on GitHub Actions unless a check needs it.
- Do not connect Codex to the test repository. Test PRs would use the Codex allowance.
- Do not reuse the production GitHub App or its webhook URL. Use a separate webhook with its
  own secret, pointed at a test copy of swarm-webhook on its own port.
- Start the test copy of swarm-webhook only for a check, and stop it after. The server has
  8 GB of memory.
- Keep the number of test issues and PRs small, and mark each one as a test. Wait at least one
  second between two GitHub writes. Stay far below GitHub's limit of 80 content-creating
  requests per minute and 500 per hour.

### Setup (once, by Michal)

A human must create the organization in the GitHub web UI.

1. Go to https://github.com/organizations/plan and pick "Free".
2. Name the organization `tangentcode-methlab` (if the name is taken, pick another and put it
   in the config below).
3. In the organization, create a private repository `sandbox` with a README.
4. In the organization, create a project (board) named "Meth Lab" with a Status field. Give
   the Status field the same columns as Project 6.
5. In the repository settings, add a webhook. Set the URL to the test copy of swarm-webhook
   (its own port, see below), the content type to `application/json`, and a new secret. Pick
   "Send me everything". Do not use the production webhook URL or secret.
6. Do not install the ChatGPT Codex connector on this organization.

### Config

Put the settings in `~/.config/tangentswarm/config.toml`:

```toml
[methlab]
org = "tangentcode-methlab"
repo = "tangentcode-methlab/sandbox"
project_number = 1
port = 8782
secret_files = ["~/.config/tangentswarm/methlab-webhook-secret"]
state_dir = "~/.local/state/methlab"
```

`swarm-methlab live-check` checks the settings. It refuses a production org or repository
(the board number is read only inside the Meth Lab org), the production webhook port or secret, and `codex = true`.

`swarm-methlab live-env` prints the environment for the test copy of swarm-webhook: its own
port, secret, log, inbox, seen file and event DB under `state_dir`. The swarmbot wake URL is a
file that does not exist, so a test exception never wakes the live swarmbot. It refuses to
print anything when `live-check` finds a problem.

The first live end-to-end run, and the config of the sandbox board for it, is a follow-up
after the organization exists.
