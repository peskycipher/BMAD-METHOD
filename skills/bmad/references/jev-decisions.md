# Optional Jev Decision Support

Advisory, opt-in decision support for the BMad help flow, powered by
TypeSafe's Jev decision model accessed **exclusively through OpenRouter**.
Disabled by default: with no configuration, this feature makes zero network
calls and the help flow behaves exactly as before.

## What it is

Two narrow, bounded judgments are wired into the help flow:

1. **Workflow recommendation** (`jev_recommend.py`) — when the ordinary
   knowledge-document routing leaves several installed skills genuinely
   plausible, Jev picks among the candidate ids the help flow itself derived.
   Explicit user choices and single-candidate cases never reach the provider.
2. **Clarification triage** (`jev_clarify.py`) — judges whether missing
   information (intended outcome, scope boundaries, acceptance criteria,
   consequential constraints) would materially change the next action. It
   reports the material gap; the primary reasoning model writes the actual
   question, and "proceed" outcomes let the flow act on reasonable, reversible
   assumptions instead of asking.

Everything is advisory. Script results never override an explicit user
instruction, a knowledge-document route, or an existing approval checkpoint.

## Roles and models

- The primary agent (conversation, reasoning, planning, code generation)
  runs on the host's configured model — for pi.dev, GLM-5.3-flash through
  Ollama Cloud. That configuration belongs to the host, not to BMad; other
  hosts and models work unchanged.
- Jev decisions go through OpenRouter's decisions endpoint,
  `https://openrouter.ai/api/alpha/decisions`, using the native TypeSafe
  request/response contract. The only credential is the
  `OPENROUTER_API_KEY` environment variable. No TypeSafe-direct endpoint
  (`api.typesafe.ai`) is ever contacted and no separate TypeSafe key is
  needed.

## Enabling

1. Export the key: `export OPENROUTER_API_KEY=...` (reuse an existing key).
2. Enable the integration, either:
   - environment: `export BMAD_JEV_ENABLED=1`, or
   - config: add to a central config layer, e.g.
     `{project-root}/_bmad/custom/config.user.toml`:

     ```toml
     [jev]
     enabled = true
     # optional overrides (defaults shown)
     model = "typesafe/jev-1.13"
     endpoint = "https://openrouter.ai/api/alpha/decisions"
     timeout_seconds = 8.0
     max_state_chars = 4000
     ```

3. Verify: `uv run <skill-root>/scripts/jev_recommend.py --request "test" --candidates "a,b"` —
   expect `{"status": "disabled", ...}` before enabling and a JSON outcome
   with `status: ok` or `unavailable` after.

Model identifiers are configurable; only `typesafe/jev-1.13` and the
`~typesafe/jev-latest` alias are verified through OpenRouter. Do not point
`endpoint` at a TypeSafe-direct URL — the adapter's contract is OpenRouter's.

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

- A `choice`/`score` answer's `confidence` is the provider's
  distribution-concentration statistic. It is **not** a measured probability
  of correctness and must not be described as one.
- `noul` answers carry only a yes-probability; there is no separate
  confidence field, and none is invented.
- Every response is validated (answer ids, types, numeric ranges, option
  membership, probability sums). Invalid or incomplete responses yield an
  explicit `unavailable` outcome and the ordinary path resumes.
- Thresholds (`RECOMMEND_CONFIDENCE_THRESHOLD = 0.60`,
  `CLARIFY_NOUL_THRESHOLD = 0.70`) are provisional until evaluated against
  project examples; they are deliberately conservative so ambiguous cases
  return to the primary model or the user.
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

## Known limitations

- OpenRouter's decisions endpoint is alpha and currently undocumented in
  OpenRouter's docs; Jev also does not appear in `/v1/models`. The endpoint
  and schema were verified live (`typesafe/jev-1.13`, resolves to
  `typesafe/jev-1.13-20260917`); treat future contract changes as possible
  and rely on validation + fallback.
- Through OpenRouter the native TypeSafe `state` accepts strings and
  structured records; only the string form is exercised here. Usage reports
  include a `cost` field, which the native docs do not mention.
- No streaming, no batching of dependent questions (dependent judgments must
  run in separate calls), no caching.
- Recommendation quality depends on the candidate list the help flow derives;
  Jev can only choose among supplied options.