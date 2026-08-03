from __future__ import annotations

import json

from prep import db
from prep.models import Grade, Question, TopicState


def make_question(topic="concrete", text="Why did the cubes fail?", **kw) -> Question:
    return Question(topic=topic, text=text, expect=["curing", "mix"], **kw)


class TestQuestions:
    def test_insert_and_read_back(self, prep_conn):
        qid = db.insert_question(prep_conn, make_question())
        row = db.get_question(prep_conn, qid)
        restored = db.row_to_question(row)
        assert restored.text == "Why did the cubes fail?"
        assert restored.expect == ["curing", "mix"]

    def test_recent_keys_only_include_asked_questions(self, prep_conn):
        asked = db.insert_question(
            prep_conn, make_question(text="Asked one", asked_on="2026-08-01")
        )
        db.insert_question(prep_conn, make_question(text="Never asked"))
        keys = db.recent_question_keys(prep_conn)
        assert "asked one" in keys
        assert "never asked" not in keys
        assert asked

    def test_dedupe_key_ignores_case_and_spacing(self):
        a = make_question(text="What  is an ITP?")
        b = make_question(text="what is an itp?")
        assert a.dedupe_key() == b.dedupe_key()


class TestDeliveries:
    def test_a_question_is_delivered_once_per_channel(self, prep_conn):
        qid = db.insert_question(prep_conn, make_question())
        assert db.record_delivery(prep_conn, qid, "telegram", "2026-08-03", 11) is True
        assert db.record_delivery(prep_conn, qid, "telegram", "2026-08-03", 12) is False

    def test_delivered_on_counts_the_day(self, prep_conn):
        for i in range(3):
            qid = db.insert_question(prep_conn, make_question(text=f"q{i}"))
            db.record_delivery(prep_conn, qid, "telegram", "2026-08-03", 10 + i)
        assert db.delivered_on(prep_conn, "2026-08-03") == 3
        assert db.delivered_on(prep_conn, "2026-08-04") == 0

    def test_open_questions_excludes_answered_ones(self, prep_conn):
        first = db.insert_question(prep_conn, make_question(text="first"))
        second = db.insert_question(prep_conn, make_question(text="second"))
        db.record_delivery(prep_conn, first, "telegram", "2026-08-03", 1)
        db.record_delivery(prep_conn, second, "telegram", "2026-08-03", 2)
        db.insert_answer(prep_conn, first, "an answer")

        still_open = [int(r["id"]) for r in db.open_questions(prep_conn)]
        assert still_open == [second]

    def test_undelivered_questions_are_not_open(self, prep_conn):
        db.insert_question(prep_conn, make_question())
        assert db.open_questions(prep_conn) == []


class TestAnswers:
    def test_grading_moves_an_answer_out_of_the_queue(self, prep_conn):
        qid = db.insert_question(prep_conn, make_question())
        aid = db.insert_answer(prep_conn, qid, "because the curing failed")
        assert len(db.ungraded_answers(prep_conn)) == 1

        db.record_grade(prep_conn, aid, Grade(score=72, feedback="close", missing=["mix"]))
        assert db.ungraded_answers(prep_conn) == []

        pending = db.pending_feedback(prep_conn)
        assert len(pending) == 1
        assert pending[0]["score"] == 72
        assert json.loads(pending[0]["missing_json"]) == ["mix"]

    def test_feedback_is_sent_once(self, prep_conn):
        qid = db.insert_question(prep_conn, make_question())
        aid = db.insert_answer(prep_conn, qid, "answer")
        db.record_grade(prep_conn, aid, Grade(score=50, feedback="thin"))
        db.mark_feedback_sent(prep_conn, aid)
        assert db.pending_feedback(prep_conn) == []

    def test_scores_since_filters_by_time(self, prep_conn):
        qid = db.insert_question(prep_conn, make_question())
        aid = db.insert_answer(prep_conn, qid, "answer")
        db.record_grade(prep_conn, aid, Grade(score=40, feedback="x"))
        assert len(db.scores_since(prep_conn, "2000-01-01T00:00:00+00:00")) == 1
        assert db.scores_since(prep_conn, "2999-01-01T00:00:00+00:00") == []


class TestTopicState:
    def test_missing_state_reads_as_a_fresh_topic(self, prep_conn):
        state = db.get_topic_state(prep_conn, "concrete")
        assert state.asked_count == 0
        assert state.is_due() is True

    def test_roundtrip(self, prep_conn):
        db.save_topic_state(
            prep_conn,
            TopicState(
                topic="concrete",
                asked_count=3,
                answered_count=2,
                avg_score=64.5,
                last_score=50,
                due_on="2026-08-05",
                interval_days=2,
                strong_streak=1,
            ),
        )
        state = db.get_topic_state(prep_conn, "concrete")
        assert state.avg_score == 64.5
        assert state.interval_days == 2
        assert state.is_due("2026-08-04") is False
        assert state.is_due("2026-08-06") is True

    def test_save_is_an_upsert(self, prep_conn):
        db.save_topic_state(prep_conn, TopicState(topic="concrete", asked_count=1))
        db.save_topic_state(prep_conn, TopicState(topic="concrete", asked_count=9))
        assert db.get_topic_state(prep_conn, "concrete").asked_count == 9
        assert len(db.all_topic_state(prep_conn)) == 1


class TestContextVersions:
    def test_an_unchanged_prompt_creates_no_new_version(self, prep_conn):
        first = db.record_context(
            prep_conn, system_prompt="p", prompt_hash="abc", adaptive_json="{}", reason="first"
        )
        again = db.record_context(
            prep_conn, system_prompt="p", prompt_hash="abc", adaptive_json="{}", reason="same"
        )
        assert first == 1
        assert again is None

    def test_a_changed_prompt_increments(self, prep_conn):
        db.record_context(
            prep_conn, system_prompt="p", prompt_hash="abc", adaptive_json="{}", reason="first"
        )
        second = db.record_context(
            prep_conn, system_prompt="q", prompt_hash="def", adaptive_json="{}", reason="changed"
        )
        assert second == 2
        assert int(db.latest_context(prep_conn)["version"]) == 2
        assert len(db.context_history(prep_conn)) == 2


class TestEvents:
    def test_events_are_applied_once(self, prep_conn):
        eid = db.record_event(prep_conn, "goal", json.dumps({"argument": "chase Parsons"}))
        assert len(db.unapplied_events(prep_conn)) == 1
        db.mark_events_applied(prep_conn, [eid])
        assert db.unapplied_events(prep_conn) == []

    def test_marking_nothing_is_harmless(self, prep_conn):
        db.mark_events_applied(prep_conn, [])


class TestKv:
    def test_default_and_upsert(self, prep_conn):
        assert db.get_kv(prep_conn, "telegram_offset", "0") == "0"
        db.set_kv(prep_conn, "telegram_offset", "42")
        db.set_kv(prep_conn, "telegram_offset", "43")
        assert db.get_kv(prep_conn, "telegram_offset") == "43"


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "prep.db"
    db.init_db(path)
    db.init_db(path)
    with db.connect(path) as conn:
        assert db.counts(conn)["questions"] == 0
