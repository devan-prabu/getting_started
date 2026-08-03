"""Sending the drill, reading the replies, pushing back the grades.

The three verbs the CLI and the cron path are built from:

  - `ask`     — pick topics, generate, send, record.
  - `poll`    — read Telegram, attach answers to questions, queue commands.
  - `grade_pending` — grade what arrived and send the verdict back.

Everything is idempotent per day. `deliveries` has UNIQUE(question_id, channel)
and `ask` refuses to send twice on the same local date, so running the workflow
every two hours produces exactly one drill.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import db, schedule
from .config import ProfileConfig, TopicsConfig
from .generate import Generator, generate_question
from .grade import GraderBackend, grade_answer, parse_self_rating, render_feedback
from .models import Grade, Grader, Question, today_iso
from .prompt import render_context_summary
from .telegram import TelegramClient, TelegramError, Update

log = logging.getLogger(__name__)

KV_OFFSET = "telegram_offset"
KV_FORCE_ASK = "force_ask"
KV_PAUSED_UNTIL = "paused_until"
# How far back a bare (non-reply) message can attach to an open question.
ATTACH_WINDOW = 8


@dataclass
class AskReport:
    sent: int = 0
    skipped_reason: str | None = None
    topics: list[str] = field(default_factory=list)
    failed: int = 0

    def as_line(self) -> str:
        if self.skipped_reason:
            return f"sent=0 ({self.skipped_reason})"
        return f"sent={self.sent} failed={self.failed} topics={','.join(self.topics) or '—'}"


@dataclass
class PollReport:
    answers: int = 0
    commands: int = 0
    replies_sent: int = 0
    ignored: int = 0

    def as_line(self) -> str:
        return (
            f"answers={self.answers} commands={self.commands} "
            f"replies={self.replies_sent} ignored={self.ignored}"
        )


@dataclass
class GradeReport:
    graded: int = 0
    feedback_sent: int = 0
    average: float | None = None

    def as_line(self) -> str:
        avg = f"{self.average:.0f}" if self.average is not None else "—"
        return f"graded={self.graded} feedback_sent={self.feedback_sent} avg={avg}"


# --- ask ------------------------------------------------------------------


def render_question(q: Question, topic_name: str, index: int, total: int) -> str:
    stars = "●" * q.difficulty + "○" * (5 - q.difficulty)
    lines = [
        f"🎯 *Drill {index}/{total}* — {topic_name}  {stars}",
        "",
        q.text,
        "",
        "_Reply to this message with your answer._",
    ]
    return "\n".join(lines)


def paused_until(conn: sqlite3.Connection) -> str | None:
    value = db.get_kv(conn, KV_PAUSED_UNTIL)
    if value and value > today_iso():
        return value
    return None


def ask(
    conn: sqlite3.Connection,
    profile_cfg: ProfileConfig,
    topics_cfg: TopicsConfig,
    client: TelegramClient,
    generator: Generator,
    *,
    system_prompt: str,
    context_version: int,
    count: int | None = None,
    force: bool = False,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> AskReport:
    """Send today's drill, unless it has already gone out or it is too early."""
    profile = profile_cfg.profile
    local = schedule.local_now(profile, now)
    today = local.date().isoformat()

    forced = force or db.get_kv(conn, KV_FORCE_ASK) == "1"
    already = db.delivered_on(conn, today)

    if not forced:
        held = paused_until(conn)
        if held:
            return AskReport(skipped_reason=f"paused until {held}")
        if not schedule.should_send_now(profile, already, now):
            reason = (
                "already sent today"
                if already
                else f"before the local ask hour ({profile.delivery.ask_hour_local}:00 "
                f"{profile.delivery.timezone})"
            )
            return AskReport(skipped_reason=reason)
    # Forced (`--force` or /ask from the phone) sends even if a drill already
    # went out today; the flag is cleared at the end so it fires exactly once.

    wanted = count or schedule.questions_today(profile, now)
    states = {t.id: db.get_topic_state(conn, t.id) for t in topics_cfg.topics}
    chosen = schedule.choose_topics(
        topics_cfg, states, profile, wanted, local.date(), rng or random.Random()
    )
    avoid_keys = db.recent_question_keys(conn)
    avoid_text = _recent_question_texts(conn)

    report = AskReport(topics=chosen)
    for index, topic_id in enumerate(chosen, start=1):
        topic = topics_cfg.get(topic_id)
        if topic is None:
            continue
        question = generate_question(
            generator,
            topics_cfg,
            topic_id,
            system_prompt=system_prompt,
            difficulty=profile.adaptive.difficulty,
            avoid=avoid_text,
            stage=_stage_for(topic.stages, profile),
        )
        if question.dedupe_key() in avoid_keys and question.source != "bank":
            log.info("generated a repeat on %s — asking it anyway, nothing better", topic_id)
        question.context_version = context_version
        question.asked_on = today
        question_id = db.insert_question(conn, question)

        try:
            message_id = client.send(render_question(question, topic.name, index, len(chosen)))
        except TelegramError as exc:
            log.error("could not send drill %d: %s", index, exc)
            report.failed += 1
            continue

        db.record_delivery(conn, question_id, client.name, today, message_id)
        db.save_topic_state(conn, schedule.mark_topic_asked(states[topic_id], today))
        avoid_keys.add(question.dedupe_key())
        avoid_text.append(question.text)
        report.sent += 1

    if forced:
        db.set_kv(conn, KV_FORCE_ASK, "0")
    log.info("ask: %s", report.as_line())
    return report


