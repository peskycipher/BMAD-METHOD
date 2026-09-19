# Jev Decision Evaluation Set

Small evaluation set for the optional Jev integration: expected outcomes
recorded per case, plus the bounded live results observed against
`typesafe/jev-1.13-20260917` through OpenRouter on 2026-09-19 (6 decision
calls, ~$0.0002 total). These are examples to re-evaluate, not permanent
thresholds. Mocked tests prove the adapter mechanics; only these live runs
are model-quality evidence.

## Recommendation cases (Priority A)

| Case | Request | Candidates | Expected |
| --- | --- | --- | --- |
| straightforward | "implement story 2.3 from the sprint status file; the spec is ready" (+ evidence: spec exists and is approved) | bmad-build, bmad-spec, bmad-review | `bmad-build`, or `uncertain` if spread |
| ambiguous | "help me figure out what to build next" (+ evidence: no planning artifacts, half-formed idea) | bmad-spec, bmad-brainstorming, bmad-product-brief | any shaping skill (`bmad-brainstorming` / `bmad-spec` / `bmad-product-brief`) or `uncertain` |
| conflicting | "quick tweak to the deploy script" (+ evidence: the last such request grew into an epic-sized initiative) | bmad-build, bmad-spec | `uncertain` (conflict must not be resolved silently) |
| insufficient | "do the usual" | bmad-build, bmad-spec, bmad-review | `uncertain` / `unsure` (abstain rather than pick) |

Recorded live outcomes (2026-09-19, jev-1.13-20260917):

- **straightforward** → `uncertain` (`confidence_below_threshold`; top option
  `bmad-build` 0.34, unsure 0.33, bmad-spec 0.30). The conservative threshold
  refused to act on a three-way spread; the help flow falls back to asking or
  reasoning. Acceptable per expectations, though a higher threshold than
  Jev's hedging — evidence that 0.60 confidence is the right conservative
  default for this domain.
- **ambiguous** → `ok`, `bmad-brainstorming` (confidence 0.88). Matches the
  expected set.
- **conflicting** → `uncertain` (`model_returned_unsure`, unsure 0.43 vs
  bmad-build 0.40). Returns to the primary model/user as expected.
- **insufficient** → `uncertain` (`model_returned_unsure`, unsure 0.98).
  Abstains as expected.

## Clarification triage cases (Priority B)

| Case | Request | Context supplied | Expected |
| --- | --- | --- | --- |
| vague | "improve the wizard" | none | `ask`, with a material gap named |
| clear | "fix the typo in README line 3 that says 'reprovision' instead of 're-provision'" | scope: single word on one line; outcome: typo corrected | `proceed` (no material gap) |

Recorded live outcomes (2026-09-19):

- **vague** → `ask`; gaps: `outcome_missing` 0.86, `scope_missing` 0.82,
  `acceptance_missing` 0.76; evidence_sufficient 0.05. Matches expectations.
- **clear** → `proceed`; no gaps; evidence_sufficient 0.61. Matches
  expectations.

## Interpretation notes

- All four non-straightforward recommendation cases behaved conservatively;
  the one confident pick (ambiguous case) was correct for the expected set.
- The `straightforward` case shows Jev hedges across neighbouring workflow
  choices even when the answer seems obvious to a human reader — reinforcing
  that results stay advisory and the ordinary reasoning path remains the
  default when the outcome is `uncertain`.
- One process-level observation: the adapter's default per-run call budget
  (4) is sized for a single decision point per skill invocation, not for
  batch evaluation — evaluation drivers should use one client per case group
  (this is how the budget behaved during the recorded run).