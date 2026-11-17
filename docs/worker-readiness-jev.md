# Worker readiness classification with JEV (design)

Status: specification. Issue #12. Nothing here is built.
This document does not authorize code, token retrieval, paid API calls, or polling.
Section "Open questions" lists what the maintainer must confirm before any live work.

## Why

The swarm must know if a worker pane is ready for input, is busy, or waits on a
question. The deterministic probes (below) answer most cases. Some screens are
ambiguous: an unfamiliar dialog, a question in free text, a new agent layout. For those
cases, a low-cost model can give a second, advisory opinion from the screen text.

## Scope and safeguards

- Input is terminal screen TEXT from the existing swarm capture. It is never a screenshot.
- The deterministic probe always runs first. The model runs only when the probe has no
  answer, and only for changed, compact, redacted text.
- The result is advisory. It must not push, merge, dispatch work, restart queues, type
  into a pane, or move a card. It can only be shown (status, events, logs) and counted.
- No secret and no unredacted screen text goes into a log, an event, or a PR comment.

## Existing deterministic probes (the first stage)

These exist today and stay the source of truth:

- `tangentswarm/agents.py`: `claude_prompt_blank`, `codex_prompt_blank`,
  `gemini_prompt_blank`, `muse_prompt_blank`, `grok_prompt_blank`, and `claude_prompt(raw)`.
  `claude_prompt` reads an escape-coded capture and separates a dim/grey ghost
  suggestion (`suggestion`) from typed input (`input`). `pane_blocked` finds dialogs.
- `tangentswarm/tick.py`: `classify_screen(screen, prompt_blank)` gives `working`,
  `menu`, `input`, `asking` or `idle`. `question_line(screen)` finds a pending question.
- `tangentswarm/tmux_status.py`: `is_busy(screen)` reads only the status area.
- `tangentswarm/livestate.py`: `PaneWatch` keeps a hash of each pane screen and the time
  it last changed.

The classifier calls the model only when the deterministic result is "no answer". The
design defines "no answer" as: no prompt detector for the agent, or `classify_screen`
gives a class that does not map to a readiness label (see the mapping below).

| Deterministic result          | Readiness label     | Model call? |
|-------------------------------|---------------------|-------------|
| `working` / `is_busy`         | `busy`              | no          |
| `menu`, `asking`, blocked     | `question_pending`  | no          |
| `idle` with a blank prompt    | `ready`             | no          |
| `input` (real typed text)     | `busy`              | no          |
| no detector, or no match      | (none)              | yes, if allowed |

## Labels

`ready`, `question_pending`, `busy`, `unknown`.

`unknown` is the fallback. The classifier gives `unknown` when:
- the model is off, not configured, or the call fails (network, HTTP error, timeout,
  bad response body);
- the screen text is stale (see below);
- the top probability or the confidence is below its threshold;
- the provider gives a label that is not one of the three model labels.

## Ghost autocomplete

A ghost suggestion is grey/dim text that the agent draws in the input box. It is not
typed input. Before text goes to the model, the classifier takes the escape-coded
capture and uses `claude_prompt(raw)` (or `input_view`) to remove the suggestion. The
model never sees ghost text as input. If the suggestion cannot be told apart from input,
the existing rule applies: treat it as input, so the result errs toward "not ready".

## Stale output

- Each request carries the screen hash and the capture time.
- If the capture is older than `max_age_s` (default: the PaneWatch capture interval), the
  classifier gives `unknown` and makes no call.
- A cached model answer is valid only for the same screen hash. When the hash changes,
  the old answer is dropped. An old `ready` never stands for a new screen.
- Unchanged-text suppression: if the hash equals the hash of the last call for that pane,
  the classifier makes no new call. It returns the cached result for that hash.

## Compact, redacted text

1. Take the last N lines (default 40) of the capture, after ghost removal.
2. Strip ANSI escapes and trailing blanks. Collapse runs of blank lines.
3. Redact before the text leaves the process: API key shapes (`sk-...`, `ghp_...`,
   `github_pat_...`, `xox[bap]-...`, `AKIA...`), `Authorization:` and `Bearer ...`
   values, `KEY=value` / `TOKEN=value` / `SECRET=value` / `PASSWORD=value` pairs, private
   key blocks, and email addresses. Each match becomes `[REDACTED:<kind>]`.
