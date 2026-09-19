import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RECOMMEND = SCRIPTS / "jev_recommend.py"
CLARIFY = SCRIPTS / "jev_clarify.py"


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
    for key in ("BMAD_JEV_ENABLED", "BMAD_JEV_MODEL", "BMAD_JEV_ENDPOINT"):
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
            {"BMAD_JEV_ENABLED": "0", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "disabled")
        self.assertEqual(outcome["calls_made"], 0)

    def test_enabled_without_key_is_unavailable(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy my model", "--candidates", "bmad-spec,bmad-build"],
            {"BMAD_JEV_ENABLED": "1", "OPENROUTER_API_KEY": ""},
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
                    "BMAD_JEV_ENABLED": "1",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_JEV_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
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
            {"BMAD_JEV_ENABLED": "1", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["source"], "single_candidate")
        self.assertEqual(outcome["calls_made"], 0)

    def test_unknown_candidate_id_is_rejected(self):
        result = run(
            RECOMMEND,
            ["--request", "deploy", "--candidates", "../etc/passwd,bmad-build"],
            {"BMAD_JEV_ENABLED": "1", "OPENROUTER_API_KEY": "test-key"},
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
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        server, handler = make_server(200, body)
        try:
            result = run(
                RECOMMEND,
                ["--request", "implement story 2.3", "--candidates", "bmad-spec,bmad-build"],
                {
                    "BMAD_JEV_ENABLED": "1",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_JEV_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["recommendation"]["id"], "bmad-build")
        self.assertEqual(outcome["calls_made"], 1)
        self.assertIn("not a probability of correctness", outcome["confidence_note"])
        # The payload only carries what was supplied: bounded state, no secrets.
        sent = handler.requests[0]
        self.assertEqual(sent["model"], "typesafe/jev-1.13")
        self.assertIn("implement story 2.3", sent["state"])
        self.assertNotIn("OPENROUTER_API_KEY", json.dumps(sent))

    def test_invalid_provider_response_falls_back(self):
        body = {"model": "x", "answers": {"workflow": {"type": "choice", "choice": "not-offered", "probabilities": {}, "confidence": 1}}}
        server, _ = make_server(200, body)
        try:
            result = run(
                RECOMMEND,
                ["--request", "implement", "--candidates", "bmad-spec,bmad-build"],
                {
                    "BMAD_JEV_ENABLED": "1",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_JEV_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
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
            {"BMAD_JEV_ENABLED": "0", "OPENROUTER_API_KEY": "sk-or-secret-value"},
        )
        self.assertNotIn("sk-or-secret-value", result.stdout + result.stderr)


class ClarifyCliTests(unittest.TestCase):
    def test_disabled(self):
        result = run(
            CLARIFY,
            ["--request", "add a feature"],
            {"BMAD_JEV_ENABLED": "0", "OPENROUTER_API_KEY": "test-key"},
        )
        self.assertEqual(result.returncode, 0)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["action"], "disabled")
        self.assertEqual(outcome["calls_made"], 0)

    def test_mocked_triage_reports_gap(self):
        questions = ["outcome_missing", "scope_missing", "acceptance_missing", "constraint_missing", "evidence_sufficient"]
        answers = {q: {"type": "noul", "noul": 0.2} for q in questions}
        answers["scope_missing"] = {"type": "noul", "noul": 0.85}
        answers["evidence_sufficient"] = {"type": "noul", "noul": 0.6}
        body = {"model": "typesafe/jev-1.13-20260917", "answers": answers, "usage": {"input_tokens": 90, "output_tokens": 30}}
        server, handler = make_server(200, body)
        try:
            result = run(
                CLARIFY,
                ["--request", "improve the wizard", "--context", "goal=better UX", "--project-root", tempfile.mkdtemp()],
                {
                    "BMAD_JEV_ENABLED": "1",
                    "OPENROUTER_API_KEY": "test-key",
                    "BMAD_JEV_ENDPOINT": f"http://127.0.0.1:{server.server_port}/api/alpha/decisions",
                },
            )
        finally:
            server.shutdown()
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["action"], "ask")
        self.assertEqual(outcome["gaps"][0]["dimension"], "scope_missing")
        self.assertEqual(handler.requests[0]["state"].count("goal: better UX"), 1)


if __name__ == "__main__":
    unittest.main()