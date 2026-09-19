#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ////
"""Decision-policy layer for Jev readiness gates and story review.

Two additional advisory decision points for the BMad flow, sharing the
jev_adapter provider:

- **Readiness gate** (`interpret_readiness`): before a BMad phase
  transition, judge whether the upstream artifact is ready — concrete
  requirements, testable acceptance criteria, no explicit unresolved
  blocking decisions — plus an ordered readiness score and a blocker
  taxonomy when it fails.
- **Story review** (`interpret_review`): after an implementation, judge a
  story/implementation pair against a bounded rubric — three noul gates
  (spec satisfied, no regression, no security violation), four scored
  dimensions on a 2-10 scale, and a failure taxonomy.

This layer never talks to the network; it consumes `JevResult` values
produced by jev_adapter and returns explicit, bounded outcomes the calling
skill must treat as advisory signals. A `proceed` verdict is one vote, not
an approval; a `hold`/`rework` verdict is a signal to surface the recorded
reasons to the user, never an automatic checkpoint bypass or block.

Thresholds are provisional-but-calibrated: fitted on 2026-09-19 against the
jev-BMAD golden sets (302 labeled examples; readiness n=8, story review
n=10; model `typesafe/jev-1.13-20260917` via OpenRouter) and re-fit when the
pinned model changes. Confidence is the model's distribution-concentration
statistic, NOT a measured probability of correctness.
"""

from __future__ import annotations

from typing import Any

# --- Readiness gate thresholds (fitted 2026-09-19, readiness golden set) ---
READINESS_SPECIFIC_THRESHOLD = 0.40
READINESS_TESTABLE_THRESHOLD = 0.45
READINESS_BLOCKERS_THRESHOLD = 0.55
READINESS_SCORE_MIN = 3.0  # levels 0-4; 3 == "ready"

# --- Story review thresholds (fitted 2026-09-19, story_review golden set) ---
REVIEW_SPEC_THRESHOLD = 0.75
REVIEW_REGRESSION_THRESHOLD = 0.65
REVIEW_SECURITY_THRESHOLD = 0.85
REVIEW_DIM_PASS = 5.0  # on the 2-10 scale (raw rubric position + 2)

TRANSITIONS = ("analysis_to_planning", "planning_to_solutioning", "solutioning_to_implementation")

MAX_ARTIFACT_CHARS = 6000
MAX_STORY_CHARS = 4000
MAX_IMPLEMENTATION_CHARS = 6000

DIMENSION_KEYS = ("dim_correctness", "dim_quality", "dim_tests", "dim_bmad")

_DIM_LEGEND = [
    "2 - unacceptable: wrong or missing",
    "3 - poor: major gaps or flaws",
    "4 - below bar: several notable issues",
    "5 - mediocre: meets some expectations",
    "6 - adequate: minor issues only",
    "7 - good: solid, ready with trivial nitpicks",
    "8 - strong: clean and well-considered",
    "9 - excellent: thorough, edge cases handled",
    "10 - exemplary: nothing to improve",
]


class PolicyError(ValueError):
    """The caller supplied input the policy layer cannot act on."""


def validate_transition(raw: str) -> str:
    """Validate one BMad phase-transition id."""
    transition = raw.strip()
    if transition not in TRANSITIONS:
        raise PolicyError(f"unknown transition {transition!r}; use one of {TRANSITIONS}")
    return transition


def validate_text(raw: str, what: str, max_chars: int) -> str:
    """Bound and validate caller-supplied text. Text is data, never commands."""
    if raw is None or not raw.strip():
        raise PolicyError(f"{what} is empty")
    return raw[:max_chars]


