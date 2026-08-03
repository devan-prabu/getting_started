from __future__ import annotations

from datetime import date, timedelta

from prep.models import Adaptive, TopicState
from prep.prompt import (
    adaptive_block,
    build_system_prompt,
    candidate_block,
    prompt_hash,
    render_context_summary,
)


class TestCandidateBlock:
    def test_carries_the_facts_a_question_writer_needs(self, profile_cfg):
        block = candidate_block(profile_cfg.profile)
        assert "Proscape LLC" in block
        assert "consultancy" in block.lower()
        assert "gaps" in block.lower()

    def test_missing_fields_are_omitted_not_rendered_as_none(self, profile_cfg):
        profile = profile_cfg.profile
        profile.identity.years_total = None
        profile.identity.years_gcc = None
        block = candidate_block(profile)
        assert "None" not in block

    def test_salary_floor_appears_when_set(self, profile_cfg):
        profile = profile_cfg.profile
        profile.target.compensation.target_min_aed_month = 18000
        assert "18,000" in candidate_block(profile)

    def test_the_must_beat_current_constraint_is_stated(self, profile_cfg):
        assert "beat the current package" in candidate_block(profile_cfg.profile)

    def test_interview_window_is_rendered_in_days(self, profile_cfg):
        profile = profile_cfg.profile
        profile.target.interview_window_from = (date.today() + timedelta(days=30)).isoformat()
        assert "in 30 days" in candidate_block(profile)


class TestAdaptiveBlock:
    def test_focus_and_difficulty_reach_the_prompt(self, profile_cfg):
        profile = profile_cfg.profile
        profile.adaptive = Adaptive(version=4, focus=["concrete"], difficulty=5, notes=["note one"])
        block = adaptive_block(profile)
        assert "edition 4" in block
        assert "concrete" in block
        assert "hostile panel" in block
        assert "note one" in block

    def test_measured_weakness_is_quoted_with_numbers(self, profile_cfg):
        states = [TopicState(topic="qms_iso", avg_score=41.0, answered_count=3)]
        block = adaptive_block(profile_cfg.profile, states)
        assert "qms_iso (41/100 over 3)" in block

    def test_strong_topics_are_not_listed_as_weak(self, profile_cfg):
        states = [TopicState(topic="concrete", avg_score=92.0, answered_count=4)]
        assert "Measured weak topics" not in adaptive_block(profile_cfg.profile, states)


class TestSystemPrompt:
    def test_contains_all_three_sections(self, profile_cfg):
        prompt = build_system_prompt(profile_cfg.profile)
        assert "You are the interview coach" in prompt
        assert "## The candidate" in prompt
        assert "## Adaptive state" in prompt

    def test_hash_is_stable_for_identical_input(self, profile_cfg):
        a = build_system_prompt(profile_cfg.profile)
        b = build_system_prompt(profile_cfg.profile)
        assert prompt_hash(a) == prompt_hash(b)

    def test_hash_moves_when_the_adaptive_block_changes(self, profile_cfg):
        before = prompt_hash(build_system_prompt(profile_cfg.profile))
        profile_cfg.profile.adaptive = Adaptive(version=2, focus=["ncr_sor"], difficulty=4)
        after = prompt_hash(build_system_prompt(profile_cfg.profile))
        assert before != after

    def test_hash_moves_when_the_goal_changes(self, profile_cfg):
        before = prompt_hash(build_system_prompt(profile_cfg.profile))
        profile_cfg.profile.target.primary_role = "QA/QC Manager — contractor"
        assert prompt_hash(build_system_prompt(profile_cfg.profile)) != before

    def test_hash_moves_when_measured_scores_change(self, profile_cfg):
        before = prompt_hash(build_system_prompt(profile_cfg.profile))
        states = [TopicState(topic="qms_iso", avg_score=30.0, answered_count=2)]
        assert prompt_hash(build_system_prompt(profile_cfg.profile, states)) != before


class TestSummary:
    def test_summary_is_short_and_names_the_edition(self, profile_cfg):
        profile_cfg.profile.adaptive = Adaptive(version=6, focus=["concrete"], difficulty=2)
        summary = render_context_summary(profile_cfg.profile)
        assert "Context edition 6" in summary
        assert "concrete" in summary
        assert len(summary.splitlines()) <= 12
