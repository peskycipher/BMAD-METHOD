#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Shared provider adapter for optional Jev decision support.

Calls TypeSafe's Jev decision model through OpenRouter's decisions endpoint
(``https://openrouter.ai/api/alpha/decisions``) using the native TypeSafe
request/response contract ({state, questions} -> {answers, usage}). The only
credential source is the ``OPENROUTER_API_KEY`` environment variable; no
TypeSafe-direct endpoint is ever contacted.

The adapter is strictly opt-in: it runs network calls only when enabled via
``[jev] enabled = true`` in the central BMad config layers or the
``BMAD_JEV_ENABLED=1`` environment variable. When disabled, unavailable, or
given an invalid response, callers get an explicit status and must fall back
to the ordinary BMad path.

Import this module from sibling scripts (it expects config_utils.py beside
it); it has no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Installed scripts are consumer files, not a location for interpreter caches.
sys.dont_write_bytecode = True

try:
    from config_utils import ConfigError, load_central_config
except ModuleNotFoundError as error:  # pragma: no cover - sibling import only
    if error.name != "tomllib":
        raise
    sys.stderr.write("error: Python 3.11+ is required (stdlib `tomllib` not found).\n")
    raise SystemExit(3) from None

DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

TRUTHY = {"1", "true", "yes", "on"}

# Per-process call budget: each CLI run performs at most a handful of
# decisions; the adapter refuses to exceed this no matter how callers loop.
DEFAULT_MAX_CALLS = 4
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_STATE_CHARS = 4000

Transport = Callable[[str, dict[str, Any], float], tuple[int, bytes]]


class JevAdapterError(RuntimeError):
    """The adapter was asked to operate in a state it cannot act on."""


@dataclass(frozen=True)
class JevSettings:
    """Resolved, opt-in configuration for the decision provider."""

    enabled: bool
    model: str
    endpoint: str
    api_key: str | None
    timeout_seconds: float
    max_retries: int
    max_calls: int
    max_state_chars: int

    @property
    def callable(self) -> bool:
        """True only when every prerequisite for a network call is present."""
        return bool(self.enabled and self.api_key)


@dataclass
class JevResult:
    """Explicit outcome of one adapter request."""

    status: str  # "ok" | "disabled" | "unavailable"
    reason: str | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in TRUTHY


def load_settings(project_root: Path | None) -> JevSettings:
    """Resolve settings from central config layers, then environment overrides.

    Reads the optional ``[jev]`` table from the central BMad config (all
    layers, merged by config_utils) and applies environment overrides:
    ``BMAD_JEV_ENABLED``, ``BMAD_JEV_MODEL``, ``BMAD_JEV_ENDPOINT``,
    ``BMAD_JEV_TIMEOUT_SECONDS``, ``BMAD_JEV_MAX_STATE_CHARS``. The API key
    comes only from ``OPENROUTER_API_KEY``.
    """
    table: dict[str, Any] = {}
    if project_root is not None:
        try:
            table = load_central_config(project_root).get("jev", {}) or {}
        except ConfigError:
            table = {}
        if not isinstance(table, dict):
            table = {}

    def _table_str(key: str) -> str | None:
        value = table.get(key)
        return value if isinstance(value, str) and value.strip() else None

    def _table_float(key: str) -> float | None:
        value = table.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    enabled_env = _env_flag("BMAD_JEV_ENABLED")
    if enabled_env is not None:
        enabled = enabled_env
    else:
        config_enabled = table.get("enabled")
        enabled = config_enabled is True

    model = (
        os.environ.get("BMAD_JEV_MODEL", "").strip()
        or _table_str("model")
        or DEFAULT_MODEL
    )
    endpoint = (
        os.environ.get("BMAD_JEV_ENDPOINT", "").strip()
        or _table_str("endpoint")
        or DEFAULT_ENDPOINT
    )
    timeout_seconds = _table_float("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    max_state_chars = _table_float("max_state_chars") or DEFAULT_MAX_STATE_CHARS

    return JevSettings(
        enabled=enabled,
        model=model,
        endpoint=endpoint,
        api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        timeout_seconds=timeout_seconds,
        max_retries=DEFAULT_MAX_RETRIES,
        max_calls=DEFAULT_MAX_CALLS,
        max_state_chars=int(max_state_chars),
    )


def _make_transport(api_key: str) -> Transport:
    def transport(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, b""

    return transport


def _log(operation: str, duration_ms: int, status: str, reason: str | None, usage: dict[str, Any]) -> None:
    """One-line operational metadata to stderr. Never secrets, never content."""
    record = {
        "op": operation,
        "duration_ms": duration_ms,
        "outcome": status,
    }
    if reason:
        record["fallback_reason"] = reason
    if usage:
        record["usage"] = usage
    sys.stderr.write(json.dumps(record, sort_keys=True) + "\n")


def _validate_answers(
    questions: dict[str, dict[str, Any]], answers: Any
) -> tuple[dict[str, Any], str | None]:
    """Validate the answers map against the questions. Returns (answers, error)."""
    if not isinstance(answers, dict):
        return {}, "answers is not an object"
    validated: dict[str, Any] = {}
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if answer is None:
            return {}, f"missing answer for question `{question_id}`"
        if not isinstance(answer, dict):
            return {}, f"answer `{question_id}` is not an object"
        answer_type = answer.get("type")
        expected_type = question.get("type")
        if answer_type != expected_type:
            return {}, f"answer `{question_id}` type {answer_type!r} != question type {expected_type!r}"
        if expected_type == "noul":
            noul = answer.get("noul")
            if not isinstance(noul, (int, float)) or isinstance(noul, bool) or not 0.0 <= noul <= 1.0:
                return {}, f"answer `{question_id}` noul out of range or missing"
            validated[question_id] = {"type": "noul", "noul": float(noul)}
        elif expected_type == "choice":
            choice = answer.get("choice")
            options = (question.get("criteria") or {}).keys()
            if not isinstance(choice, str) or choice not in options:
                return {}, f"answer `{question_id}` choice is not one of the supplied options"
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, dict) or not probabilities:
                return {}, f"answer `{question_id}` probabilities missing"
            total = 0.0
            for option, probability in probabilities.items():
                if option not in options:
                    return {}, f"answer `{question_id}` probabilities name a non-supplied option"
                if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not 0.0 <= probability <= 1.0:
                    return {}, f"answer `{question_id}` probability out of range"
                total += float(probability)
            if abs(total - 1.0) > 0.05:
                return {}, f"answer `{question_id}` probabilities do not sum to 1"
            confidence = answer.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= confidence <= 1.0:
                return {}, f"answer `{question_id}` confidence out of range or missing"
            validated[question_id] = {
                "type": "choice",
                "choice": choice,
                "probabilities": {k: float(v) for k, v in probabilities.items()},
                "confidence": float(confidence),
            }
        elif expected_type == "score":
            legend = answer.get("legend")
            probabilities = answer.get("probabilities")
            if not isinstance(legend, dict) or not isinstance(probabilities, dict) or not probabilities:
                return {}, f"answer `{question_id}` legend or probabilities missing"
            total = 0.0
            for level, probability in probabilities.items():
                if level not in legend:
                    return {}, f"answer `{question_id}` probabilities name an unknown level"
                if not isinstance(probability, (int, float)) or isinstance(probability, bool) or not 0.0 <= probability <= 1.0:
                    return {}, f"answer `{question_id}` probability out of range"
                total += float(probability)
            if abs(total - 1.0) > 0.05:
                return {}, f"answer `{question_id}` probabilities do not sum to 1"
            score = answer.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return {}, f"answer `{question_id}` score missing"
            confidence = answer.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0.0 <= confidence <= 1.0:
                return {}, f"answer `{question_id}` confidence out of range or missing"
            validated[question_id] = {
                "type": "score",
                "score": float(score),
                "legend": dict(legend),
                "probabilities": {k: float(v) for k, v in probabilities.items()},
                "confidence": float(confidence),
            }
        else:
            return {}, f"question `{question_id}` has unsupported type {expected_type!r}"
    return validated, None


class JevClient:
    """Bounded, budgeted client for the OpenRouter decisions endpoint."""

    def __init__(self, settings: JevSettings, transport: Transport | None = None) -> None:
        self.settings = settings
        self._transport = transport or (_make_transport(settings.api_key) if settings.api_key else None)
        self._calls_used = 0

    @property
    def calls_used(self) -> int:
        return self._calls_used

    def _budget_left(self) -> bool:
        return self._calls_used < self.settings.max_calls

    def post_decision(self, *, operation: str, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
        """Evaluate one batch of independent questions. Explicit statuses only.

        Returns status "ok" with validated answers, "disabled" when the
        integration is off, or "unavailable" with a fallback reason. Never
        raises for provider problems; callers must handle both non-ok statuses.
        """
        settings = self.settings
        if not settings.enabled:
            return JevResult(status="disabled", reason="disabled_by_config")
        if not settings.api_key or self._transport is None:
            return JevResult(status="unavailable", reason="missing_openrouter_api_key")
        if not questions:
            return JevResult(status="unavailable", reason="no_questions")
        if not self._budget_left():
            return JevResult(status="unavailable", reason="call_budget_exhausted")

        if isinstance(state, str) and len(state) > settings.max_state_chars:
            state = state[: settings.max_state_chars]
        payload: dict[str, Any] = {"model": settings.model, "state": state, "questions": questions}

        started = time.monotonic()
        status_code, body = b"", 0
        result: JevResult | None = None
        attempts = settings.max_retries + 1
        for attempt in range(attempts):
            self._calls_used += 1
            try:
                status_code, body = self._transport(settings.endpoint, payload, settings.timeout_seconds)
            except (OSError, TimeoutError, ValueError):
                # An injected transport may raise; treat every transport
                # failure uniformly as an unavailable outcome.
                status_code, body = 0, b""
            if status_code == 200:
                result = self._parse_success(body, questions)
                break
            # Retry once on rate limiting or transient upstream errors.
            if status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1 and self._budget_left():
                time.sleep(0.25 * (attempt + 1))
                continue
            result = JevResult(status="unavailable", reason=f"http_{status_code}")
            break

        assert result is not None
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = result.usage if result.status == "ok" else {}
        _log(operation, duration_ms, result.status, result.reason, usage)
        return result

    def _parse_success(self, body: bytes, questions: dict[str, dict[str, Any]]) -> JevResult:
        try:
            envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JevResult(status="unavailable", reason="invalid_json_response")
        if not isinstance(envelope, dict):
            return JevResult(status="unavailable", reason="invalid_json_response")
        answers, error = _validate_answers(questions, envelope.get("answers"))
        if error is not None:
            return JevResult(status="unavailable", reason=error)
        usage = envelope.get("usage")
        model = envelope.get("model")
        return JevResult(
            status="ok",
            answers=answers,
            usage=usage if isinstance(usage, dict) else {},
            model=model if isinstance(model, str) else None,
        )


__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "JevAdapterError",
    "JevClient",
    "JevResult",
    "JevSettings",
    "Transport",
    "load_settings",
]