def build_readiness_questions() -> dict[str, dict[str, Any]]:
    """The batched readiness-gate questions (one provider call).

    Work that is simply not done yet does not count as a blocker; only
    explicit unresolved decisions (TODOs, open either/or choices, missing
    required content) do. The `blocker_kind` choice is only meaningful when
    a gate fails; `none_applicable` otherwise.
    """
    return {
        "spec_specific": {
            "type": "noul",
            "instructions": (
                "Does the artifact state concrete, specific requirements — "
                "named components, expected behavior, and scope — rather than "
                "vague goals or background material?"
            ),
            "proposition": "The artifact states concrete, specific requirements.",
        },
        "requirements_testable": {
            "type": "noul",
            "instructions": (
                "Are the acceptance criteria verifiable — could a reviewer or "
                "automated test objectively confirm each one (given inputs, "
                "expected outputs, measurable conditions)?"
            ),
            "proposition": "The acceptance criteria are objectively verifiable.",
        },
        "no_blockers": {
            "type": "noul",
            "instructions": (
                "Work that is simply not done yet does NOT count as a blocker. "
                "Count only explicit unresolved blocking decisions: TODOs, open "
                "either/or choices, or required content that is missing or only "
                "referenced but absent. A document that is entirely absent or "
                "placeholder counts as a blocker."
            ),
            "proposition": "There are no explicit unresolved blocking decisions.",
        },
        "ready_score": {
            "type": "score",
            "instructions": "Rate the overall readiness of this artifact for the next BMAD phase.",
            "criteria": [
                "0 - not ready: background material, goals, or discussion with no actionable requirements. NOT for: a concrete task list (that is at least 1).",
                "1 - weak: some concrete requirements but vague scope, untestable criteria, or open blockers remain.",
                "2 - partial: mostly concrete and testable, but at least one significant gap (one gate fails, one criterion unmeasurable).",
                "3 - ready: concrete requirements, objectively testable criteria, no open blockers; minor polish still possible.",
                "4 - exemplary: concrete, testable, complete, AND edge cases and failure modes explicitly addressed.",
            ],
        },
        "blocker_kind": {
            "type": "choice",
            "instructions": (
                "If any readiness gate failed, classify the primary blocker. "
                "If all gates passed, choose none_applicable."
            ),
            "criteria": {
                "scope_vague": "Requirements are vague, generic, or missing scope boundaries",
                "criteria_untestable": "Acceptance criteria cannot be objectively verified",
                "open_questions": "Unresolved blocking decisions or TODOs remain",
                "missing_artifact": "The artifact itself is empty, missing, or not provided",
                "none_applicable": "No gate failed; the artifact is ready to proceed",
                "other": "None of the listed categories fit",
            },
        },
    }


def build_readiness_state(artifact: str, transition: str) -> str:
    """Assemble the bounded readiness state. Artifact text is data only."""
    return (
        f"BMAD phase transition under review: {transition}\n\n"
        f"Upstream artifact:\n\"\"\"\n{artifact}\n\"\"\""
    )


