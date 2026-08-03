from __future__ import annotations

import pytest
import yaml

from prep.config import ProfileConfig, TopicsConfig
from prep.models import Adaptive


class TestProfile:
    def test_the_shipped_profile_parses(self, profile_cfg: ProfileConfig):
        profile = profile_cfg.profile
        assert profile.identity.current_employer == "Proscape LLC"
        assert "consultancy" in profile.target.primary_role.lower()
        assert profile.gaps, "the profile ships with a gap list to drill against"

    def test_delivery_defaults_are_sane(self, profile_cfg: ProfileConfig):
        d = profile_cfg.profile.delivery
        assert 0 <= d.ask_hour_local <= 23
        assert 1 <= d.questions_per_day <= 10

    def test_weekend_gets_the_longer_set(self, profile_cfg: ProfileConfig):
        profile = profile_cfg.profile
        assert profile.questions_for("Saturday") == profile.delivery.weekend_questions
        assert profile.questions_for("Tuesday") == profile.delivery.questions_per_day

    def test_write_adaptive_touches_only_the_adaptive_block(self, profile_cfg: ProfileConfig):
        """A rewrite must never eat a hand-written career goal."""
        before = yaml.safe_load(profile_cfg.path.read_text(encoding="utf-8"))
        profile_cfg.write_adaptive(
            Adaptive(version=7, focus=["concrete"], difficulty=5, notes=["chasing Parsons"])
        )
        after = yaml.safe_load(profile_cfg.path.read_text(encoding="utf-8"))

        assert after["adaptive"]["version"] == 7
        assert after["adaptive"]["focus"] == ["concrete"]
        for key in ("identity", "target", "strengths", "gaps", "delivery"):
            assert after[key] == before[key]

    def test_write_adaptive_keeps_the_do_not_edit_banner(self, profile_cfg: ProfileConfig):
        profile_cfg.write_adaptive(Adaptive(version=2))
        assert "MACHINE-MAINTAINED" in profile_cfg.path.read_text(encoding="utf-8")

    def test_rewritten_file_reloads(self, profile_cfg: ProfileConfig):
        profile_cfg.write_adaptive(Adaptive(version=3, focus=["ncr_sor"], difficulty=4))
        reloaded = ProfileConfig.load(profile_cfg.path)
        assert reloaded.adaptive.version == 3
        assert reloaded.adaptive.focus == ["ncr_sor"]
        assert reloaded.profile.identity.current_employer == "Proscape LLC"

    def test_write_without_a_path_is_refused(self):
        cfg = ProfileConfig({"identity": {"name": "x"}})
        with pytest.raises(ValueError, match="not loaded from a file"):
            cfg.write_adaptive(Adaptive())

    def test_a_bad_interview_date_is_dropped_not_fatal(self):
        cfg = ProfileConfig({"target": {"interview_window_from": "next september"}})
        assert cfg.profile.target.interview_window_from is None


class TestTopics:
    def test_the_shipped_bank_is_usable(self, topics_cfg: TopicsConfig):
        assert len(topics_cfg.topics) >= 10
        assert topics_cfg.seed_count() >= 40

    def test_every_topic_has_seeds_with_answer_keys(self, topics_cfg: TopicsConfig):
        """A seed with no `expect` cannot be graded offline — that is a bug."""
        for topic in topics_cfg.topics:
            assert topic.seeds, f"{topic.id} has no banked questions"
            for seed in topic.seeds:
                assert seed.expect, f"{topic.id} has a seed with no answer key: {seed.q[:40]}"

    def test_topic_ids_are_unique(self, topics_cfg: TopicsConfig):
        assert len(topics_cfg.ids) == len(set(topics_cfg.ids))

    def test_the_move_to_consultancy_is_covered(self, topics_cfg: TopicsConfig):
        """The whole point of the agent is the contractor to consultancy jump."""
        assert topics_cfg.get("consultancy_side") is not None
        assert topics_cfg.get("negotiation") is not None

    def test_stages_resolve(self, topics_cfg: TopicsConfig):
        assert topics_cfg.for_stage("consultant_panel")
        assert topics_cfg.for_stage("hr_screen")

    def test_empty_topics_file_is_rejected(self, tmp_path):
        path = tmp_path / "topics.yml"
        path.write_text("topics: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no topics"):
            TopicsConfig.load(path)
