#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Priority B — advisory clarification triage via Jev (opt-in).

Judges whether missing information on named dimensions (intended outcome,
scope boundaries, acceptance criteria, consequential constraints) would
materially change the next action. One batched call of independent noul
questions over the supplied, bounded context. The script identifies gaps;
it never writes the clarifying question — the primary reasoning model turns
a reported gap into one concise question.

When triage would not materially improve the result, the outcome is
"proceed": act on reasonable, reversible assumptions instead of asking.

Disabled by default: enable with `[jev] enabled = true` in the central BMad
config or `BMAD_JEV_ENABLED=1`. When disabled, this makes zero network calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

try:
    from jev_adapter import JevClient, load_settings
    from jev_policy import PolicyError, build_clarify_questions, build_state, interpret_clarify, parse_evidence
except ModuleNotFoundError as error:
    if error.name not in ("tomllib", "jev_adapter", "jev_policy"):
        raise
    sys.stderr.write("error: run this script from its own directory (sibling modules missing).\n")
    raise SystemExit(3) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advisory Jev clarification triage")
    parser.add_argument("--request", required=True, help="the user's request text")
    parser.add_argument("--project-root", default=".", help="BMad project root for central config")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="bounded context item already known (repeatable)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_root = Path(args.project_root).resolve()
    settings = load_settings(project_root)

    if not settings.callable:
        status = "disabled" if not settings.enabled else "unavailable"
        reason = "disabled_by_config" if status == "disabled" else "missing_openrouter_api_key"
        print(json.dumps({"action": status, "reason": reason, "calls_made": 0}))
        return 0

    try:
        context = parse_evidence(args.context)
    except PolicyError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2

    client = JevClient(settings)
    result = client.post_decision(
        operation="clarification_triage",
        state=build_state(args.request, context, max_chars=settings.max_state_chars),
        questions=build_clarify_questions(),
    )
    outcome = interpret_clarify(result)
    outcome["calls_made"] = client.calls_used
    if result.status == "ok":
        outcome["usage"] = result.usage
        outcome["model"] = result.model
    outcome["advisory"] = True
    outcome["note"] = "the question wording is formulated by the primary model from the reported gap, not by this script"
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())