def interpret_readiness(result: Any) -> dict[str, Any]:
    """Turn an adapter result into an explicit readiness outcome.

    `verdict` is "proceed" only when every noul gate clears its threshold
    and the readiness score reaches READINESS_SCORE_MIN. Any failure yields
    "hold" with the primary blocker taxonomy. Non-ok adapter statuses pass
    through unchanged; the caller falls back to ordinary reasoning.
    """
    if result is None:
        return {"status": "unavailable", "reason": "no_result"}
    if result.status != "ok":
        return {"status": result.status, "reason": result.reason}

    answers = result.answers
    gates_failed, reasons = [], []
    for gate, threshold in (
        ("spec_specific", READINESS_SPECIFIC_THRESHOLD),
        ("requirements_testable", READINESS_TESTABLE_THRESHOLD),
        ("no_blockers", READINESS_BLOCKERS_THRESHOLD),
    ):
        answer = answers.get(gate)
        if answer is None or answer.get("type") != "noul":
            return {"status": "unavailable", "reason": f"missing_or_invalid_answer_{gate}"}
        if answer["noul"] < threshold:
            gates_failed.append(gate)
            reasons.append(f"{gate} {answer['noul']:.2f} < {threshold}")
    score_answer = answers.get("ready_score")
    if score_answer is None or score_answer.get("type") != "score":
        return {"status": "unavailable", "reason": "missing_or_invalid_answer_ready_score"}
    score = score_answer["score"]
    if score < READINESS_SCORE_MIN:
        gates_failed.append("ready_score")
        reasons.append(f"ready_score {score:.2f} < {READINESS_SCORE_MIN}")

    verdict = "hold" if gates_failed else "proceed"
    blocker_kind = None
    if verdict == "hold":
        blocker_answer = answers.get("blocker_kind")
        blocker_kind = (blocker_answer or {}).get("choice") if blocker_answer else None
        if blocker_kind in (None, "none_applicable"):
            blocker_kind = "other"  # gates failed but no listed blocker named

    return {
        "status": "ok",
        "verdict": verdict,
        "blocker_kind": blocker_kind,
        "gates_failed": gates_failed,
        "reasons": reasons,
        "ready_score": score,
        "gate_nouls": {
            gate: answers[gate]["noul"]
            for gate in ("spec_specific", "requirements_testable", "no_blockers")
        },
    }


def build_review_questions() -> dict[str, dict[str, Any]]:
    """The batched story-review questions (one provider call).

    Three noul gates, four scored dimensions on a 2-10 scale (rendered as a
    9-level rubric; the raw position maps to the scale by +2), and a
    failure taxonomy that is only meaningful when a gate fails.
    """
    questions: dict[str, dict[str, Any]] = {
        "gate_spec": {
            "type": "noul",
            "instructions": (
                "Judging only from the implementation description: does it "
                "satisfy every acceptance criterion in the story, with nothing "
                "missing and no substitutions (e.g. a different output format "
                "or omitted columns count as unsatisfied)?"
            ),
            "proposition": "As described, the implementation satisfies the story.",
        },
        "gate_no_regression": {
            "type": "noul",
            "instructions": (
                "Judging only from the implementation description: does it "
                "leave all existing behavior intact — no failing or skipped "
                "tests, no conflicting features, and no removed functionality "
                "are described? Judge the evidence in the text, not what might "
                "be hidden."
            ),
            "proposition": "As described, existing behavior remains intact.",
        },
        "gate_security": {
            "type": "noul",
            "instructions": (
                "Judging only from the implementation description: is it free "
                "of security or data-safety violations — no secret leaks, no "
                "unsafe commands, and no weakened validation or auth are "
                "described?"
            ),
            "proposition": "As described, the implementation is free of security or data-safety violations.",
        },
        "failure_kind": {
            "type": "choice",
            "instructions": (
                "If any gate failed, classify the primary failure. If all "
                "gates passed, choose none_applicable."
            ),
            "criteria": {
                "spec-misread": "The story requirements were misunderstood or misread",
                "partial-implementation": "Some required parts of the story were not implemented",
                "regression": "Existing behavior was broken",
                "architecture-violation": "The solution violates documented architecture constraints",
                "test-gap": "Tests are missing or inadequate for the implemented behavior",
                "environment": "Failure caused by environment/build issues, not the implementation",
                "none_applicable": "No gate failed",
                "other": "None of the listed categories fit",
            },
        },
    }
    for key, instruction in (
        ("dim_correctness", "Rate correctness and completeness of the implementation against the story (2-10 scale)."),
        ("dim_quality", "Rate code quality and maintainability of the implementation (2-10 scale)."),
        (
            "dim_tests",
            "Rate the adequacy of test coverage relative to what the story's risk actually requires "
            "(a low-risk change like a rename needs few tests; a multi-tenant isolation change needs many). 2-10 scale.",
        ),
        ("dim_bmad", "Rate BMAD compliance: correct artifacts updated, workflow gates followed, story conventions respected (2-10 scale)."),
    ):
        questions[key] = {"type": "score", "instructions": instruction, "criteria": list(_DIM_LEGEND)}
    return questions


