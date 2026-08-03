from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from prep import schedule
from prep.models import Adaptive, TopicState


def at(hour: int, tz: str = "UTC") -> datetime:
    return datetime(2026, 8, 3, hour, 0, tzinfo=ZoneInfo(tz))


class TestSendWindow:
    def test_not_before_the_local_hour(self, profile_cfg):
        profile = profile_cfg.profile
        profile.delivery.timezone = "Asia/Dubai"
        profile.delivery.ask_hour_local = 20
        # 14:00 UTC is 18:00 in Dubai — too early.
        assert schedule.should_send_now(profile, 0, at(14)) is False
        # 16:00 UTC is 20:00 in Dubai.
        assert schedule.should_send_now(profile, 0, at(16)) is True

    def test_never_twice_in_one_day(self, profile_cfg):
        assert schedule.should_send_now(profile_cfg.profile, 1, at(23)) is False

    def test_an_unknown_timezone_falls_back_to_utc(self, profile_cfg):
        profile_cfg.profile.delivery.timezone = "Mars/Olympus"
        assert schedule.local_now(profile_cfg.profile, at(9)).hour == 9

    def test_weekend_count(self, profile_cfg):
        profile = profile_cfg.profile
        profile.delivery.timezone = "UTC"
        saturday = datetime(2026, 8, 1, 21, tzinfo=ZoneInfo("UTC"))
        tuesday = datetime(2026, 8, 4, 21, tzinfo=ZoneInfo("UTC"))
        assert schedule.questions_today(profile, saturday) == profile.delivery.weekend_questions
        assert schedule.questions_today(profile, tuesday) == profile.delivery.questions_per_day


class TestPriority:
    def test_a_weak_topic_outranks_a_strong_one(self, profile_cfg):
        profile = profile_cfg.profile
        weak = TopicState(topic="a", avg_score=35.0, answered_count=3, last_asked="2026-08-01")
        strong = TopicState(topic="b", avg_score=95.0, answered_count=3, last_asked="2026-08-01")
        today = date(2026, 8, 3)
        assert schedule.topic_priority(
            "a", weak, profile, 1.0, [], today
        ) > schedule.topic_priority("b", strong, profile, 1.0, [], today)

    def test_focus_pushes_a_topic_up(self, profile_cfg):
        profile = profile_cfg.profile
        state = TopicState(topic="a", last_asked="2026-08-02")
        plain = schedule.topic_priority("a", state, profile, 1.0, [], date(2026, 8, 3))
        profile.adaptive = Adaptive(focus=["a"])
        focused = schedule.topic_priority("a", state, profile, 1.0, [], date(2026, 8, 3))
        assert focused > plain + 2

    def test_resting_pushes_a_topic_down_without_banning_it(self, profile_cfg):
        profile = profile_cfg.profile
        profile.adaptive = Adaptive(resting=["a"])
        state = TopicState(topic="a", last_asked="2026-08-02")
        score = schedule.topic_priority("a", state, profile, 1.0, [], date(2026, 8, 3))
        assert score > 0

    def test_a_topic_not_yet_due_is_damped(self, profile_cfg):
        profile = profile_cfg.profile
        due = TopicState(topic="a", last_asked="2026-08-02", due_on="2026-08-03")
        not_due = TopicState(topic="a", last_asked="2026-08-02", due_on="2026-08-20")
        today = date(2026, 8, 3)
        assert schedule.topic_priority("a", due, profile, 1.0, [], today) > schedule.topic_priority(
            "a", not_due, profile, 1.0, [], today
        )

    def test_a_never_asked_topic_beats_one_asked_today(self, profile_cfg):
        profile = profile_cfg.profile
        today = date(2026, 8, 3)
        fresh = schedule.topic_priority("a", TopicState(topic="a"), profile, 1.0, [], today)
        used = schedule.topic_priority(
            "b", TopicState(topic="b", last_asked="2026-08-03"), profile, 1.0, [], today
        )
        assert fresh > used

    def test_behavioural_climbs_as_the_interview_window_approaches(self, profile_cfg):
        profile = profile_cfg.profile
        state = TopicState(topic="behavioural", last_asked="2026-08-02")
        today = date(2026, 8, 3)

        profile.target.interview_window_from = None
        far = schedule.topic_priority("behavioural", state, profile, 1.0, ["behavioural"], today)
        profile.target.interview_window_from = (today + timedelta(days=5)).isoformat()
        near = schedule.topic_priority("behavioural", state, profile, 1.0, ["behavioural"], today)
        assert near > far


class TestChooseTopics:
    def test_returns_the_requested_count_without_repeats(self, topics_cfg, profile_cfg):
        chosen = schedule.choose_topics(
            topics_cfg, {}, profile_cfg.profile, 3, date(2026, 8, 3), random.Random(1)
        )
        assert len(chosen) == 3
        assert len(set(chosen)) == 3

    def test_asking_for_more_than_exist_is_capped(self, topics_cfg, profile_cfg):
        chosen = schedule.choose_topics(
            topics_cfg, {}, profile_cfg.profile, 999, date(2026, 8, 3), random.Random(1)
        )
        assert len(chosen) == len(topics_cfg.topics)

    def test_the_weakest_topic_is_picked_first(self, topics_cfg, profile_cfg):
        states = {
            t.id: TopicState(topic=t.id, avg_score=90.0, answered_count=3, last_asked="2026-08-02")
            for t in topics_cfg.topics
        }
        states["concrete"] = TopicState(
            topic="concrete", avg_score=20.0, answered_count=3, last_asked="2026-08-02"
        )
        chosen = schedule.choose_topics(
            topics_cfg, states, profile_cfg.profile, 1, date(2026, 8, 3), random.Random(7)
        )
        assert chosen == ["concrete"]


class TestSpacedRepetition:
    def test_a_bad_answer_comes_back_tomorrow(self):
        state = schedule.next_due(30, TopicState(topic="a", interval_days=8), date(2026, 8, 3))
        assert state.interval_days == 1
        assert state.due_on == "2026-08-04"
        assert state.strong_streak == 0

    def test_a_strong_answer_doubles_the_interval(self):
        state = schedule.next_due(90, TopicState(topic="a", interval_days=2), date(2026, 8, 3))
        assert state.interval_days == 4
        assert state.strong_streak == 1

    def test_the_interval_is_capped_at_a_fortnight(self):
        state = schedule.next_due(95, TopicState(topic="a", interval_days=12), date(2026, 8, 3))
        assert state.interval_days == 14

    def test_the_average_is_a_running_mean(self):
        state = schedule.next_due(80, TopicState(topic="a"), date(2026, 8, 3))
        assert state.avg_score == 80
        state = schedule.next_due(60, state, date(2026, 8, 3))
        assert state.avg_score == 70
        assert state.answered_count == 2

    def test_marking_asked_does_not_touch_scores(self):
        state = schedule.mark_topic_asked(TopicState(topic="a", avg_score=70.0), "2026-08-03")
        assert state.asked_count == 1
        assert state.last_asked == "2026-08-03"
        assert state.avg_score == 70.0
