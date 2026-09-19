#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Decision-policy layer for optional Jev decision support.

Builds typed Jev questions (Priority A: workflow recommendation; Priority B:
clarification triage) and interprets validated answers under conservative,
provisional thresholds. This layer never talks to the network; it consumes
`JevResult` values produced by jev_adapter and returns explicit, bounded
outcomes the calling skill must treat as advisory signals.

Thresholds are provisional until evaluated against project examples (see
references/jev-decisions.md). All outcomes are advisory: an explicit user
choice or a knowledge-document route always outranks a model result.
"""

from __future__ import annotations

from typing import Any

# Provisional thresholds. Confidence is the model's distribution-concentration
# statistic, NOT a measured probability of correctness.
RECOMMEND_CONFIDENCE_THRESHOLD = 0.60
CLARIFY_NOUL_THRESHOLD = 0.70
CONFLICT_LOW = 0.50

UNSURE_OPTION = "unsure"
MAX_CANDIDATES = 8
MAX_EVIDENCE_ITEMS = 12
MAX_EVIDENCE_VALUE_CHARS = 300

# Clarification dimensions, each asked as one independent noul question:
# "would the missing information on this dimension materially change the
# next action?" A near-1 answer means the gap is material.
CLARIFY_DIMENSIONS = {
    "outcome_missing": "the intended outcome of the request",
    "scope_missing": "the boundaries of what is included and excluded",
    "acceptance_missing": "how the user will judge the result is done and correct",
    "constraint_missing": "a consequential constraint such as a deadline, budget, or hard dependency",
}


class PolicyError(ValueError):
    """The caller supplied input the policy layer cannot act on."""


def sanitize_candidate_id(raw: str) -> str:
    """Validate one candidate skill id. Ids are data, never paths or commands."""
    candidate = raw.strip()
    if not candidate or len(candidate) > 80:
        raise PolicyError("candidate id is empty or too long")
    if any(char in candidate for char in ("/", "\\", "..", "\x00")):
        raise PolicyError(f"candidate id `{candidate}` contains path-like characters")
    if any(char.isspace() for char in candidate):
        raise PolicyError(f"candidate id `{candidate}` contains whitespace")
    return candidate


def parse_evidence(pairs: list[str]) -> dict[str, str]:
    """Parse `key=value` evidence pairs, bounded and order-stable."""
    evidence: dict[str, str] = {}
    for index, pair in enumerate(pairs[:MAX_EVIDENCE_ITEMS]):
        if "=" not in pair:
            raise PolicyError(f"evidence item {index + 1} is not `key=value`")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key or key in evidence:
            raise PolicyError(f"evidence key `{key}` is empty or duplicated")
        evidence[key] = value[:MAX_EVIDENCE_VALUE_CHARS]
    return evidence


def build_state(request: str, evidence: dict[str, str], *, max_chars: int) -> str:
    """Assemble the bounded decision state. Evidence only — no secrets flow here."""
    parts = [f"User request: {request[:600]}"]
    for key, value in evidence.items():
        parts.append(f"{key}: {value}")
    state = "\n".join(parts)
    return state[:max_chars]


def build_recommend_questions(candidates: list[str]) -> dict[str, dict[str, Any]]:
    """One choice question over derived candidates plus an explicit unsure outcome.

    The unsure option is the documented no-match outcome: it lets the model
    express that no candidate clearly fits instead of forcing a pick.
    """
    if not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise PolicyError("recommendation needs 2..8 candidates")
    criteria: dict[str, str | None] = {candidate: None for candidate in candidates}
    criteria[UNSURE_OPTION] = "No candidate clearly fits the request, or the supplied evidence is insufficient"
    return {
        "workflow": {
            "type": "choice",
            "instructions": (
                "Which candidate skill best matches the user request and current "
                "project state? Choose the smallest sufficient process. If none "
                "clearly fits, choose unsure."
            ),
            "criteria": criteria,
        }
    }


def interpret_recommend(result: Any, candidates: list[str], chosen: str | None) -> dict[str, Any]:
    """Turn an adapter result into an explicit recommendation outcome.

    Returns a dict with `status` (ok | uncertain | disabled | unavailable),
    an optional `recommendation` (id, confidence, probabilities), and a
    machine-readable `reason`. The `confidence` field is the provider's
    distribution-concentration statistic, not a probability of correctness.
    """
    if chosen is not None:
        return {"status": "ok", "source": "explicit_user_choice", "recommendation": {"id": chosen}}
    if result is None:
        return {"status": "unavailable", "reason": "no_result"}
    if result.status == "disabled":
        return {"status": "disabled", "reason": result.reason}
    if result.status != "ok":
        return {"status": "unavailable", "reason": result.reason}
    answer = result.answers.get("workflow", {})
    if answer.get("type") != "choice":
        return {"status": "unavailable", "reason": "unexpected_answer_type"}
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities", {})
    if choice not in candidates:
        # Includes the unsure option: ambiguity is an outcome, not a pick.
        return {
            "status": "uncertain",
            "reason": "model_returned_unsure",
            "confidence": confidence,
            "probabilities": probabilities,
        }
    if confidence < RECOMMEND_CONFIDENCE_THRESHOLD:
        return {
            "status": "uncertain",
            "reason": "confidence_below_threshold",
            "recommendation": {"id": choice, "confidence": confidence, "probabilities": probabilities},
        }
    return {
        "status": "ok",
        "source": "jev",
        "recommendation": {"id": choice, "confidence": confidence, "probabilities": probabilities},
    }


def build_clarify_questions() -> dict[str, dict[str, Any]]:
    """Independent noul questions for each clarification dimension.

    All dimensions are asked together in one call: they are independent
    judgments over the same state and cannot see one another's answers.
    """
    questions: dict[str, dict[str, Any]] = {}
    for question_id, dimension in CLARIFY_DIMENSIONS.items():
        questions[question_id] = {
            "type": "noul",
            "instructions": (
                f"Would proceeding without knowing {dimension} materially change "
                "the next action, such that asking the user is worth the cost?"
            ),
            "criteria": {
                "true": "The missing information would materially change the next action",
                "false": "Reasonable, reversible defaults make the action safe without it",
            },
        }
    questions["evidence_sufficient"] = {
        "type": "noul",
        "instructions": (
            "Does the supplied evidence as a whole give enough context to act "
            "sensibly on the request without asking anything?"
        ),
        "criteria": {
            "true": "The evidence covers what the action needs",
            "false": "The evidence leaves the request too vague to act on",
        },
    }
    return questions


def interpret_clarify(result: Any) -> dict[str, Any]:
    """Turn an adapter result into an explicit triage outcome.

    Returns `action` (proceed | ask | uncertain | unavailable | disabled),
    the material `gaps` found (highest first), and `assumptions_ok` when the
    caller may proceed on reasonable, reversible assumptions. The question's
    wording is deliberately NOT generated here: that stays with the primary
    reasoning model.
    """
    if result is None:
        return {"action": "unavailable", "reason": "no_result"}
    if result.status == "disabled":
        return {"action": "disabled", "reason": result.reason}
    if result.status != "ok":
        return {"action": "unavailable", "reason": result.reason}

    answers = result.answers
    gaps: list[dict[str, Any]] = []
    for question_id in CLARIFY_DIMENSIONS:
        answer = answers.get(question_id, {})
        noul = answer.get("noul")
        if noul is not None and noul >= CLARIFY_NOUL_THRESHOLD:
            gaps.append({"dimension": question_id, "noul": noul})
    gaps.sort(key=lambda item: -item["noul"])

    sufficient = answers.get("evidence_sufficient", {}).get("noul")

    if gaps:
        return {"action": "ask", "gaps": gaps, "sufficient": sufficient}
    if sufficient is not None and sufficient < CONFLICT_LOW:
        # No single dimension is material, yet overall evidence is judged
        # insufficient: conflicting signals, do not decide from them.
        return {"action": "uncertain", "reason": "conflicting_signals", "gaps": gaps, "sufficient": sufficient}
    return {"action": "proceed", "gaps": [], "sufficient": sufficient, "assumptions_ok": True}


__all__ = [
    "CLARIFY_DIMENSIONS",
    "CONFLICT_LOW",
    "CLARIFY_NOUL_THRESHOLD",
    "PolicyError",
    "RECOMMEND_CONFIDENCE_THRESHOLD",
    "UNSURE_OPTION",
    "build_clarify_questions",
    "build_recommend_questions",
    "build_state",
    "interpret_clarify",
    "interpret_recommend",
    "parse_evidence",
    "sanitize_candidate_id",
]