def build_review_state(story: str, implementation: str) -> str:
    """Assemble the bounded story-review state. Text is data only."""
    return (
        f"User story:\n\"\"\"\n{story}\n\"\"\"\n\n"
        f"Implementation (code/diff/description):\n\"\"\"\n{implementation}\n\"\"\""
    )


def _scale_210(raw: float) -> float:
    """Map a 0-8 rubric position onto the 2-10 scale."""
    return raw + 2


def interpret_review(result: Any) -> dict[str, Any]:
    """Turn an adapter result into an explicit story-review outcome.

    `verdict` is "first_pass" only when all three gates clear their
    thresholds AND every dimension reaches REVIEW_DIM_PASS on the 2-10
    scale. Otherwise "rework" with the primary failure taxonomy. Non-ok
    adapter statuses pass through; the caller falls back to ordinary
    reasoning.
    """
    if result is None:
        return {"status": "unavailable", "reason": "no_result"}
    if result.status != "ok":
        return {"status": result.status, "reason": result.reason}

    answers = result.answers
    gates_failed, reasons = [], []
    for gate, threshold in (
        ("gate_spec", REVIEW_SPEC_THRESHOLD),
        ("gate_no_regression", REVIEW_REGRESSION_THRESHOLD),
        ("gate_security", REVIEW_SECURITY_THRESHOLD),
    ):
        answer = answers.get(gate)
        if answer is None or answer.get("type") != "noul":
            return {"status": "unavailable", "reason": f"missing_or_invalid_answer_{gate}"}
        if answer["noul"] < threshold:
            gates_failed.append(gate)
            reasons.append(f"{gate} {answer['noul']:.2f} < {threshold}")

    dimensions = {}
    for key in DIMENSION_KEYS:
        answer = answers.get(key)
        if answer is None or answer.get("type") != "score":
            return {"status": "unavailable", "reason": f"missing_or_invalid_answer_{key}"}
        score_210 = _scale_210(answer["score"])
        dimensions[key] = score_210
        if score_210 < REVIEW_DIM_PASS:
            gates_failed.append(key)
            reasons.append(f"{key} {score_210:.1f} < {REVIEW_DIM_PASS} (2-10 scale)")

    verdict = "rework" if gates_failed else "first_pass"
    failure_kind = None
    if verdict == "rework":
        failure_answer = answers.get("failure_kind")
        failure_kind = failure_answer.get("choice") if failure_answer else None
        if failure_kind in (None, "none_applicable"):
            failure_kind = "other"

    return {
        "status": "ok",
        "verdict": verdict,
        "failure_kind": failure_kind,
        "gates_failed": gates_failed,
        "reasons": reasons,
        "dimensions": dimensions,
        "gate_nouls": {gate: answers[gate]["noul"] for gate in ("gate_spec", "gate_no_regression", "gate_security")},
    }


__all__ = [
    "DIMENSION_KEYS",
    "MAX_ARTIFACT_CHARS",
    "MAX_IMPLEMENTATION_CHARS",
    "MAX_STORY_CHARS",
    "PolicyError",
    "READINESS_BLOCKERS_THRESHOLD",
    "READINESS_SCORE_MIN",
    "READINESS_SPECIFIC_THRESHOLD",
    "READINESS_TESTABLE_THRESHOLD",
    "REVIEW_DIM_PASS",
    "REVIEW_REGRESSION_THRESHOLD",
    "REVIEW_SECURITY_THRESHOLD",
    "REVIEW_SPEC_THRESHOLD",
    "TRANSITIONS",
    "build_readiness_questions",
    "build_readiness_state",
    "build_review_questions",
    "build_review_state",
    "interpret_readiness",
    "interpret_review",
    "validate_text",
    "validate_transition",
]