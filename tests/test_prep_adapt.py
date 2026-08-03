from __future__ import annotations

import json
from datetime import date, timedelta

from prep import adapt, db
from prep.config import ProfileConfig
from prep.models import Adaptive, Grade, Question, TopicState


def seed_scores(conn, topic: str, scores: list[int]) -> None:
    """Record real answers so the adaptive loop has evidence to work from."""
    state = db.get_topic_state(conn, topic)
    for score in scores:
        qid = db.insert_question(conn, Question(topic=topic, text=f"q{score} for {topic}"))
        aid = db.insert_answer(conn, qid, "an answer")
        db.record_grade(conn, aid, Grade(score=score, feedback="x"))
        from prep import schedule

        state = schedule.next_due(score, state)
    db.save_topic_state(conn, state)


def queue(conn, command: str, argument: str = "") -> None:
    db.record_event(conn, command, json.dumps({"command": command, "argument": argument}))


class TestFocus:
    def test_weak_topics_become_the_focus_weakest_first(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "qms_iso", [40, 45])
        seed_scores(prep_conn, "concrete", [20, 25])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert adaptive.focus[0] == "concrete"
        assert "qms_iso" in adaptive.focus

    def test_strong_topics_go_to_rest(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "landscape_hardscape", [92, 95])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert "landscape_hardscape" in adaptive.resting

    def test_one_strong_answer_is_not_enough_to_rest_a_topic(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        seed_scores(prep_conn, "landscape_hardscape", [95])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert "landscape_hardscape" not in adaptive.resting

    def test_measured_failures_outrank_never_asked_topics(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [30, 35])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert adaptive.focus[0] == "concrete"

    def test_never_asked_topics_fill_the_remaining_slots(self, prep_conn, profile_cfg, topics_cfg):
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert len(adaptive.focus) == adapt.MAX_FOCUS


class TestDifficulty:
    def test_consistently_strong_answers_turn_it_up(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [90, 92, 88, 95])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert adaptive.difficulty == profile_cfg.adaptive.difficulty + 1

    def test_consistently_weak_answers_turn_it_down(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [30, 35, 40, 25])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert adaptive.difficulty == profile_cfg.adaptive.difficulty - 1

    def test_too_few_answers_leaves_the_dial_alone(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [95, 98])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert adaptive.difficulty == profile_cfg.adaptive.difficulty

    def test_it_moves_one_notch_at_a_time(self):
        assert adapt.compute_difficulty(3, 99.0, 10) == 4
        assert adapt.compute_difficulty(3, 10.0, 10) == 2

    def test_it_is_clamped_at_the_ends(self):
        assert adapt.compute_difficulty(5, 99.0, 10) == 5
        assert adapt.compute_difficulty(1, 10.0, 10) == 1

    def test_harder_from_the_phone_wins(self, prep_conn, profile_cfg, topics_cfg):
        queue(prep_conn, "harder")
        adaptive, _ = adapt.build_adaptive(
            prep_conn, profile_cfg, topics_cfg, db.unapplied_events(prep_conn)
        )
        assert adaptive.difficulty == profile_cfg.adaptive.difficulty + 1


class TestCommands:
    def test_a_goal_becomes_a_note_in_the_context(self, prep_conn, profile_cfg, topics_cfg):
        queue(prep_conn, "goal", "chasing the KEO landscape inspector role")
        adaptive, reason = adapt.build_adaptive(
            prep_conn, profile_cfg, topics_cfg, db.unapplied_events(prep_conn)
        )
        assert any("KEO landscape inspector" in n for n in adaptive.notes)
        assert "command" in reason

    def test_focus_from_the_phone_jumps_the_queue(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [20, 25])
        queue(prep_conn, "focus", "negotiation")
        adaptive, _ = adapt.build_adaptive(
            prep_conn, profile_cfg, topics_cfg, db.unapplied_events(prep_conn)
        )
        assert adaptive.focus[0] == "negotiation"

    def test_drop_parks_a_topic_even_when_it_scores_badly(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [20, 25])
        queue(prep_conn, "drop", "concrete")
        adaptive, _ = adapt.build_adaptive(
            prep_conn, profile_cfg, topics_cfg, db.unapplied_events(prep_conn)
        )
        assert "concrete" not in adaptive.focus
        assert "concrete" in adaptive.resting

    def test_an_unknown_topic_in_an_event_is_ignored(self, prep_conn, profile_cfg, topics_cfg):
        queue(prep_conn, "focus", "astrophysics")
        adaptive, _ = adapt.build_adaptive(
            prep_conn, profile_cfg, topics_cfg, db.unapplied_events(prep_conn)
        )
        assert "astrophysics" not in adaptive.focus


class TestNotes:
    def test_user_notes_survive_a_rewrite(self, prep_conn, profile_cfg, topics_cfg):
        profile_cfg.profile.adaptive = Adaptive(notes=["Stated goal (2026-07-01): join Parsons"])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert any("join Parsons" in n for n in adaptive.notes)

    def test_computed_notes_are_regenerated_not_accumulated(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        profile_cfg.profile.adaptive = Adaptive(
            notes=["Interview window opens in 40 days — start mixing in behavioural"]
        )
        profile_cfg.profile.target.interview_window_from = None
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert not any(n.startswith("Interview window") for n in adaptive.notes)

    def test_an_approaching_window_adds_the_shift_note(self, prep_conn, profile_cfg, topics_cfg):
        profile_cfg.profile.target.interview_window_from = (
            date.today() + timedelta(days=20)
        ).isoformat()
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert any("Interview window opens in 20 days" in n for n in adaptive.notes)

    def test_an_open_window_says_so(self, prep_conn, profile_cfg, topics_cfg):
        profile_cfg.profile.target.interview_window_from = date.today().isoformat()
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert any("window is OPEN" in n for n in adaptive.notes)

    def test_notes_are_capped(self, prep_conn, profile_cfg, topics_cfg):
        profile_cfg.profile.adaptive = Adaptive(notes=[f"user note {i}" for i in range(40)])
        adaptive, _ = adapt.build_adaptive(prep_conn, profile_cfg, topics_cfg, [])
        assert len(adaptive.notes) <= adapt.MAX_NOTES


class TestAdaptRun:
    def test_it_writes_the_profile_and_records_a_version(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [20, 25])
        report = adapt.adapt(prep_conn, profile_cfg, topics_cfg)

        assert report.changed is True
        assert report.version == 1
        reloaded = ProfileConfig.load(profile_cfg.path)
        assert "concrete" in reloaded.adaptive.focus
        assert reloaded.adaptive.updated_at is not None

    def test_running_it_twice_with_no_new_evidence_changes_nothing(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        seed_scores(prep_conn, "concrete", [20, 25])
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        second = adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        assert second.changed is False
        assert int(db.latest_context(prep_conn)["version"]) == 1

    def test_new_evidence_creates_a_second_edition(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [20, 25])
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        seed_scores(prep_conn, "ncr_sor", [10, 15])
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        assert int(db.latest_context(prep_conn)["version"]) == 2

    def test_events_are_consumed_exactly_once(self, prep_conn, profile_cfg, topics_cfg):
        queue(prep_conn, "goal", "chase Stantec")
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        assert db.unapplied_events(prep_conn) == []

        report = adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        assert report.applied_events == 0

    def test_events_are_consumed_even_when_the_prompt_did_not_move(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        """Otherwise an unchanged prompt would replay the same event forever."""
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        queue(prep_conn, "harder")
        db.record_event(prep_conn, "noise", json.dumps({"command": "unknown", "argument": ""}))
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        assert db.unapplied_events(prep_conn) == []

    def test_dry_run_writes_nothing(self, prep_conn, profile_cfg, topics_cfg):
        seed_scores(prep_conn, "concrete", [20, 25])
        before = profile_cfg.path.read_text(encoding="utf-8")
        report = adapt.adapt(prep_conn, profile_cfg, topics_cfg, dry_run=True)
        assert report.changed is True
        assert profile_cfg.path.read_text(encoding="utf-8") == before
        assert db.latest_context(prep_conn) is None

    def test_the_hand_written_half_of_the_profile_is_never_touched(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        seed_scores(prep_conn, "concrete", [20, 25])
        adapt.adapt(prep_conn, profile_cfg, topics_cfg)
        reloaded = ProfileConfig.load(profile_cfg.path)
        assert reloaded.profile.identity.current_employer == "Proscape LLC"
        assert reloaded.profile.gaps == profile_cfg.profile.gaps


class TestCurrentContext:
    def test_a_hand_edit_to_the_profile_is_versioned_without_adapt(
        self, prep_conn, profile_cfg, topics_cfg
    ):
        version, _ = adapt.current_context(prep_conn, profile_cfg, topics_cfg)
        assert version == 1

        profile_cfg.profile.target.primary_role = "QA/QC Manager — PMC"
        second, prompt = adapt.current_context(prep_conn, profile_cfg, topics_cfg)
        assert second == 2
        assert "QA/QC Manager — PMC" in prompt

    def test_an_unchanged_profile_reuses_the_version(self, prep_conn, profile_cfg, topics_cfg):
        first, _ = adapt.current_context(prep_conn, profile_cfg, topics_cfg)
        second, _ = adapt.current_context(prep_conn, profile_cfg, topics_cfg)
        assert first == second

    def test_measured_state_reaches_the_prompt(self, prep_conn, profile_cfg, topics_cfg):
        db.save_topic_state(
            prep_conn, TopicState(topic="qms_iso", avg_score=38.0, answered_count=4)
        )
        _, prompt = adapt.current_context(prep_conn, profile_cfg, topics_cfg)
        assert "qms_iso" in prompt
