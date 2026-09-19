# Optional Jev Decision Support

Advisory, opt-in decision support for the BMad help flow, powered by
TypeSafe's Jev decision model accessed **exclusively through OpenRouter**.
Disabled by default: with no configuration, this feature makes zero network
calls and the help flow behaves exactly as before.

## What it is

One narrow, bounded judgment is wired into the help flow:

- **Workflow recommendation** (`jev_recommend.py`) — when the ordinary
  knowledge-document routing leaves several installed skills genuinely
  plausible, Jev picks among the candidate ids the help flow itself derived.
  Explicit user choices and single-candidate cases never reach the provider.

Everything is advisory. Script results never override an explicit user
instruction, a knowledge-document route, or an existing approval checkpoint.

### Deliberately out of scope

This pilot intentionally does **not** touch planning, readiness gates,
context filtering, code review, or autonomous workflow execution, and does
not add a second decision point (such as clarification triage). Those remain
possible future work and would need their own scoped design, evaluation, and
compatibility review.

## Roles and models

- The primary agent (conversation, reasoning, planning, code generation)
  runs on the host's configured model. That configuration belongs to the
  host, not to BMad; no host or model is required or changed by this feature.
- Jev decisions go through OpenRouter's decisions endpoint,
  `https://openrouter.ai/api/alpha/decisions`, using the native TypeSafe
  request/response contract. The only credential is the
  `OPENROUTER_API_KEY` environment variable. No TypeSafe-direct endpoint
  (`api.typesafe.ai`) is ever contacted and no separate TypeSafe key is
  needed.
- The adapter validates all three Jev answer primitives — `noul` (yes/no
  probability), `choice` (option pick with distribution), and `score`
  (ordered rubric position with distribution and confidence). The wired
  workflow currently uses only `choice`; the other two are available to
  future decision points and rejected nowhere (a question of an unsupported
  type still yields an explicit `unavailable` outcome).

## Modes and enabling

`BMAD_DECISION_ASSIST_MODE` (or the `[jev] mode` central-config key) selects
the behavior:

- `off` (**default**) — existing BMad behavior; the scripts make zero
  network calls and print an explicit `disabled` status.
- `shadow` — the script evaluates and returns a full result, but the help
  flow must not let it influence the user-facing recommendation. Use shadow
  mode to gather comparison data against the unmodified flow.
- `suggest` — the script returns a bounded advisory recommendation that the
  help flow assesses under its existing rules (one weighted vote, trivially
  overridden). The recommendation call batches three independent questions
  over the same state: a `choice` pick among the candidates (with an explicit
  `unsure` outcome), a `noul` yes/no gate on whether any candidate clearly
  fits, and a `score` position on the ordered rubric ["no clear fit",
  "partial fit", "clear fit"]. A recommendation surfaces only when all three
  signals agree (pick is a real candidate, noul ≥ 0.50, score ≥ 1.50,
  confidence ≥ 0.60 — thresholds provisional); any disagreement is a
  conservative `uncertain` outcome and the ordinary path resumes.

To enable:

1. Export the key: `export OPENROUTER_API_KEY=...` (reuse an existing key).
2. Select the mode, either:
   - environment: `export BMAD_DECISION_ASSIST_MODE=suggest` (or `shadow`), or
   - config: add to a central config layer, e.g.
     `{project-root}/_bmad/custom/config.user.toml`:

     ```toml
     [jev]
     mode = "suggest"
     # optional overrides (defaults shown)
     model = "typesafe/jev-1.13-20260917"
     endpoint = "https://openrouter.ai/api/alpha/decisions"
     timeout_seconds = 8.0
     max_state_chars = 4000
     ```

3. Verify: `uv run <skill-root>/scripts/jev_recommend.py --request "test" --candidates "a,b"` —
   expect `{"status": "disabled", ...}` before enabling and a JSON outcome
   with `status: ok` or `unavailable` after.