def _stage_for(stages: list[str], profile) -> str | None:  # noqa: ANN001 - Profile, avoids a cycle
    if not stages:
        return None
    days = profile.target.days_to_interview_window()
    if days is not None and days <= schedule.LATE_WINDOW_DAYS:
        for late in schedule.LATE_STAGES:
            if late in stages:
                return late
    return stages[0]


def _recent_question_texts(conn: sqlite3.Connection, limit: int = 25) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM questions WHERE asked_on IS NOT NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [r["text"] for r in rows]


# --- poll -----------------------------------------------------------------

HELP = """\
*Drill commands*

/ask — send today's drill now
/again — resend the questions still open
/skip — give up on the newest open question (counts as a miss)
/score 70 — grade your last answer yourself

*Steering the agent*

/goal <text> — what you are aiming at now
/target <company> — a company you are chasing
/cv <text> — something new on your CV
/focus <topic> — drill this harder
/drop <topic> — park this topic
/harder · /easier — move the difficulty dial
/note <text> — anything else it should remember

*Checking on it*

/context — the context it is running on now
/status — scores, streak, what is weak
/topics — the topic ids you can focus or drop
/pause 7 — no drills for 7 days · /resume
"""


def poll(
    conn: sqlite3.Connection,
    client: TelegramClient,
    profile_cfg: ProfileConfig,
    topics_cfg: TopicsConfig,
) -> PollReport:
    """Read Telegram once, attach answers, queue commands for adapt."""
    report = PollReport()
    offset_raw = db.get_kv(conn, KV_OFFSET)
    offset = int(offset_raw) + 1 if offset_raw else None

    updates = client.get_updates(offset=offset)
    if not updates:
        return report

    for update in updates:
        db.set_kv(conn, KV_OFFSET, str(update.update_id))
        command = update.command
        if command is not None:
            reply = handle_command(conn, command, profile_cfg, topics_cfg)
            report.commands += 1
            if reply:
                try:
                    client.send(reply)
                    report.replies_sent += 1
                except TelegramError as exc:
                    log.error("could not reply to /%s: %s", command.name, exc)
            continue

        question_id = _attach_to_question(conn, update)
        if question_id is None:
            report.ignored += 1
            log.info("no open question to attach an answer to — ignoring %r", update.text[:40])
            continue
        db.insert_answer(conn, question_id, update.text, update.update_id)
        report.answers += 1

    log.info("poll: %s", report.as_line())
    return report


def _attach_to_question(conn: sqlite3.Connection, update: Update) -> int | None:
    """A reply attaches to its own question; a bare message to the newest open one."""
    if update.reply_to_message_id is not None:
        row = conn.execute(
            "SELECT question_id FROM deliveries WHERE message_id = ?",
            (update.reply_to_message_id,),
        ).fetchone()
        if row is not None:
            return int(row["question_id"])
    open_rows = db.open_questions(conn, limit=ATTACH_WINDOW)
    return int(open_rows[0]["id"]) if open_rows else None