4. Cap the result at `max_chars` (default 4000).
5. Logs and events record only the hash, the length, and the redaction counts. They
   never record the text.

## Candidate provider contract (from public docs, NOT confirmed)

No project note names the JEV provider. Public docs describe a product named "Jev" by
TypeSafe AI (typesafe.ai), a "System One" decision model that returns typed answers with
probabilities. It fits the issue (low cost, text in, probabilities out). The maintainer
must confirm that this is the intended JEV (open question 1).

Sources, read 2026-11-17 (third-party pages and gateway docs; docs.typesafe.ai was not
read directly):
- AIMLAPI reference: https://docs.aimlapi.com/api-references/decision-models/typesafe/jev
- OpenRouter blog: https://openrouter.ai/blog/insights/what-is-jev/
- https://flaviocopes.com/jev/ and https://flaviocopes.com/jev-confidence/
- https://opentweet.io/jev/choice-score-noul

### Routes

| Route       | Endpoint                                     | Auth header                    | Key env var (SDK)   |
|-------------|----------------------------------------------|--------------------------------|---------------------|
| TypeSafe    | `POST https://api.typesafe.ai/v1/systemone`  | `Authorization: Bearer <key>`  | `TYPESAFE_API_KEY`  |
| OpenRouter  | `POST https://openrouter.ai/api/alpha/decisions` | `Authorization: Bearer <key>` | `OPENROUTER_API_KEY` |
| AIMLAPI     | `POST https://api.aimlapi.com/v1/decisions`  | `Authorization: Bearer <key>`  | (none named)        |

This is not an OpenAI-compatible chat API. The body is not `messages`.

### Request (shape common to the routes)

    {
      "model": "<model id>",
      "state": "<compact redacted screen text>",
      "questions": {
        "readiness": {
          "type": "choice",
          "instructions": "Is this terminal agent waiting for a new task, waiting for an answer to a question, or still working?",
          "criteria": {
            "ready": "The input prompt is empty and the agent waits for a new instruction.",
            "question_pending": "The agent asks the user a question or shows a menu or a permission dialog.",
            "busy": "The agent is working, or the input box holds typed text."
          }
        }
      }
    }

- `questions` keys are local IDs and are not sent to the model as text.
- Limits (stated): about 64k tokens per request by default. No per-string limit is stated.

### Response

    {
      "model": "<resolved model id>",
      "answers": {
        "readiness": {
          "type": "choice",
          "choice": "ready",
          "probabilities": {"ready": 0.91, "question_pending": 0.06, "busy": 0.03},
          "confidence": 0.86
        }
      },
      "usage": {"input_tokens": 512, "output_tokens": 4}
    }

(Values above are illustrative. The field names are from the sources.)

- `probabilities`: one number per option. They sum to 1. The sources say they are
  calibrated in aggregate, not per answer.
- `confidence`: 0 to 1, given only for `choice` and `score`. Sources disagree on its
  meaning: AIMLAPI calls it "calibrated confidence"; OpenRouter and others say it is
  derived from the shape of `probabilities`. The formula is not published.
- `usage`: `input_tokens`, `output_tokens` on all routes. OpenRouter adds `usage.cost`.
  AIMLAPI adds `meta.usage.usd_spent` and `meta.usage.credits_used`.

### Model IDs (stated)

- TypeSafe: `jev-latest` (floating), `jev-1.14.0`, `jev-1.13.0`, `jev-preview`.
- OpenRouter: `typesafe/jev-1.14`, `typesafe/jev-1.13`, `~typesafe/jev-latest`.
- AIMLAPI: `typesafe/jev`, `typesafe/jev-1.14`, `typesafe/jev-1.13`.

The design pins a versioned ID, never a floating alias, so the thresholds stay valid.

### Errors (stated for the TypeSafe route only)

401 bad or missing key, 422 validation error, 429 rate limit, 529 overload. The error
body format is not stated. Every error and every non-2xx status gives `unknown`. The
classifier does not retry inside a tick. The next changed screen can try again, after a
backoff for 429 and 529.

### Cost and access (stated, to be confirmed)

- Price on TypeSafe and OpenRouter: USD 0.042 per million input tokens, output free.
  A 1000-token screen costs about USD 0.000042.
