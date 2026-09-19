import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_adapter import JevResult  # noqa: E402
from jev_gates import (  # noqa: E402
    DIMENSION_KEYS,
    PolicyError,
    TRANSITIONS,
    build_readiness_questions,
    build_review_questions,
    interpret_readiness,
    interpret_review,
    validate_text,
    validate_transition,
)


def readiness_result(spec=0.9, testable=0.9, blockers=0.9, score=3.5, blocker_kind="none_applicable"):
    return JevResult(
        status="ok",
        answers={
            "spec_specific": {"type": "noul", "noul": spec},
            "requirements_testable": {"type": "noul", "noul": testable},
            "no_blockers": {"type": "noul", "noul": blockers},
            "ready_score": {"type": "score", "score": score, "confidence": 0.9,
                            "probabilities": {str(min(int(round(score)), 4)): 1.0}},
            "blocker_kind": {"type": "choice", "choice": blocker_kind,
                             "probabilities": {blocker_kind: 1.0}, "confidence": 1.0},
        },
    )


def review_result(spec=0.9, regression=0.9, security=0.9, dims=None, failure_kind="none_applicable"):
    if dims is None:
        dims = {key: 5.0 for key in DIMENSION_KEYS}  # raw 5.0 -> 7.0 on the 2-10 scale
    return JevResult(
        status="ok",
        answers={
            "gate_spec": {"type": "noul", "noul": spec},
            "gate_no_regression": {"type": "noul", "noul": regression},
            "gate_security": {"type": "noul", "noul": security},
            "failure_kind": {"type": "choice", "choice": failure_kind,
                             "probabilities": {failure_kind: 1.0}, "confidence": 1.0},
            **{
                key: {"type": "score", "score": value, "confidence": 0.9,
                      "probabilities": {str(min(int(round(value)), 8)): 1.0}}
                for key, value in dims.items()
            },
        },
    )


class TransitionTests(unittest.TestCase):
    def test_accepts_known_transitions(self):
        for transition in TRANSITIONS:
            self.assertEqual(validate_transition(transition), transition)

    def test_rejects_unknown(self):
        for raw in ("", "planning_to_sprinting", "../etc", "free_form"):
            with self.assertRaises(PolicyError):
                validate_transition(raw)


class TextTests(unittest.TestCase):
    def test_bounds_and_rejects_empty(self):
        with self.assertRaises(PolicyError):
            validate_text("", "artifact", 100)
        with self.assertRaises(PolicyError):
            validate_text("   \n ", "artifact", 100)
        self.assertEqual(len(validate_text("x" * 5000, "artifact", 100)), 100)


class QuestionShapeTests(unittest.TestCase):
    def test_readiness_questions_match_contract(self):
        questions = build_readiness_questions()
        self.assertEqual(len(questions), 5)
        for qid, question in questions.items():
            self.assertIn(question["type"], ("noul", "choice", "score"))
            self.assertIn("instructions", question)
            if question["type"] in ("choice", "score"):
                self.assertIn("criteria", question)
            if question["type"] == "noul":
                self.assertIn("proposition", question)

    def test_review_questions_match_contract(self):
        questions = build_review_questions()
        self.assertEqual(len(questions), 8)
        self.assertEqual(len(questions["dim_correctness"]["criteria"]), 9)  # 2-10 scale

    def test_blocker_choice_includes_other_and_none(self):
        criteria = build_readiness_questions()["blocker_kind"]["criteria"]
        self.assertIn("other", criteria)
        self.assertIn("none_applicable", criteria)

    def test_failure_choice_includes_other_and_none(self):
        criteria = build_review_questions()["failure_kind"]["criteria"]
        self.assertIn("other", criteria)
        self.assertIn("none_applicable", criteria)


class ReadinessInterpretationTests(unittest.TestCase):
    def test_proceed_when_all_gates_pass(self):
        outcome = interpret_readiness(readiness_result())
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["verdict"], "proceed")
        self.assertEqual(outcome["gates_failed"], [])
        self.assertIsNone(outcome["blocker_kind"])

    def test_hold_on_spec_gate(self):
        outcome = interpret_readiness(readiness_result(spec=0.30, blocker_kind="scope_vague"))
        self.assertEqual(outcome["verdict"], "hold")
        self.assertIn("spec_specific", outcome["gates_failed"])
        self.assertEqual(outcome["blocker_kind"], "scope_vague")

    def test_hold_on_score_below_minimum(self):
        outcome = interpret_readiness(readiness_result(score=2.9))
        self.assertEqual(outcome["verdict"], "hold")
        self.assertIn("ready_score", outcome["gates_failed"])

    def test_hold_classifies_blocker(self):
        outcome = interpret_readiness(readiness_result(blockers=0.2, blocker_kind="open_questions"))
        self.assertEqual(outcome["blocker_kind"], "open_questions")

    def test_hold_defaults_blocker_to_other_when_none_applicable(self):
        outcome = interpret_readiness(readiness_result(testable=0.1, blocker_kind="none_applicable"))
        self.assertEqual(outcome["verdict"], "hold")
        self.assertEqual(outcome["blocker_kind"], "other")

    def test_non_ok_statuses_pass_through(self):
        for status, reason in (("disabled", "disabled_by_config"), ("unavailable", "http_500")):
            outcome = interpret_readiness(JevResult(status=status, reason=reason))
            self.assertEqual(outcome["status"], status)
            self.assertEqual(outcome["reason"], reason)

    def test_none_result_is_unavailable(self):
        self.assertEqual(interpret_readiness(None)["status"], "unavailable")

    def test_missing_answer_is_unavailable(self):
        result = readiness_result()
        result.answers.pop("no_blockers")
        self.assertEqual(interpret_readiness(result)["reason"], "missing_or_invalid_answer_no_blockers")


class ReviewInterpretationTests(unittest.TestCase):
    def test_first_pass_when_gates_and_dims_pass(self):
        outcome = interpret_review(review_result())
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["verdict"], "first_pass")
        self.assertIsNone(outcome["failure_kind"])
        self.assertEqual(outcome["dimensions"]["dim_correctness"], 7.0)  # raw + 2

    def test_rework_on_spec_gate(self):
        outcome = interpret_review(review_result(spec=0.5))
        self.assertEqual(outcome["verdict"], "rework")
        self.assertIn("gate_spec", outcome["gates_failed"])
        self.assertEqual(outcome["failure_kind"], "other")  # none_applicable maps to other

    def test_rework_classifies_failure(self):
        outcome = interpret_review(review_result(spec=0.2, failure_kind="partial-implementation"))
        self.assertEqual(outcome["failure_kind"], "partial-implementation")

    def test_rework_on_dimension_below_pass(self):
        outcome = interpret_review(review_result(dims={"dim_correctness": 5.0, "dim_quality": 5.0,
                                                        "dim_tests": 1.5, "dim_bmad": 5.0}))
        self.assertEqual(outcome["verdict"], "rework")
        self.assertIn("dim_tests", outcome["gates_failed"])  # raw 1.5 -> 3.5 < 5.0

    def test_non_ok_statuses_pass_through(self):
        outcome = interpret_review(JevResult(status="unavailable", reason="call_budget_exhausted"))
        self.assertEqual(outcome["status"], "unavailable")
        self.assertEqual(outcome["reason"], "call_budget_exhausted")

    def test_none_result_is_unavailable(self):
        self.assertEqual(interpret_review(None)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()