def handle_command(
    conn: sqlite3.Connection,
    command,  # noqa: ANN001 - Command, kept loose to avoid a cycle
    profile_cfg: ProfileConfig,
    topics_cfg: TopicsConfig,
) -> str | None:
    """Answer the ones that are read-only; queue the ones that change context."""
    name, argument = command.name, command.argument

    if name in ("help", "start"):
        return HELP
    if name == "context":
        return render_context_summary(profile_cfg.profile)
    if name == "status":
        return render_status(conn, topics_cfg)
    if name == "topics":
        return "*Topic ids*\n" + "\n".join(f"`{t.id}` — {t.name}" for t in topics_cfg.topics)

    if name == "ask":
        db.set_kv(conn, KV_FORCE_ASK, "1")
        return "Queued — the next run sends a drill."
    if name == "pause":
        days = _int_or(argument, 7)
        db.set_kv(conn, KV_PAUSED_UNTIL, (date.today() + timedelta(days=days)).isoformat())
        return f"Paused for {days} days. /resume when you want it back."
    if name == "resume":
        db.set_kv(conn, KV_PAUSED_UNTIL, "")
        return "Back on. Next drill goes out at the usual hour."

    if name == "skip":
        return _skip_newest(conn)
    if name == "score":
        return _self_score(conn, argument)
    if name == "again":
        return _resend_open(conn, topics_cfg)

    if name in ("goal", "target", "cv", "note", "focus", "drop", "harder", "easier"):
        if name in ("focus", "drop"):
            topic_id = argument.strip().lower().replace(" ", "_")
            if topic_id not in topics_cfg.ids:
                return (
                    f"No topic called `{topic_id}`. Send /topics for the list."
                    if topic_id
                    else f"Usage: /{name} <topic id> — send /topics for the list."
                )
            argument = topic_id
        elif name in ("goal", "target", "cv", "note") and not argument:
            return f"Usage: /{name} <text>"

        db.record_event(
            conn,
            kind=name,
            payload=json.dumps({"command": name, "argument": argument}),
        )
        return _ack(name, argument)

    return f"Unknown command /{name}. Send /help."


def _ack(name: str, argument: str) -> str:
    if name == "harder":
        return "Noted — turning the difficulty up on the next adapt."
    if name == "easier":
        return "Noted — easing off on the next adapt."
    if name == "focus":
        return f"`{argument}` goes to the front of the queue."
    if name == "drop":
        return f"`{argument}` parked. /focus {argument} brings it back."
    return f"Saved. It goes into the context on the next adapt:\n_{argument}_"


def _int_or(value: str, default: int) -> int:
    try:
        return max(1, min(90, int(value.strip())))
    except (TypeError, ValueError):
        return default


def _skip_newest(conn: sqlite3.Connection) -> str:
    rows = db.open_questions(conn, limit=1)
    if not rows:
        return "Nothing open to skip."
    question_id = int(rows[0]["id"])
    answer_id = db.insert_answer(conn, question_id, "(skipped)")
    db.record_grade(
        conn,
        answer_id,
        Grade(
            score=0,
            feedback="Skipped. It comes back tomorrow.",
            missing=json.loads(rows[0]["expect_json"] or "[]"),
            grader=Grader.SELF,
        ),
    )
    db.mark_feedback_sent(conn, answer_id)
    state = schedule.next_due(0, db.get_topic_state(conn, rows[0]["topic"]))
    db.save_topic_state(conn, state)
    return f"Skipped. `{rows[0]['topic']}` is back in tomorrow's queue."