- Access to the TypeSafe route: new sign-ups were paused on 2026-09-22 (per one source).
  OpenRouter needs only an OpenRouter key.
- Data: TypeSafe says it does not train on requests; zero retention is for enterprise
  customers. Retention on the gateways is not stated.

## Decision rule

Given `probabilities` and `confidence` from a `choice` answer:

    top = max(probabilities)
    if top >= min_probability and (confidence is None or confidence >= min_confidence):
        label = choice
    else:
        label = unknown

Defaults: `min_probability = 0.85`, `min_confidence = 0.70`. Both are configurable. If
the provider gives no `confidence`, only `min_probability` applies, and the result says
`confidence: null` (not supported), never a made-up number.

## Configuration

In `config.toml` (new `[readiness]` table; all off by default):

    [readiness]
    model_fallback = false          # the only switch that allows provider calls
    provider = "mock"               # "mock" | "typesafe" | "openrouter" | "aimlapi"
    endpoint = ""                   # empty: the route default above
    model = ""                      # a pinned version, required for a live provider
    api_key_env = "TYPESAFE_API_KEY"  # the NAME of the env var, never the key
    min_probability = 0.85
    min_confidence = 0.70
    max_age_s = 60
    max_lines = 40
    max_chars = 4000
    timeout_s = 3

- The key is read from the environment at call time only. It is never in a file, a log,
  an event, an exception message, or a repr.
- If the env var is missing, the result is `unknown` with reason `no_credentials`. No call.

## Counters and cost

Per provider and per day, in the event DB:
- `requests`, `errors` (by kind), `suppressed_unchanged`, `suppressed_stale`,
  `deterministic_answered`.
- `input_tokens`, `output_tokens`: from `usage`, labeled `provider`.
- `cost_usd`: from the provider when it gives one (`usage.cost`, `meta.usage.usd_spent`),
  labeled `provider`. Else an estimate from the configured price, labeled `estimate`.
  If neither is possible, `unavailable`. A missing metric is never shown as zero.

## Result record

    {
      "label": "ready" | "question_pending" | "busy" | "unknown",
      "source": "deterministic" | "model" | "cache" | "none",
      "reason": "<short code>",       # e.g. stale, below_threshold, http_429, no_credentials
      "probability": 0.91 | null,
      "confidence": 0.86 | null,
      "screen_hash": "<sha1>",
      "model": "<resolved id>" | null,
      "advisory": true
    }

## Mock provider and tests (no paid calls)

A `MockProvider` takes a function from compact text to a canned response body, or an
exception. All tests use it, or a fake HTTP transport. No test opens a socket.

Tests to write when implementation is authorized:

| Case                  | Check |
|-----------------------|-------|
| busy                  | spinner screen: deterministic `busy`, no provider call |
| idle                  | blank prompt: deterministic `ready`, no provider call |
| question pending      | dialog / question line: deterministic `question_pending`, no call |
| ghost autocomplete    | dim suggestion in an empty box: `ready`; the ghost text is not in the provider input |
| typed input           | real text in the box: `busy` |
| stale output          | old capture: `unknown`, reason `stale`, no call; old cached `ready` is not reused for a new hash |
| unchanged text        | same hash twice: one provider call |
| thresholds            | same response, two configs: label vs `unknown` |
| uncertainty           | low top probability or low confidence: `unknown` |
| provider errors       | 401, 422, 429, 529, timeout, connection error, bad JSON: `unknown`, error counter up |
| redaction             | keys, tokens, emails in the screen: absent from the provider input and from logs |
| no secret logging     | capture logs with a fake key in env: the key does not appear anywhere |
| advisory only         | the classifier has no path to push, merge, dispatch, restart, or type |
| off by default        | `model_fallback = false`: no provider is built |

## Open questions (for the maintainer)

1. Is "JEV" the TypeSafe AI "Jev" decision model? If not, which provider and docs?
2. Which route: TypeSafe direct, OpenRouter, or AIMLAPI? Do you have an account and key
   on it? (TypeSafe direct paused new sign-ups on 2026-09-22, per one source.)
3. Which env var holds the key on this host? The design reads only the name from config.
4. Which pinned model: `jev-1.13.0` or `jev-1.14.0` (or the gateway form)?
5. May the next PR build the mock provider, the classifier, and the tests above, with
   `model_fallback = false` by default and no live calls?
