import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RECOMMEND = SCRIPTS / "jev_recommend.py"


class _MockDecisionsHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for OpenRouter's decisions endpoint."""

    body = None  # set per test via the factory below

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _payload = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        self.send_response(cls.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(cls.body).encode())
        cls.requests.append(_payload)

    def log_message(self, *args):
        pass


def make_server(status, body):
    handler = type("Handler", (_MockDecisionsHandler,), {"status": status, "body": body, "requests": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, handler


def run(script, args, env_overrides):
    env = dict(os.environ)
    env.update(env_overrides)
    for key in (
        "BMAD_DECISION_ASSIST_MODE",
        "BMAD_DECISION_ASSIST_MODEL",
        "BMAD_DECISION_ASSIST_ENDPOINT",
    ):
        env.setdefault(key, "")
    return subprocess.run(
        [sys.executable, str(script), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
        cwd=str(SCRIPTS),
        timeout=30,
    )


class RecommendCliTests(unittest.TestCase):
    def test_disabled_makes_zero_calls(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy my model", "--candidates", "bmad-spec,bmad-build"],
            {"BMAD_DECISION_ASSIST_MODE": "off", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "disabled")
        self.assertEqual(outcome["calls_made"], 0)

    def test_suggest_without_key_is_unavailable(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy my model", "--candidates", "bmad-spec,bmad-build"],
            {"BMAD_DECISION_ASSIST_MODE": "suggest", "OPENROUTER_API_KEY": ""},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertEqual(outcome["reason"], "missing_openrouter_api_key")
        self.assertEqual(outcome["calls_made"], 0)

    def test_explicit_choice_skips_provider(self):
        server, handler = make_server(200, {})
        try:
            result = run(
                RECOMMEND,
                ["--request", "deploy", "--candidates", "bmad-spec,bmad-build", "--chosen", "bmad-build"],
                {
                    "BMAD_DECISION_ASSIST_MODE": "suggest",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_DECISION_ASSIST_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["source"], "explicit_user_choice")
        self.assertEqual(outcome["recommendation"]["id"], "bmad-build")
        self.assertEqual(handler.requests, [])

    def test_single_candidate_makes_no_call(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "bmad-build"],
            {"BMAD_DECISION_ASSIST_MODE": "suggest", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["source"], "single_candidate")
        self.assertEqual(outcome["calls_made"], 0)

    def test_unknown_candidate_id_is_rejected(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "../etc/passwd,bmad-build"],
            {"BMAD_DECISION_ASSIST_MODE": "suggest", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("path-like", result.stderr)

    def test_mocked_provider_round_trip(self):
        body = {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {
                "workflow": {
                    "type": "choice",
                    "choice": "bmad-build",
                    "probabilities": {"bmad-spec": 0.1, "bmad-build": 0.85, "unsure": 0.05},
                    "confidence": 0.85,
                },
                "matches": {"type": "noul", "noul": 0.9},
                "request_integrity": {"type": "noul", "noul": 0.05},
                "fit": {
                    "type": "score",
                    "score": 1.8,
                    "confidence": 0.9,
                    "probabilities": {"1": 0.2, "2": 0.8},
                },
            },
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        server, handler = make_server(200, body)
        try:
            result = run(
                RECOMMEND,
                ["--request", "implement story 2.3", "--candidates", "bmad-spec,bmad-build"],
                {
                    "BMAD_DECISION_ASSIST_MODE": "suggest",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_DECISION_ASSIST_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["recommendation"]["id"], "bmad-build")
        self.assertEqual(outcome["calls_made"], 2)
        self.assertEqual(outcome["mode"], "suggest")
        self.assertIn("not a probability of correctness", outcome["confidence_note"])
        # The payload only carries what was supplied: bounded state, no secrets.
        sent = handler.requests[0]
        self.assertEqual(sent["model"], "typesafe/jev-1.13-20260917")
        self.assertIn("implement story 2.3", sent["state"]["request"])
        self.assertNotIn("OPENROUTER_API_KEY", json.dumps(sent))

    def test_invalid_provider_response_falls_back(self):
        body = {
            "model": "x",
            "answers": {"workflow": {"type": "choice", "choice": "not-offered", "probabilities": {}, "confidence": 1}},
        }
        server, _ = make_server(200, body)
        try:
            result = run(
                RECOMMEND,
                ["--request", "implement", "--candidates", "bmad-spec,bmad-build"],
                {
                    "BMAD_DECISION_ASSIST_MODE": "suggest",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_DECISION_ASSIST_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("not one of the supplied options", outcome["reason"])

    def test_no_secrets_in_diagnostics(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "a,b"],
            {"BMAD_DECISION_ASSIST_MODE": "off", "OPENROUTER_API_KEY": "sk-or-secret-value"},
        )
        self.assertNotIn("sk-or-secret-value", result.stdout + result.stderr)

    def test_shadow_mode_runs_but_tags_output(self):
        """Shadow mode evaluates, but the output is tagged so hosts can refuse
        to let it influence the user-facing recommendation."""
        body = {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {
                "workflow": {
                    "type": "choice",
                    "choice": "bmad-spec",
                    "probabilities": {"bmad-spec": 0.9, "bmad-build": 0.05, "unsure": 0.05},
                    "confidence": 0.9,
                },
                "matches": {"type": "noul", "noul": 0.95},
                "request_integrity": {"type": "noul", "noul": 0.05},
                "fit": {
                    "type": "score",
                    "score": 1.9,
                    "confidence": 0.95,
                    "probabilities": {"2": 1.0},
                },
            },
            "usage": {"input_tokens": 50, "output_tokens": 10},
        }
        server, handler = make_server(200, body)
        try:
            result = run(
                RECOMMEND,
                ["--request", "plan something", "--candidates", "bmad-spec,bmad-build"],
                {
                    "BMAD_DECISION_ASSIST_MODE": "shadow",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_DECISION_ASSIST_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["mode"], "shadow")
        self.assertEqual(len(handler.requests), 2)

    def test_invalid_mode_falls_back_to_disabled(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "a,b"],
            {"BMAD_DECISION_ASSIST_MODE": "turbo", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "disabled")
        self.assertEqual(outcome["calls_made"], 0)

    def test_helper_execution_writes_no_artifacts(self):
        """Help-time runs must not leave files behind (no caches, no logs,
        no telemetry). The skill tree is snapshot before and after."""
        before = {p.relative_to(SCRIPTS) for p in SCRIPTS.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "bmad-build"],
            {"BMAD_DECISION_ASSIST_MODE": "suggest", "OPENROUTER_API_KEY": ""},
        )
        after = {p.relative_to(SCRIPTS) for p in SCRIPTS.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(result.returncode, 0)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
