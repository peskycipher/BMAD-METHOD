import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_adapter import JevResult  # noqa: E402
from jev_policy import (  # noqa: E402
    PolicyError,
    build_clarify_questions,
    build_recommend_questions,
    build_state,
    interpret_clarify,
    interpret_recommend,
    parse_evidence,
    sanitize_candidate_id,
)


def choice_result(choice, confidence, probabilities=None):
    if probabilities is None:
        probabilities = {choice: confidence, "unsure": 1 - confidence}
    return JevResult(
        status="ok",
        answers={
            "workflow": {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        },
    )


class CandidateIdTests(unittest.TestCase):
    def test_rejects_path_like_ids(self):
        for raw in ("../etc", "a/b", "a\\b", "a b", "", "x" * 81):
            with self.assertRaises(PolicyError):
                sanitize_candidate_id(raw)

    def test_accepts_plain_ids(self):
        self.assertEqual(sanitize_candidate_id(" bmad-spec "), "bmad-spec")


class EvidenceTests(unittest.TestCase):
    def test_parses_and_bounds(self):
        evidence = parse_evidence(["goal=deploy model", "state=sprint 2 running"])
        self.assertEqual(evidence["goal"], "deploy model")

    def test_rejects_bad_pairs(self):
        with self.assertRaises(PolicyError):
            parse_evidence(["noseparator"])
        with self.assertRaises(PolicyError):
            parse_evidence(["k=v", "k=w"])

    def test_caps_items(self):
        evidence = parse_evidence([f"k{i}=v" for i in range(20)])
        self.assertEqual(len(evidence), 12)


class BuildStateTests(unittest.TestCase):
    def test_state_contains_request_and_evidence(self):
        state = build_state("deploy my model", {"goal": "runpod"}, max_chars=4000)
        self.assertIn("deploy my model", state)
        self.assertIn("goal: runpod", state)

    def test_state_is_capped(self):
        state = build_state("x" * 2000, {}, max_chars=500)
        self.assertEqual(len(state), 500)


class RecommendQuestionsTests(unittest.TestCase):
    def test_includes_unsure_outcome(self):
        questions = build_recommend_questions(["a", "b"])
        criteria = questions["workflow"]["criteria"]
        self.assertEqual(set(criteria), {"a", "b", "unsure"})
        self.assertEqual(questions["workflow"]["type"], "choice")

    def test_rejects_wrong_counts(self):
        with self.assertRaises(PolicyError):
            build_recommend_questions(["a"])
        with self.assertRaises(PolicyError):
            build_recommend_questions(["a"] * 9)


class InterpretRecommendTests(unittest.TestCase):
    def test_explicit_choice_skips_model(self):
        outcome = interpret_recommend(choice_result("b", 0.99), ["a", "b"], chosen="a")
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["source"], "explicit_user_choice")
        self.assertEqual(outcome["recommendation"]["id"], "a")

    def test_ok_when_confident(self):
        outcome = interpret_recommend(choice_result("a", 0.9), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["recommendation"]["id"], "a")

    def test_unsure_is_uncertain_not_a_pick(self):
        outcome = interpret_recommend(choice_result("unsure", 0.97), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "model_returned_unsure")
        self.assertNotIn("recommendation", outcome)

    def test_low_confidence_is_uncertain(self):
        outcome = interpret_recommend(choice_result("a", 0.3), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "confidence_below_threshold")

    def test_disabled_and_unavailable_pass_through(self):
        self.assertEqual(interpret_recommend(JevResult(status="disabled", reason="disabled_by_config"), ["a", "b"], None)["status"], "disabled")
        outcome = interpret_recommend(JevResult(status="unavailable", reason="http_500"), ["a", "b"], None)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertEqual(outcome["reason"], "http_500")


class ClarifyTests(unittest.TestCase):
    def test_five_independent_noul_questions(self):
        questions = build_clarify_questions()
        self.assertEqual(len(questions), 5)
        for question in questions.values():
            self.assertEqual(question["type"], "noul")

    def test_gap_reported(self):
        answers = {q: {"type": "noul", "noul": 0.2} for q in build_clarify_questions()}
        answers["scope_missing"] = {"type": "noul", "noul": 0.9}
        outcome = interpret_clarify(JevResult(status="ok", answers=answers))
        self.assertEqual(outcome["action"], "ask")
        self.assertEqual(outcome["gaps"][0]["dimension"], "scope_missing")

    def test_proceed_when_no_gaps_and_sufficient(self):
        answers = {q: {"type": "noul", "noul": 0.2} for q in build_clarify_questions()}
        answers["evidence_sufficient"] = {"type": "noul", "noul": 0.8}
        outcome = interpret_clarify(JevResult(status="ok", answers=answers))
        self.assertEqual(outcome["action"], "proceed")
        self.assertTrue(outcome["assumptions_ok"])

    def test_conflicting_signals_are_uncertain(self):
        answers = {q: {"type": "noul", "noul": 0.2} for q in build_clarify_questions()}
        answers["evidence_sufficient"] = {"type": "noul", "noul": 0.1}
        outcome = interpret_clarify(JevResult(status="ok", answers=answers))
        self.assertEqual(outcome["action"], "uncertain")
        self.assertEqual(outcome["reason"], "conflicting_signals")

    def test_unavailable_passes_through(self):
        self.assertEqual(interpret_clarify(JevResult(status="unavailable", reason="http_0"))["action"], "unavailable")
        self.assertEqual(interpret_clarify(JevResult(status="disabled", reason="disabled_by_config"))["action"], "disabled")


if __name__ == "__main__":
    unittest.main()