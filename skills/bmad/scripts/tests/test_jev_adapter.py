import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_adapter import (  # noqa: E402
    DEFAULT_ENDPOINT,
    JevClient,
    JevResult,
    JevSettings,
    load_settings,
)


def make_settings(**overrides):
    defaults = dict(
        mode="suggest",
        model="typesafe/jev-1.13-20260917",
        endpoint="https://openrouter.ai/api/alpha/decisions",
        api_key="test-key",
        timeout_seconds=1.0,
        max_retries=1,
        max_calls=4,
        max_state_chars=4000,
    )
    defaults.update(overrides)
    return JevSettings(**defaults)


def envelope(answers):
    return json.dumps(
        {
            "model": "typesafe/jev-1.13-20260917",
            "answers": answers,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001},
        }
    ).encode()


QUESTIONS = {
    "pick": {
        "type": "choice",
        "instructions": "pick one",
        "criteria": {"alpha": None, "beta": None, "unsure": None},
    },
}
GOOD_ANSWERS = {
    "pick": {
        "type": "choice",
        "choice": "alpha",
        "probabilities": {"alpha": 0.7, "beta": 0.2, "unsure": 0.1},
        "confidence": 0.7,
    },
}


class SpyTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        status, body = self.responses.pop(0)
        return status, body


