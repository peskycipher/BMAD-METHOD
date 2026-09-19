#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ////
"""Advisory Jev story review (opt-in, disabled by default).

Reads a user story and its implementation description from files, judges
the pair against the bounded review rubric through the Jev provider, and
prints an advisory `first_pass`/`rework` JSON verdict with the failure
taxonomy on stdout. The calling skill keeps full control: a `rework`
verdict is a signal to surface the recorded reasons in its own review
flow, never an automatic rejection; a `first_pass` is one vote, never an
approval that bypasses a checkpoint.

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
        MAX_IMPLEMENTATION_CHARS,
        MAX_STORY_CHARS,
        PolicyError,
        build_review_questions,
        build_review_state,
        interpret_review,
        validate_text,
    )
    from jev_policy import apply_request_integrity, build_integrity_questions
except ModuleNotFoundError as error:
    if error.name not in ("tomllib", "jev_adapter", "jev_gates", "jev_policy"):
        raise
    sys.stderr.write("error: run this script from its own directory (sibling modules missing).\n")
    raise SystemExit(3) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advisory Jev story review")
    parser.add_argument("--project-root", default=".", help="BMad project root for central config")
    parser.add_argument("--story-file", required=True, help="path to the user story text")
    parser.add_argument(
        "--implementation-file",
        required=True,
        help="path to the implementation description (code summary, diff, or report)",
    )
    return parser


def _read_bounded(path_text: str, what: str, max_chars: int) -> str:
    path = Path(path_text)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PolicyError(f"cannot read {what} file: {error}") from error
    return validate_text(raw, what, max_chars)


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
        story = _read_bounded(args.story_file, "story", MAX_STORY_CHARS)
        implementation = _read_bounded(args.implementation_file, "implementation", MAX_IMPLEMENTATION_CHARS)
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2

    client = JevClient(settings)
    state = build_review_state(story, implementation)
    result = client.post_decision(
        operation="story_review",
        state=state,
        questions=build_review_questions(),
    )
    outcome = interpret_review(result)
    if outcome.get("status") == "ok" and outcome.get("verdict") == "first_pass":
        # Second-stage prompt-injection gate, mirroring jev_recommend: the
        # story and implementation are untrusted text, and the check only
        # matters once the review already passed.
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