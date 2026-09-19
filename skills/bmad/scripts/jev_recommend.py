#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Advisory Jev workflow recommendation (opt-in, disabled by default).

Narrow judgment over supplied evidence: the caller (a BMad skill) derives
candidate skill ids from the installed registry, passes them here, and this
script either honors an explicit user choice, handles single-candidate cases
deterministically, or asks Jev through OpenRouter when semantic ambiguity
remains. Output is a JSON advisory signal on stdout; the calling skill keeps
full control and must fall back to its ordinary reasoning whenever the status
is not "ok".

Disabled by default. Set the decision-assist mode to ``suggest`` (advisory
recommendations) or ``shadow`` (evaluation only) via
``BMAD_DECISION_ASSIST_MODE`` or the ``[jev] mode`` central-config key. When
off, this makes zero network calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

try:
    from jev_adapter import JevClient, load_settings
    from jev_policy import (
        PolicyError,
        apply_request_integrity,
        build_integrity_questions,
        build_recommend_questions,
        build_state,
        interpret_recommend,
        parse_evidence,
        sanitize_candidate_id,
    )
except ModuleNotFoundError as error:
    if error.name not in ("tomllib", "jev_adapter", "jev_policy"):
        raise
    sys.stderr.write("error: run this script from its own directory (sibling modules missing).\n")
    raise SystemExit(3) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advisory Jev workflow recommendation")
    parser.add_argument("--request", required=True, help="the user's request text")
    parser.add_argument("--project-root", default=".", help="BMad project root for central config")
    parser.add_argument(
        "--candidates",
        required=True,
        help="comma-separated candidate skill ids derived from the installed registry",
    )
    parser.add_argument(
        "--chosen",
        default=None,
        help="explicit user choice: skip the provider entirely and echo the choice",
    )
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="bounded evidence item (repeatable)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_root = Path(args.project_root).resolve()
    settings = load_settings(project_root)

    if not settings.callable:
        status = "disabled" if settings.mode == "off" else "unavailable"
        reason = "disabled_by_config" if status == "disabled" else "missing_openrouter_api_key"
        print(json.dumps({"status": status, "reason": reason, "calls_made": 0}))
        return 0

    chosen = None
    if args.chosen:
        try:
            chosen = sanitize_candidate_id(args.chosen)
        except PolicyError as error:
            sys.stderr.write(f"error: {error}\n")
            return 2
        # An explicit user choice outranks any model result: echo it and
        # skip the provider entirely (zero network calls).
        print(
            json.dumps(
                {"status": "ok", "source": "explicit_user_choice", "recommendation": {"id": chosen}, "calls_made": 0}
            )
        )
        return 0

    try:
        candidates = [sanitize_candidate_id(raw) for raw in args.candidates.split(",") if raw.strip()]
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2
    # Dedupe, preserving order, and cap the option count via policy checks.
    candidates = list(dict.fromkeys(candidates))

    if len(candidates) == 1:
        print(
            json.dumps(
                {"status": "ok", "source": "single_candidate", "recommendation": {"id": candidates[0]}, "calls_made": 0}
            )
        )
        return 0
    if len(candidates) == 0:
        print(json.dumps({"status": "unavailable", "reason": "no_candidates", "calls_made": 0}))
        return 0

    try:
        evidence = parse_evidence(args.evidence)
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2

    questions = build_recommend_questions(candidates)
    state = build_state(args.request, evidence, max_chars=settings.max_state_chars)
    client = JevClient(settings)
    result = client.post_decision(
        operation="workflow_recommendation",
        state=state,
        questions=questions,
    )
    outcome = interpret_recommend(result, candidates, None)
    if outcome["status"] == "ok":
        # Second-stage prompt-injection gate: a genuine serial dependency
        # (it only matters once every recommendation gate already passed).
        integrity_result = client.post_decision(
            operation="request_integrity",
            state=state,
            questions=build_integrity_questions(),
        )
        outcome = apply_request_integrity(outcome, integrity_result)
    outcome["calls_made"] = client.calls_used
    if result.status == "ok":
        outcome["usage"] = result.usage
        outcome["model"] = result.model
    outcome["mode"] = settings.mode
    outcome["advisory"] = True
    outcome["confidence_note"] = (
        "confidence is the provider's distribution-concentration statistic, not a probability of correctness"
    )
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
