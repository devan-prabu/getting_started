from __future__ import annotations

import random

import httpx
import pytest
import respx

from prep.generate import (
    ANTHROPIC_API,
    BankGenerator,
    ClaudeGenerator,
    GenerationError,
    build_generator,
    generate_question,
)
from prep.models import QuestionSource


def anthropic_reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


GOOD_JSON = (
    '{"question": "Your subbase FDT passed but the surface still deflects. '
    'What do you do?", "expect": ["proof roll", "soft spot", "retest"], "difficulty": 4}'
)


class TestBank:
    def test_produces_a_question_from_the_shipped_bank(self, topics_cfg):
        topic = topics_cfg.get("concrete")
        q = BankGenerator(rng=random.Random(1)).generate(topic, difficulty=3)
        assert q.topic == "concrete"
        assert q.expect
        assert q.source == QuestionSource.BANK

    def test_avoids_recently_asked_questions(self, topics_cfg):
        topic = topics_cfg.get("negotiation")
        avoid = [s.q for s in topic.seeds[:-1]]
        q = BankGenerator(rng=random.Random(3)).generate(topic, difficulty=3, avoid=avoid)
        assert q.text == topic.seeds[-1].q

    def test_reuses_rather_than_failing_when_everything_was_asked(self, topics_cfg):
        topic = topics_cfg.get("negotiation")
        q = BankGenerator().generate(topic, difficulty=3, avoid=[s.q for s in topic.seeds])
        assert q.text in [s.q for s in topic.seeds]

    def test_picks_the_closest_difficulty(self, topics_cfg):
        topic = topics_cfg.get("negotiation")
        hardest = max(s.difficulty for s in topic.seeds)
        q = BankGenerator(rng=random.Random(5)).generate(topic, difficulty=5)
        assert q.difficulty == hardest

    def test_is_always_available(self):
        assert BankGenerator().available() is True


class TestClaude:
    @respx.mock
    def test_parses_a_good_response(self, topics_cfg):
        respx.post(ANTHROPIC_API).mock(return_value=anthropic_reply(GOOD_JSON))
        q = ClaudeGenerator(api_key="k", model="claude-sonnet-5").generate(
            topics_cfg.get("earthworks"), system_prompt="you are a coach", difficulty=4
        )
        assert "deflects" in q.text
        assert q.expect == ["proof roll", "soft spot", "retest"]
        assert q.source == QuestionSource.CLAUDE
        assert q.model == "claude-sonnet-5"

    @respx.mock
    def test_json_wrapped_in_prose_is_still_read(self, topics_cfg):
        respx.post(ANTHROPIC_API).mock(
            return_value=anthropic_reply(f"Here you go:\n```json\n{GOOD_JSON}\n```")
        )
        q = ClaudeGenerator(api_key="k").generate(topics_cfg.get("earthworks"), system_prompt="s")
        assert "deflects" in q.text

    @respx.mock
    def test_a_junk_reply_triggers_one_repair_retry(self, topics_cfg):
        route = respx.post(ANTHROPIC_API).mock(
            side_effect=[anthropic_reply("no json here"), anthropic_reply(GOOD_JSON)]
        )
        q = ClaudeGenerator(api_key="k").generate(topics_cfg.get("earthworks"), system_prompt="s")
        assert route.call_count == 2
        assert "deflects" in q.text

    @respx.mock
    def test_two_failures_raise(self, topics_cfg):
        respx.post(ANTHROPIC_API).mock(return_value=anthropic_reply("still not json"))
        with pytest.raises(GenerationError, match="two attempts failed"):
            ClaudeGenerator(api_key="k").generate(topics_cfg.get("earthworks"), system_prompt="s")

    @respx.mock
    def test_an_http_error_is_wrapped(self, topics_cfg):
        respx.post(ANTHROPIC_API).mock(return_value=httpx.Response(429, text="slow down"))
        with pytest.raises(GenerationError):
            ClaudeGenerator(api_key="k").generate(topics_cfg.get("earthworks"), system_prompt="s")

    @respx.mock
    def test_the_system_prompt_and_avoid_list_are_actually_sent(self, topics_cfg):
        route = respx.post(ANTHROPIC_API).mock(return_value=anthropic_reply(GOOD_JSON))
        ClaudeGenerator(api_key="k").generate(
            topics_cfg.get("earthworks"),
            system_prompt="CANDIDATE WORKS AT PROSCAPE",
            avoid=["What is compaction?"],
        )
        body = route.calls[0].request.content.decode()
        assert "CANDIDATE WORKS AT PROSCAPE" in body
        assert "What is compaction?" in body

    def test_unavailable_without_a_key(self):
        assert ClaudeGenerator(api_key="").available() is False


class TestFallback:
    @respx.mock
    def test_a_dead_api_still_produces_tonights_question(self, topics_cfg):
        """The one failure mode that kills the habit is a silent skipped day."""
        respx.post(ANTHROPIC_API).mock(return_value=httpx.Response(500, text="down"))
        q = generate_question(
            ClaudeGenerator(api_key="k"),
            topics_cfg,
            "concrete",
            system_prompt="s",
            difficulty=3,
            avoid=[],
        )
        assert q.source == QuestionSource.BANK
        assert q.text

    def test_an_unknown_topic_is_an_error_not_a_silent_skip(self, topics_cfg):
        with pytest.raises(GenerationError, match="unknown topic"):
            generate_question(
                BankGenerator(),
                topics_cfg,
                "astrophysics",
                system_prompt="",
                difficulty=3,
                avoid=[],
            )


class TestBuildGenerator:
    def test_no_key_means_the_bank(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("PREP_GENERATOR", raising=False)
        assert build_generator().name == "bank"

    def test_a_key_means_claude(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("PREP_GENERATOR", raising=False)
        assert build_generator().name == "claude"

    def test_the_bank_can_be_forced(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("PREP_GENERATOR", "bank")
        assert build_generator().name == "bank"
