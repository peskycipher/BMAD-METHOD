# PR description (prepared, not submitted)

> Upstream submission prerequisite: BMAD's CONTRIBUTING.md asks that changes
> which add features or touch more than a couple of files be confirmed with a
> maintainer first (Discord). **No maintainer agreement has been sought or
> obtained.** This description is prepared for later review only.

**Title:** `feat(bmad): optional advisory Jev workflow recommendation in help (off by default)`

## Problem

BMAD's help flow routes on knowledge documents and, when several installed
skills remain genuinely plausible, falls back to the host model's judgment
alone. There is no cheap, structured second opinion at that one decision
point, and no way to gather comparison data about how such a decision point
behaves.

## Behavior change

Adds an opt-in, disabled-by-default advisory helper for the help flow's
next-skill recommendation:

- `skills/bmad/scripts/jev_adapter.py` — bounded client for TypeSafe's Jev
  decision model via OpenRouter's decisions endpoint
  (`/api/alpha/decisions`, native TypeSafe `{state, questions} →
  {answers, usage}` contract). Strict response validation (question ids,
  answer types, numeric ranges, option membership, probability sums), 8 s
  timeout, per-process call budget (4), one retry on 429/5xx, one-line
  stderr metadata logging with no secrets or content. Only credential:
  `OPENROUTER_API_KEY`. Model pinned to `typesafe/jev-1.13-20260917`
  (verified live 2026-09-19), overridable via `BMAD_DECISION_ASSIST_MODEL`.
- `skills/bmad/scripts/jev_policy.py` — one `choice` question over the
  caller-derived candidates plus an explicit `unsure` outcome; conservative
  provisional confidence threshold (0.60); explicit user choices always win.
- `skills/bmad/scripts/jev_recommend.py` — CLI entry point; prints a JSON
  advisory; never launches a workflow, never writes files.
- `skills/bmad/SKILL.md` — a concise hook after the existing discovery and
  knowledge-loading steps; on any non-`ok` status the flow continues
  silently with ordinary reasoning.
- `references/jev-decisions.md`, `references/jev-eval-set.md` — setup, data
  handling, fallback behavior, small evaluation set with recorded bounded
  live results.

Configuration: `BMAD_DECISION_ASSIST_MODE=off|shadow|suggest` (default
`off`; zero network calls when off), `BMAD_DECISION_ASSIST_MODEL`, or the
`[jev]` central-config table. `shadow` mode evaluates without influencing
the user-facing recommendation.

## Preserved compatibility

- Diff is purely additive (no existing lines removed); all existing
  commands, skill names, invocation patterns, menus, workflow order,
  routing rules, halt conditions, approval checkpoints, artifact formats
  and status transitions are untouched.
- Explicit user choices and single-candidate cases never reach the
  provider; explicit choices bypass it entirely (zero calls).
- Uncertain results fall back to the existing ask/reason path; confidence
  is labeled a distribution-concentration statistic, never a probability of
  correctness, and never overrides workflow constraints.
- No new user-facing command, no cached discovery, no telemetry files, no
  setup/doctor/installation side effects, no automatic workflow launch, no
  dependency installation during help.
- Works with no Jev configuration, no API key, and no additional setup;
  standalone skills do not depend on the hub or Jev.

## Test evidence

- 47 offline deterministic tests (injected transport + local mock server):
  disabled mode zero calls, missing credentials, explicit-request bypass,
  unsure/low-confidence fallback, unknown/invalid candidate ids, malformed
  and incomplete responses, out-of-range and non-summing probabilities,
  unsupported answer types, provider errors/timeouts fallback, retry
  budget, secret-free diagnostics, shadow mode tagging, unknown mode → off,
  and no help-time artifact writes.
- Full quality gate on the pushed HEAD:
  `uv sync --frozen && (cd docs-site && npm ci) && uv run --frozen tools/quality.py`
  — all checks passed (includes `tools/validate_skills.py --strict`).
- Live contract verification (bounded, 2 synthetic probes): pinned model
  responds with valid, validated decisions in ~560 ms p50, ~$0.00002/call.

## Performance limitations (honest status)

- The full acceptance evaluation (unmodified BMAD vs. deterministic
  baseline vs. Jev-assisted; ≥100 held-out cases; ≥95 % suggestion
  precision; ≥20 % median latency improvement; ≤5 % p95 regression; no
  completion-quality regression in human-reviewed cases) is **pending** and
  requires an evaluation driver plus human review.
- The small recorded eval set (5 recommendation cases + 1 synthetic probe)
  behaved conservatively and matched expectations, but is far too small to
  claim model-quality success.
- Therefore the feature ships **experimental and off by default**; the
  threshold is provisional and no success is claimed.