class AdapterTests(unittest.TestCase):
    def test_disabled_makes_zero_calls(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(mode="off"), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.reason, "disabled_by_config")
        self.assertEqual(spy.calls, [])
        self.assertEqual(client.calls_used, 0)

    def test_shadow_mode_makes_calls_like_suggest(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(mode="shadow"), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(spy.calls), 1)
        self.assertTrue(make_settings(mode="shadow").callable)
        self.assertFalse(make_settings(mode="off").callable)

    def test_missing_key_is_unavailable(self):
        client = JevClient(make_settings(api_key=None), transport=SpyTransport([]))
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "missing_openrouter_api_key")

    def test_settings_callable_requires_key(self):
        settings = make_settings(api_key=None)
        self.assertFalse(settings.callable)
        self.assertTrue(make_settings().callable)

    def test_success_parses_and_validates(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answers["pick"]["choice"], "alpha")
        self.assertEqual(result.model, "typesafe/jev-1.13-20260917")
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertEqual(spy.calls[0]["payload"]["model"], "typesafe/jev-1.13-20260917")
        self.assertEqual(spy.calls[0]["url"], DEFAULT_ENDPOINT)

    def test_invalid_json_is_unavailable(self):
        spy = SpyTransport([(200, b"not json")])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "invalid_json_response")

    def test_missing_answer_is_unavailable(self):
        spy = SpyTransport([(200, envelope({}))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("missing answer", result.reason)

    def test_incomplete_answer_fields_are_unavailable(self):
        incomplete = {
            "pick": {
                "type": "choice",
                "choice": "alpha",
                "probabilities": {"alpha": 0.7, "beta": 0.2, "unsure": 0.1},
                # confidence missing
            }
        }
        spy = SpyTransport([(200, envelope(incomplete))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("confidence", result.reason)

    def test_choice_outside_options_is_unavailable(self):
        answers = dict(GOOD_ANSWERS)
        answers["pick"] = {
            "type": "choice",
            "choice": "gamma",
            "probabilities": {"gamma": 1.0},
            "confidence": 1.0,
        }
        spy = SpyTransport([(200, envelope(answers))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("not one of the supplied options", result.reason)

    def test_probability_out_of_range_is_unavailable(self):
        answers = dict(GOOD_ANSWERS)
        answers["pick"] = {
            "type": "choice",
            "choice": "alpha",
            "probabilities": {"alpha": 1.5, "beta": -0.3, "unsure": 0.1},
            "confidence": 0.7,
        }
        spy = SpyTransport([(200, envelope(answers))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("out of range", result.reason)

    def test_probabilities_not_summing_to_one_is_unavailable(self):
        answers = dict(GOOD_ANSWERS)
        answers["pick"] = {
            "type": "choice",
            "choice": "alpha",
            "probabilities": {"alpha": 0.5, "beta": 0.2, "unsure": 0.1},
            "confidence": 0.7,
        }
        spy = SpyTransport([(200, envelope(answers))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("sum to 1", result.reason)

    def test_answer_type_mismatch_is_unavailable(self):
        answers = dict(GOOD_ANSWERS)
        answers["pick"] = {"type": "noul", "noul": 0.5}
        spy = SpyTransport([(200, envelope(answers))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("type", result.reason)

    def test_unsupported_question_type_is_unavailable(self):
        questions = {"flag": {"type": "noul", "instructions": "yes or no"}}
        spy = SpyTransport([(200, envelope({"flag": {"type": "noul", "noul": 0.9}}))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=questions)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("unsupported type", result.reason)

    def test_retry_on_429_then_success(self):
        spy = SpyTransport([(429, b"rate limited"), (200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(spy.calls), 2)

    def test_no_retry_on_404(self):
        spy = SpyTransport([(404, b"nope")])
        client = JevClient(make_settings(), transport=spy)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "http_404")
        self.assertEqual(len(spy.calls), 1)

    def test_connection_failure_is_unavailable(self):
        def broken_transport(url, payload, timeout):
            raise OSError("connection refused")

        client = JevClient(make_settings(), transport=broken_transport)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "http_0")

    def test_call_budget_is_enforced(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))] * 10)
        client = JevClient(make_settings(max_calls=2), transport=spy)
        for _ in range(2):
            client.post_decision(operation="op", state="state", questions=QUESTIONS)
        result = client.post_decision(operation="op", state="state", questions=QUESTIONS)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "call_budget_exhausted")
        self.assertEqual(client.calls_used, 2)

    def test_state_is_truncated(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(max_state_chars=50), transport=spy)
        client.post_decision(operation="op", state="x" * 1000, questions=QUESTIONS)
        self.assertEqual(len(spy.calls[0]["payload"]["state"]), 50)

    def test_log_line_has_no_secrets_or_content(self):
        spy = SpyTransport([(200, envelope(GOOD_ANSWERS))])
        client = JevClient(make_settings(), transport=spy)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            client.post_decision(operation="op", state="SECRET-STATE-CONTENT test-key", questions=QUESTIONS)
        line = stderr.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        self.assertEqual(record["op"], "op")
        self.assertEqual(record["outcome"], "ok")
        self.assertNotIn("SECRET-STATE-CONTENT", line)
        self.assertNotIn("test-key", line)

    def test_default_endpoint_is_openrouter_not_typesafe_direct(self):
        self.assertIn("openrouter.ai", DEFAULT_ENDPOINT)
        self.assertNotIn("typesafe.ai", DEFAULT_ENDPOINT)

    def test_load_settings_env_overrides_config(self):
        import os

        from config_utils import ConfigError  # noqa: F401

        root = Path(__file__).resolve().parents[3]  # repo root, no _bmad/config.toml needed
        try:
            os.environ.pop("BMAD_DECISION_ASSIST_MODE", None)
            os.environ.pop("BMAD_DECISION_ASSIST_MODEL", None)
            settings = load_settings(root)
            self.assertEqual(settings.mode, "off")
            self.assertEqual(settings.model, "typesafe/jev-1.13-20260917")

            os.environ["BMAD_DECISION_ASSIST_MODE"] = "suggest"
            os.environ["BMAD_DECISION_ASSIST_MODEL"] = "typesafe/jev-1.13-test"
            settings = load_settings(root)
            self.assertEqual(settings.mode, "suggest")
            self.assertEqual(settings.model, "typesafe/jev-1.13-test")
        finally:
            os.environ.pop("BMAD_DECISION_ASSIST_MODE", None)
            os.environ.pop("BMAD_DECISION_ASSIST_MODEL", None)

    def test_load_settings_unknown_mode_is_off(self):
        import os

        root = Path(__file__).resolve().parents[3]
        try:
            os.environ["BMAD_DECISION_ASSIST_MODE"] = "turbo"
            settings = load_settings(root)
            self.assertEqual(settings.mode, "off")
            self.assertFalse(settings.callable)
        finally:
            os.environ.pop("BMAD_DECISION_ASSIST_MODE", None)

    def test_jev_result_shape(self):
        result = JevResult(status="ok")
        self.assertEqual(result.answers, {})
        self.assertEqual(result.usage, {})


if __name__ == "__main__":
    unittest.main()