The model is pinned to the dated snapshot `typesafe/jev-1.13-20260917` for
reproducible evaluation (verified live through OpenRouter on 2026-09-19).
Override it with `BMAD_DECISION_ASSIST_MODEL` or the `[jev] model` key; do
not point `endpoint` at a TypeSafe-direct URL — the adapter's contract is
OpenRouter's.

## Data handling and privacy

- Each decision sends only the request text (truncated to 600 chars) and the
  bounded evidence items the caller passes explicitly (12 items, 300 chars
  each), assembled into a state capped at `max_state_chars` (4000).
- The calling skill must never pass credentials, environment-file contents,
  or unrelated private content as evidence. Nothing else — files, config,
  conversation history — is transmitted.
- Operational logs go to stderr as one JSON line per call (`op`,
  `duration_ms`, `outcome`, `fallback_reason`, provider-reported `usage`).
  Logs never contain secrets or project content.
- If sufficient evidence cannot fit the cap, the caller is expected to
  abstain rather than decide from incomplete context; the adapter truncates
  and the skill guidance treats over-long evidence as insufficient.

## Handling of uncertainty

- A `choice` answer's `confidence` is the provider's
  distribution-concentration statistic. It is **not** a measured probability
  of correctness and must not be described as one.
- Every response is validated (question ids, answer types, numeric ranges,
  option membership, probability sums). Invalid or incomplete responses
  yield an explicit `unavailable` outcome and the ordinary path resumes.
- The threshold (`RECOMMEND_CONFIDENCE_THRESHOLD = 0.60`) is provisional
  until evaluated against project examples; it is deliberately conservative
  so ambiguous cases return to the primary model or the user.
- Recommendations are advisory votes for reversible, low-impact navigation
  only. High confidence never approves implementation, rejects an artifact,
  or bypasses a checkpoint.

## Failure behaviour

Any of these returns an explicit non-`ok` status and the flow continues with
its ordinary reasoning — no partial state, no retries by the agent:

- disabled (`disabled_by_config`), missing key (`missing_openrouter_api_key`)
- timeouts, rate limits, outages (`http_<code>`), invalid responses
  (`invalid_json_response`, per-field validation reasons)
- exhausted per-process call budget (`call_budget_exhausted`, default 4)

One bounded retry (with short backoff) is attempted for HTTP 429/5xx within
the budget; nothing else is retried.

## Verification and evaluation

- Offline tests: `skills/bmad/scripts/tests/test_jev_*.py`
  (`uv run pytest skills/bmad/scripts/tests/test_jev_adapter.py` etc.).
  They use an injected transport or a local mock server — no network.
- A small evaluation set lives in `references/jev-eval-set.md` with expected
  recommendations or acceptable outcome sets per case. Live model-quality
  results recorded there are evidence; mocked tests are not.
- Full before/after evaluation (unmodified BMAD vs. deterministic baseline
  vs. Jev-assisted, ≥100 held-out cases, precision/latency/cost targets) is
  **pending**: run shadow mode plus an evaluation driver to produce it. The
  feature stays experimental and off by default until those gates pass.

## Known limitations

- OpenRouter's decisions endpoint is alpha and currently undocumented in
  OpenRouter's docs; Jev also does not appear in the public `/v1/models`
  listing. The endpoint and schema were verified live
  (`typesafe/jev-1.13-20260917`); treat future contract changes as possible
  and rely on validation + fallback.
- Through OpenRouter the native TypeSafe `state` accepts strings and
  structured records; only the string form is exercised here. Usage reports
  include a `cost` field, which the native docs do not mention.
- No streaming, no batching of dependent questions (dependent judgments must
  run in separate calls), no caching.
- Recommendation quality depends on the candidate list the help flow derives;
  Jev can only choose among supplied options.
- Live evaluation is pending (see above); the recommendation threshold is
  provisional and the feature is experimental.