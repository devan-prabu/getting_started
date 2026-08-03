from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from prep import db, deliver
from prep.generate import BankGenerator
from prep.grade import HeuristicGrader
from prep.models import Question, today_iso
from prep.telegram import Update

EVENING = datetime(2026, 8, 3, 17, 0, tzinfo=ZoneInfo("UTC"))  # 21:00 in Dubai
MORNING = datetime(2026, 8, 3, 5, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 in Dubai


def run_ask(conn, profile_cfg, topics_cfg, client, *, now=EVENING, **kw):
    return deliver.ask(
        conn,
        profile_cfg,
        topics_cfg,
        client,
        BankGenerator(rng=random.Random(4)),
        system_prompt="system",
        context_version=1,
        now=now,
        rng=random.Random(4),
        **kw,
    )


def deliver_question(conn, text="What is an ITP?", topic="qms_iso", message_id=101) -> int:
    qid = db.insert_question(conn, Question(topic=topic, text=text, expect=["hold point"]))
    db.record_delivery(conn, qid, "telegram", today_iso(), message_id)
    return qid


class TestAsk:
    def test_sends_the_configured_number_of_questions(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        client = fake_telegram()
        report = run_ask(prep_conn, profile_cfg, topics_cfg, client)
        assert report.sent == profile_cfg.profile.delivery.questions_per_day
        assert len(client.sent) == report.sent

    def test_it_never_sends_twice_in_a_day(self, prep_conn, profile_cfg, topics_cfg, fake_telegram):
        client = fake_telegram()
        run_ask(prep_conn, profile_cfg, topics_cfg, client)
        second = run_ask(prep_conn, profile_cfg, topics_cfg, client)
        assert second.sent == 0
        assert second.skipped_reason == "already sent today"

    def test_it_holds_before_the_local_ask_hour(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        report = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(), now=MORNING)
        assert report.sent == 0
        assert "before the local ask hour" in report.skipped_reason

    def test_force_overrides_both_guards(self, prep_conn, profile_cfg, topics_cfg, fake_telegram):
        client = fake_telegram()
        run_ask(prep_conn, profile_cfg, topics_cfg, client)
        forced = run_ask(prep_conn, profile_cfg, topics_cfg, client, force=True, now=MORNING)
        assert forced.sent > 0

    def test_a_queued_slash_ask_fires_once_then_clears(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        db.set_kv(prep_conn, deliver.KV_FORCE_ASK, "1")
        first = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(), now=MORNING)
        assert first.sent > 0
        assert db.get_kv(prep_conn, deliver.KV_FORCE_ASK) == "0"
        second = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(), now=MORNING)
        assert second.sent == 0

    def test_pause_stops_the_drill(self, prep_conn, profile_cfg, topics_cfg, fake_telegram):
        later = (EVENING + timedelta(days=5)).date().isoformat()
        db.set_kv(prep_conn, deliver.KV_PAUSED_UNTIL, later)
        report = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram())
        assert report.sent == 0
        assert "paused" in report.skipped_reason

    def test_a_send_failure_is_counted_not_recorded_as_delivered(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        report = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(fail_on_send=True))
        assert report.sent == 0
        assert report.failed > 0
        assert db.delivered_on(prep_conn, "2026-08-03") == 0

    def test_questions_are_stamped_with_the_context_edition(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram())
        row = prep_conn.execute("SELECT context_version FROM questions LIMIT 1").fetchone()
        assert row["context_version"] == 1

    def test_the_same_question_is_not_asked_twice_in_one_evening(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(), count=5)
        keys = [r["text_key"] for r in prep_conn.execute("SELECT text_key FROM questions")]
        assert len(keys) == len(set(keys))

    def test_the_message_names_the_topic_and_asks_for_a_reply(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        client = fake_telegram()
        run_ask(prep_conn, profile_cfg, topics_cfg, client, count=1)
        assert "Drill 1/1" in client.sent[0]
        assert "Reply to this message" in client.sent[0]

    def test_asking_marks_the_topic_so_it_does_not_repeat_tomorrow(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        report = run_ask(prep_conn, profile_cfg, topics_cfg, fake_telegram(), count=1)
        state = db.get_topic_state(prep_conn, report.topics[0])
        assert state.asked_count == 1
        assert state.last_asked == "2026-08-03"


class TestPoll:
    def test_a_reply_attaches_to_its_own_question(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        first = deliver_question(prep_conn, "First question", message_id=101)
        deliver_question(prep_conn, "Second question", message_id=102)
        client = fake_telegram([Update(update_id=1, text="my answer", reply_to_message_id=101)])

        report = deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert report.answers == 1
        row = prep_conn.execute("SELECT question_id FROM answers").fetchone()
        assert int(row["question_id"]) == first

    def test_a_bare_message_attaches_to_the_newest_open_question(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        deliver_question(prep_conn, "First question", message_id=101)
        newest = deliver_question(prep_conn, "Second question", message_id=102)
        client = fake_telegram([Update(update_id=1, text="an answer with no reply")])

        deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        row = prep_conn.execute("SELECT question_id FROM answers").fetchone()
        assert int(row["question_id"]) == newest

    def test_an_answer_with_nothing_open_is_ignored_not_lost_to_an_error(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        client = fake_telegram([Update(update_id=1, text="hello?")])
        report = deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert report.ignored == 1
        assert report.answers == 0

    def test_the_offset_advances_so_updates_are_read_once(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        deliver_question(prep_conn)
        client = fake_telegram([Update(update_id=7, text="an answer")])
        deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert db.get_kv(prep_conn, deliver.KV_OFFSET) == "7"

        again = deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert again.answers == 0

    def test_commands_are_answered_and_not_stored_as_answers(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        deliver_question(prep_conn)
        client = fake_telegram([Update(update_id=1, text="/status")])
        report = deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert report.commands == 1
        assert report.answers == 0
        assert report.replies_sent == 1


class TestCommands:
    def test_goal_is_queued_for_adapt(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        reply = deliver.handle_command(
            prep_conn, Command(name="goal", argument="chase KEO"), profile_cfg, topics_cfg
        )
        assert "Saved" in reply
        assert len(db.unapplied_events(prep_conn)) == 1

    def test_focus_rejects_an_unknown_topic_with_help(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        reply = deliver.handle_command(
            prep_conn, Command(name="focus", argument="astrophysics"), profile_cfg, topics_cfg
        )
        assert "/topics" in reply
        assert db.unapplied_events(prep_conn) == []

    def test_focus_accepts_a_real_topic(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        deliver.handle_command(
            prep_conn, Command(name="focus", argument="concrete"), profile_cfg, topics_cfg
        )
        assert len(db.unapplied_events(prep_conn)) == 1

    def test_skip_records_a_miss_and_reschedules(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        deliver_question(prep_conn, topic="concrete")
        reply = deliver.handle_command(prep_conn, Command(name="skip"), profile_cfg, topics_cfg)
        assert "concrete" in reply
        state = db.get_topic_state(prep_conn, "concrete")
        assert state.last_score == 0
        assert state.interval_days == 1
        assert db.open_questions(prep_conn) == []

    def test_self_scoring_records_and_reschedules(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        qid = deliver_question(prep_conn, topic="concrete")
        db.insert_answer(prep_conn, qid, "my answer")
        reply = deliver.handle_command(
            prep_conn, Command(name="score", argument="90"), profile_cfg, topics_cfg
        )
        assert "90/100" in reply
        assert db.get_topic_state(prep_conn, "concrete").last_score == 90
        assert db.ungraded_answers(prep_conn) == []

    def test_pause_and_resume(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        deliver.handle_command(
            prep_conn, Command(name="pause", argument="5"), profile_cfg, topics_cfg
        )
        assert deliver.paused_until(prep_conn) is not None
        deliver.handle_command(prep_conn, Command(name="resume"), profile_cfg, topics_cfg)
        assert deliver.paused_until(prep_conn) is None

    def test_help_lists_the_steering_commands(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        reply = deliver.handle_command(prep_conn, Command(name="help"), profile_cfg, topics_cfg)
        for command in ("/goal", "/focus", "/harder", "/context", "/pause"):
            assert command in reply

    def test_context_reports_the_live_edition(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        reply = deliver.handle_command(prep_conn, Command(name="context"), profile_cfg, topics_cfg)
        assert "Context edition" in reply

    def test_an_unknown_command_says_so(self, prep_conn, profile_cfg, topics_cfg):
        from prep.models import Command

        reply = deliver.handle_command(prep_conn, Command(name="banana"), profile_cfg, topics_cfg)
        assert "/help" in reply


class TestGrading:
    def test_it_grades_scores_and_pushes_feedback_back(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        qid = db.insert_question(
            prep_conn,
            Question(topic="qms_iso", text="What is a hold point?", expect=["cannot proceed"]),
        )
        db.record_delivery(prep_conn, qid, "telegram", today_iso(), 1)
        db.insert_answer(prep_conn, qid, "Work cannot proceed past it without the consultant.")

        client = fake_telegram()
        report = deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, client)
        assert report.graded == 1
        assert report.feedback_sent == 1
        assert "/100" in client.sent[0]

    def test_grading_updates_the_topic_schedule(self, prep_conn, topics_cfg, fake_telegram):
        qid = db.insert_question(
            prep_conn, Question(topic="concrete", text="q", expect=["curing", "mix design"])
        )
        db.insert_answer(prep_conn, qid, "no idea at all about any of this really")
        deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, fake_telegram())
        state = db.get_topic_state(prep_conn, "concrete")
        assert state.answered_count == 1
        assert state.interval_days == 1

    def test_feedback_is_never_sent_twice(self, prep_conn, topics_cfg, fake_telegram):
        qid = db.insert_question(prep_conn, Question(topic="concrete", text="q", expect=["a"]))
        db.insert_answer(prep_conn, qid, "an answer that is long enough to count as real")
        client = fake_telegram()
        deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, client)
        deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, client)
        assert len(client.sent) == 1

    def test_a_send_outage_leaves_feedback_queued_for_the_next_run(
        self, prep_conn, topics_cfg, fake_telegram
    ):
        qid = db.insert_question(prep_conn, Question(topic="concrete", text="q", expect=["a"]))
        db.insert_answer(prep_conn, qid, "an answer that is long enough to count as real")
        deliver.grade_pending(
            prep_conn, HeuristicGrader(), topics_cfg, fake_telegram(fail_on_send=True)
        )
        assert len(db.pending_feedback(prep_conn)) == 1

        good = fake_telegram()
        deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, good)
        assert len(good.sent) == 1

    def test_nothing_to_grade_is_not_an_error(self, prep_conn, topics_cfg, fake_telegram):
        report = deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, fake_telegram())
        assert report.graded == 0


class TestStatusRendering:
    def test_status_names_the_weakest_topic(self, prep_conn, topics_cfg):
        from prep.models import TopicState

        db.save_topic_state(
            prep_conn, TopicState(topic="qms_iso", avg_score=32.0, answered_count=3)
        )
        db.save_topic_state(
            prep_conn, TopicState(topic="concrete", avg_score=91.0, answered_count=3)
        )
        text = deliver.render_status(prep_conn, topics_cfg)
        assert "Weakest" in text
        assert "QMS and ISO 9001" in text


class TestEndToEnd:
    def test_a_full_day_ask_answer_grade_adapt(
        self, prep_conn, profile_cfg, topics_cfg, fake_telegram
    ):
        """The whole loop, offline: no API key, no network, still works."""
        from prep import adapt as adapt_mod

        client = fake_telegram()
        asked = run_ask(prep_conn, profile_cfg, topics_cfg, client, count=2)
        assert asked.sent == 2

        open_rows = db.open_questions(prep_conn)
        client.updates = [
            Update(update_id=i, text="A short and mostly wrong answer.", message_id=200 + i)
            for i, _ in enumerate(open_rows, start=1)
        ]
        # Each bare answer consumes the newest open question, so two answers in
        # a row walk down the stack rather than piling onto one question.
        polled = deliver.poll(prep_conn, client, profile_cfg, topics_cfg)
        assert polled.answers == 2
        assert db.open_questions(prep_conn) == []

        graded = deliver.grade_pending(prep_conn, HeuristicGrader(), topics_cfg, client)
        assert graded.graded == 2

        report = adapt_mod.adapt(prep_conn, profile_cfg, topics_cfg)
        assert report.changed is True
        assert report.version == 1
