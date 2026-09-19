import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_adapter import JevResult  # noqa: E402
from jev_policy import (  # noqa: E402
    FIT_RUBRIC,
    PolicyError,
    apply_request_integrity,
    build_integrity_questions,
    build_recommend_questions,
    build_state,
    interpret_recommend,
    parse_evidence,
    sanitize_candidate_id,
)


def choice_result(choice, confidence, probabilities=None, match=0.9, fit=1.9):
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
            },
            "matches": {"type": "noul", "noul": match},
            "fit": {
                "type": "score",
                "score": fit,
                "confidence": 0.9,
                "probabilities": {str(min(int(round(fit)), 2)): 1.0},
            },
        },
    )


def integrity_result(noul):
    return JevResult(status="ok", answers={"request_integrity": {"type": "noul", "noul": noul}})


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
        self.assertEqual(state["request"], "deploy my model")
        self.assertEqual(state["evidence"]["goal"], "runpod")

    def test_state_is_capped(self):
        state = build_state("x" * 2000, {f"k{i}": "v" * 200 for i in range(10)}, max_chars=500)
        self.assertLessEqual(len(json.dumps(state)), 500)

    def test_state_trims_evidence_before_request(self):
        evidence = {f"k{i}": "v" * 200 for i in range(10)}
        state = build_state("a real request worth keeping", evidence, max_chars=500)
        self.assertEqual(state["request"], "a real request worth keeping")
        self.assertTrue(state["evidence"])
        self.assertLess(len(state["evidence"]), 10)

    def test_state_hard_truncates_request_as_last_resort(self):
        state = build_state("x" * 2000, {}, max_chars=500)
        self.assertLessEqual(len(json.dumps(state)), 500)
        self.assertEqual(len(state["request"]), 460)
        self.assertEqual(state["evidence"], {})


class RecommendQuestionsTests(unittest.TestCase):
    def test_includes_unsure_outcome(self):
        questions = build_recommend_questions(["a", "b"])
        criteria = questions["workflow"]["criteria"]
        self.assertEqual(set(criteria), {"a", "b", "unsure"})
        self.assertEqual(questions["workflow"]["type"], "choice")

    def test_batches_all_three_primitives(self):
        questions = build_recommend_questions(["a", "b"])
        self.assertEqual(questions["workflow"]["type"], "choice")
        self.assertEqual(questions["matches"]["type"], "noul")
        self.assertEqual(questions["fit"]["type"], "score")
        self.assertEqual(questions["fit"]["criteria"], FIT_RUBRIC)
        self.assertEqual(set(build_integrity_questions()), {"request_integrity"})

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

    def test_low_match_gate_is_uncertain(self):
        outcome = interpret_recommend(choice_result("a", 0.9, match=0.2), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "match_below_threshold")
        self.assertEqual(outcome["match"], 0.2)

    def test_low_fit_gate_is_uncertain(self):
        outcome = interpret_recommend(choice_result("a", 0.9, fit=0.8), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "fit_below_threshold")
        self.assertEqual(outcome["fit"], 0.8)

    def test_suspected_injection_is_uncertain(self):
        outcome = apply_request_integrity(
            interpret_recommend(choice_result("a", 0.95), ["a", "b"], None), integrity_result(0.97)
        )
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "suspected_request_injection")
        self.assertEqual(outcome["request_integrity"], 0.97)

    def test_mentioning_injection_without_steering_stays_ok(self):
        outcome = apply_request_integrity(
            interpret_recommend(choice_result("a", 0.9), ["a", "b"], None), integrity_result(0.3)
        )
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["request_integrity"], 0.3)

    def test_failed_integrity_check_abstains(self):
        outcome = apply_request_integrity(
            interpret_recommend(choice_result("a", 0.9), ["a", "b"], None),
            JevResult(status="unavailable", reason="http_500"),
        )
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(outcome["reason"], "integrity_check_unavailable")

    def test_integrity_gate_not_applied_to_uncertain_outcomes(self):
        outcome = interpret_recommend(choice_result("unsure", 0.97), ["a", "b"], None)
        self.assertEqual(outcome["reason"], "model_returned_unsure")

    def test_ok_outcome_carries_all_signals(self):
        outcome = interpret_recommend(choice_result("a", 0.9, match=0.95, fit=1.9), ["a", "b"], chosen=None)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["match"], 0.95)
        self.assertEqual(outcome["fit"], 1.9)

    def test_disabled_and_unavailable_pass_through(self):
        self.assertEqual(
            interpret_recommend(JevResult(status="disabled", reason="disabled_by_config"), ["a", "b"], None)["status"],
            "disabled",
        )
        outcome = interpret_recommend(JevResult(status="unavailable", reason="http_500"), ["a", "b"], None)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertEqual(outcome["reason"], "http_500")


if __name__ == "__main__":
    unittest.main()