def _self_score(conn: sqlite3.Connection, argument: str) -> str:
    value = parse_self_rating(argument)
    if value is None:
        return "Usage: /score 70 — or /score 7/10."
    row = conn.execute(
        "SELECT a.id, a.question_id, q.topic AS topic FROM answers a "
        "JOIN questions q ON q.id = a.question_id ORDER BY a.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "No answer to score yet."
    db.record_grade(
        conn,
        int(row["id"]),
        Grade(score=value, feedback="Self-scored.", grader=Grader.SELF),
    )
    db.mark_feedback_sent(conn, int(row["id"]))
    db.save_topic_state(conn, schedule.next_due(value, db.get_topic_state(conn, row["topic"])))
    return f"Recorded {value}/100 on `{row['topic']}`."


def _resend_open(conn: sqlite3.Connection, topics_cfg: TopicsConfig) -> str:
    rows = db.open_questions(conn, limit=5)
    if not rows:
        return "Nothing open. /ask for a fresh drill."
    lines = ["*Still open*", ""]
    for row in rows:
        topic = topics_cfg.get(row["topic"])
        lines.append(f"• _{topic.name if topic else row['topic']}_ — {row['text']}")
    return "\n".join(lines)


def render_status(conn: sqlite3.Connection, topics_cfg: TopicsConfig) -> str:
    totals = db.counts(conn)
    states = [db.get_topic_state(conn, t.id) for t in topics_cfg.topics]
    answered = [s for s in states if s.avg_score is not None]
    overall = sum(s.avg_score or 0 for s in answered) / len(answered) if answered else None

    lines = [
        "*Where you are*",
        "",
        f"Questions asked: {totals['deliveries']}",
        f"Answered: {totals['answers']} · graded: {totals['answers.graded']}",
        f"Overall average: {overall:.0f}/100" if overall is not None else "Overall average: —",
        f"Context edition: {totals['context_versions']}",
    ]

    weak = sorted(answered, key=lambda s: s.avg_score or 0)[:3]
    if weak:
        lines += ["", "*Weakest*"]
        lines += [
            f"• {(topics_cfg.get(s.topic).name if topics_cfg.get(s.topic) else s.topic)} — "
            f"{s.avg_score:.0f}/100 over {s.answered_count}"
            for s in weak
        ]
    strong = sorted(answered, key=lambda s: -(s.avg_score or 0))[:3]
    if strong:
        lines += ["", "*Strongest*"]
        lines += [
            f"• {(topics_cfg.get(s.topic).name if topics_cfg.get(s.topic) else s.topic)} — "
            f"{s.avg_score:.0f}/100 over {s.answered_count}"
            for s in strong
        ]
    untouched = [s for s in states if s.asked_count == 0]
    if untouched:
        lines += ["", f"Never asked: {len(untouched)} topic(s)"]
    return "\n".join(lines)


# --- grading --------------------------------------------------------------


def grade_pending(
    conn: sqlite3.Connection,
    grader: GraderBackend,
    topics_cfg: TopicsConfig,
    client: TelegramClient | None = None,
    *,
    system_prompt: str = "",
    limit: int | None = None,
    today: date | None = None,
) -> GradeReport:
    """Grade every ungraded answer and push the verdict back to the channel."""
    report = GradeReport()
    scores: list[int] = []

    for row in db.ungraded_answers(conn, limit=limit):
        expect = json.loads(row["expect_json"] or "[]")
        grade = grade_answer(
            grader,
            row["question_text"],
            expect,
            row["text"],
            system_prompt=system_prompt,
        )
        db.record_grade(conn, int(row["id"]), grade)
        db.save_topic_state(
            conn, schedule.next_due(grade.score, db.get_topic_state(conn, row["topic"]), today)
        )
        scores.append(grade.score)
        report.graded += 1

    if client is not None:
        for row in db.pending_feedback(conn):
            topic = topics_cfg.get(row["topic"])
            grade = Grade(
                score=int(row["score"] or 0),
                feedback=row["feedback"] or "",
                missing=json.loads(row["missing_json"] or "[]"),
                grader=row["grader"] or Grader.HEURISTIC,
            )
            try:
                client.send(
                    render_feedback(
                        row["question_text"], grade, topic.name if topic else row["topic"]
                    )
                )
            except TelegramError as exc:
                log.error("could not send feedback for answer %s: %s", row["id"], exc)
                continue
            db.mark_feedback_sent(conn, int(row["id"]))
            report.feedback_sent += 1

    if scores:
        report.average = sum(scores) / len(scores)
    log.info("grade: %s", report.as_line())
    return report
