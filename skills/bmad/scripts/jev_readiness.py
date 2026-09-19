#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ////
"""Advisory Jev readiness gate (opt-in, disabled by default).

Reads one BMad upstream artifact from a file, evaluates it for the named
phase transition through the Jev provider, and prints an advisory
`proceed`/`hold` JSON signal on stdout. The calling skill keeps full
control: a `hold` is a signal to surface the recorded reasons to the user,
never an automatic block; a `proceed` is one vote, never an approval.

Disabled by default. Set the decision-assist mode to ``suggest`` (advisory)
or ``shadow`` (evaluation only) via ``BMAD_DECISION_ASSIST_MODE`` or the
``[jev] mode`` central-config key. When off, zero network calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

try:
    from jev_adapter import JevClient, load_settings
    from jev_gates import (
        MAX_ARTIFACT_CHARS,
        PolicyError,
        build_readiness_questions,
        build_readiness_state,
        interpret_readiness,
        validate_text,
        validate_transition,
    )
    from jev_policy import apply_request_integrity, build_integrity_questions
except ModuleNotFoundError as error:
    if error.name not in ("tomllib", "jev_adapter", "jev_gates", "jev_policy"):
        raise
    sys.stderr.write("error: run this script from its own directory (sibling modules missing).\n")
    raise SystemExit(3) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advisory Jev readiness gate")
    parser.add_argument("--project-root", default=".", help="BMad project root for central config")
    parser.add_argument(
        "--transition",
        default="planning_to_solutioning",
        help="phase transition under review: analysis_to_planning, planning_to_solutioning, or solutioning_to_implementation",
    )
    parser.add_argument(
        "--artifact-file",
        required=True,
        help="path to the upstream artifact text (read locally; bounded)",
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

    try:
        transition = validate_transition(args.transition)
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2

    artifact_path = Path(args.artifact_file)
    try:
        raw = artifact_path.read_text(encoding="utf-8")
    except OSError as error:
        sys.stderr.write(f"error: cannot read artifact file: {error}\n")
        return 2
    try:
        artifact = validate_text(raw, "artifact", MAX_ARTIFACT_CHARS)
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2

    client = JevClient(settings)
    state = build_readiness_state(artifact, transition)
    result = client.post_decision(
        operation="readiness_gate",
        state=state,
        questions=build_readiness_questions(),
    )
    outcome = interpret_readiness(result)
    if outcome.get("status") == "ok" and outcome.get("verdict") == "proceed":
        # Second-stage prompt-injection gate, mirroring jev_recommend: the
        # artifact is untrusted text, and the check only matters once every
        # readiness gate already passed.
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