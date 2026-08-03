from __future__ import annotations

import httpx
import pytest
import respx

from prep.generate import ANTHROPIC_API
from prep.grade import (
    HEURISTIC_CEILING,
    ClaudeGrader,
    GradingError,
    HeuristicGrader,
    build_grader,
    grade_answer,
    parse_self_rating,
    render_feedback,
)
from prep.models import Grade, Grader

EXPECT = ["proof roll", "soft spot", "retest", "subgrade failure"]


class TestHeuristic:
    def test_full_coverage_hits_the_ceiling(self):
        answer = (
            "I would proof roll the area, look for a soft spot below, suspect subgrade "
            "failure and retest after replacing the material."
        )
        grade = HeuristicGrader().grade("q", EXPECT, answer)
        assert grade.score == HEURISTIC_CEILING
        assert grade.missing == []

    def test_partial_coverage_scores_proportionally(self):
        grade = HeuristicGrader().grade("q", EXPECT, "I would proof roll it and retest.")
        assert 0 < grade.score < HEURISTIC_CEILING
        assert "soft spot" in grade.missing

    def test_it_can_never_alone_park_a_topic_as_mastered(self):
        """80 is below the strong line, so coverage scoring cannot rest a topic."""
        from prep.models import STRONG_SCORE

        assert HEURISTIC_CEILING < STRONG_SCORE

    def test_an_empty_answer_scores_zero(self):
        grade = HeuristicGrader().grade("q", EXPECT, "   ")
        assert grade.score == 0
        assert grade.missing == EXPECT

    def test_a_one_word_answer_is_capped(self):
        grade = HeuristicGrader().grade("q", EXPECT, "retest")
        assert grade.score <= 30

    def test_it_says_it_is_only_coverage(self):
        grade = HeuristicGrader().grade("q", EXPECT, "proof roll it")
        assert "no api key" in grade.feedback.lower()

    def test_no_answer_key_gives_an_effort_score_and_says_so(self):
        grade = HeuristicGrader().grade("q", [], "a" * 100)
        assert grade.score == 55
        assert "effort score" in grade.feedback

    def test_matching_ignores_case_and_punctuation(self):
        grade = HeuristicGrader().grade(
            "q", ["proof roll"], "We PROOF-ROLLED the whole area before accepting the layer."
        )
        assert grade.score == HEURISTIC_CEILING


class TestClaudeGrader:
    @respx.mock
    def test_parses_a_verdict(self):
        respx.post(ANTHROPIC_API).mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "text",
                            "text": '{"score": 68, "feedback": "Right area, no numbers.",'
                            ' "missing": ["retest"]}',
                        }
                    ]
                },
            )
        )
        grade = ClaudeGrader(api_key="k", model="claude-sonnet-5").grade("q", EXPECT, "an answer")
        assert grade.score == 68
        assert grade.missing == ["retest"]
        assert grade.grader == Grader.CLAUDE

    @respx.mock
    def test_the_answer_key_is_sent_as_the_rubric(self):
        route = respx.post(ANTHROPIC_API).mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "text", "text": '{"score": 50, "feedback": "x"}'}]}
            )
        )
        ClaudeGrader(api_key="k").grade("q", ["proof roll"], "answer")
        assert "proof roll" in route.calls[0].request.content.decode()

    @respx.mock
    def test_a_scoreless_verdict_is_an_error(self):
        respx.post(ANTHROPIC_API).mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "text", "text": '{"feedback": "hmm"}'}]}
            )
        )
        with pytest.raises(GradingError):
            ClaudeGrader(api_key="k").grade("q", EXPECT, "answer")

    @respx.mock
    def test_a_failed_call_falls_back_rather_than_leaving_it_ungraded(self):
        respx.post(ANTHROPIC_API).mock(return_value=httpx.Response(503, text="down"))
        grade = grade_answer(ClaudeGrader(api_key="k"), "q", EXPECT, "I would proof roll it")
        assert grade.grader == Grader.HEURISTIC
        assert grade.score > 0

    def test_scores_are_clamped(self):
        assert Grade(score=140, feedback="x").score == 100
        assert Grade(score=-5, feedback="x").score == 0


class TestBuildGrader:
    def test_no_key_means_heuristic(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("PREP_GRADER", raising=False)
        assert build_grader().name == Grader.HEURISTIC

    def test_a_key_means_claude(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("PREP_GRADER", raising=False)
        assert build_grader().name == Grader.CLAUDE


class TestRendering:
    def test_feedback_carries_the_score_and_the_gap(self):
        grade = Grade(score=45, feedback="Too vague.", missing=["proof roll"])
        text = render_feedback("Why did it deflect?", grade, "Earthworks")
        assert "45/100" in text
        assert "Earthworks" in text
        assert "proof roll" in text
        assert "🔴" in text

    def test_a_strong_score_reads_green(self):
        text = render_feedback("q", Grade(score=90, feedback="Good."))
        assert "🟢" in text

    def test_long_feedback_is_capped_for_a_phone(self):
        grade = Grade(score=50, feedback="word " * 500)
        assert len(grade.feedback) <= 900


class TestSelfRating:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("70", 70), ("7/10", 70), ("85/100", 85), ("  9/10 ", 90), ("200", 100)],
    )
    def test_accepted_forms(self, text, expected):
        assert parse_self_rating(text) == expected

    @pytest.mark.parametrize("text", ["good", "", "pretty solid I think"])
    def test_rejected_forms(self, text):
        assert parse_self_rating(text) is None
