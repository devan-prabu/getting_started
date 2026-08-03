"""SQLite for the prep agent. Separate file from the radar DB.

Two reasons for a second database rather than more tables in `radar.db`: the two
pipelines run on different schedules and each commits its DB back from CI, so
sharing one file would mean two workflows racing on the same blob; and the radar
is about the outside world while this is about you (DECISIONS.md D-15).

Everything is idempotent. `prep run` twice in one evening sends one drill.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Grade, Question, TopicState, utcnow_iso

DEFAULT_DB_PATH = "data/prep.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY,
    topic           TEXT NOT NULL,
    text            TEXT NOT NULL,
    text_key        TEXT NOT NULL,
    expect_json     TEXT,
    difficulty      INTEGER DEFAULT 3,
    stage           TEXT,
    source          TEXT NOT NULL,
    model           TEXT,
    context_version INTEGER,
    asked_on        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_q_topic ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_q_asked ON questions(asked_on);
CREATE INDEX IF NOT EXISTS idx_q_key ON questions(text_key);

CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    sent_on     TEXT NOT NULL,
    message_id  INTEGER,
    UNIQUE(question_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_d_sent_on ON deliveries(sent_on);

CREATE TABLE IF NOT EXISTS answers (
    id            INTEGER PRIMARY KEY,
    question_id   INTEGER NOT NULL REFERENCES questions(id),
    text          TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    update_id     INTEGER,
    score         INTEGER,
    feedback      TEXT,
    missing_json  TEXT,
    grader        TEXT,
    graded_at     TEXT,
    feedback_sent INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_a_question ON answers(question_id);
CREATE INDEX IF NOT EXISTS idx_a_graded ON answers(graded_at);

CREATE TABLE IF NOT EXISTS topic_state (
    topic          TEXT PRIMARY KEY,
    asked_count    INTEGER DEFAULT 0,
    answered_count INTEGER DEFAULT 0,
    avg_score      REAL,
    last_score     INTEGER,
    last_asked     TEXT,
    due_on         TEXT,
    interval_days  INTEGER DEFAULT 1,
    strong_streak  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS context_versions (
    id            INTEGER PRIMARY KEY,
    version       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    prompt_hash   TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    adaptive_json TEXT NOT NULL,
    reason        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL,
    applied    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_e_applied ON events(applied);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_path() -> Path:
    return Path(os.environ.get("PREP_DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str | Path | None = None) -> Path:
    p = Path(path) if path is not None else db_path()
    with connect(p) as conn:
        conn.executescript(SCHEMA)
    return p


# --- kv -------------------------------------------------------------------


def get_kv(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- questions ------------------------------------------------------------


def insert_question(conn: sqlite3.Connection, q: Question) -> int:
    cur = conn.execute(
        """
        INSERT INTO questions
            (topic, text, text_key, expect_json, difficulty, stage, source, model,
             context_version, asked_on, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            q.topic,
            q.text,
            q.dedupe_key(),
            json.dumps(q.expect),
            q.difficulty,
            q.stage,
            q.source,
            q.model,
            q.context_version,
            q.asked_on,
            q.created_at,
        ),
    )
    return int(cur.lastrowid)


def row_to_question(row: sqlite3.Row) -> Question:
    return Question(
        id=int(row["id"]),
        topic=row["topic"],
        text=row["text"],
        expect=json.loads(row["expect_json"] or "[]"),
        difficulty=int(row["difficulty"] or 3),
        stage=row["stage"],
        source=row["source"],
        model=row["model"],
        context_version=row["context_version"],
        asked_on=row["asked_on"],
        created_at=row["created_at"],
    )


def get_question(conn: sqlite3.Connection, question_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def recent_question_keys(conn: sqlite3.Connection, limit: int = 60) -> set[str]:
    """Keys of the most recently asked questions — the repeat guard."""
    rows = conn.execute(
        "SELECT text_key FROM questions WHERE asked_on IS NOT NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return {r["text_key"] for r in rows}


def mark_asked(conn: sqlite3.Connection, question_id: int, on_date: str) -> None:
    conn.execute("UPDATE questions SET asked_on = ? WHERE id = ?", (on_date, question_id))


# --- deliveries -----------------------------------------------------------


def record_delivery(
    conn: sqlite3.Connection,
    question_id: int,
    channel: str,
    sent_on: str,
    message_id: int | None = None,
) -> bool:
    """False means this question was already delivered on this channel."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries (question_id, channel, sent_at, sent_on, message_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (question_id, channel, utcnow_iso(), sent_on, message_id),
    )
    return cur.rowcount > 0


def delivered_on(conn: sqlite3.Connection, sent_on: str, channel: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM deliveries WHERE sent_on = ?"
    params: list[object] = [sent_on]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    return int(conn.execute(sql, params).fetchone()["c"])


def open_questions(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Delivered, not yet answered, newest first — what a bare reply attaches to."""
    return list(
        conn.execute(
            """
            SELECT q.*, d.sent_at AS sent_at
            FROM questions q
            JOIN deliveries d ON d.question_id = q.id
            WHERE NOT EXISTS (SELECT 1 FROM answers a WHERE a.question_id = q.id)
            ORDER BY d.sent_at DESC, q.id DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


# --- answers --------------------------------------------------------------


def insert_answer(
    conn: sqlite3.Connection,
    question_id: int,
    text: str,
    update_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO answers (question_id, text, received_at, update_id) VALUES (?, ?, ?, ?)",
        (question_id, text, utcnow_iso(), update_id),
    )
    return int(cur.lastrowid)


def ungraded_answers(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT a.*, q.topic AS topic, q.text AS question_text, q.expect_json AS expect_json,
               q.difficulty AS difficulty
        FROM answers a JOIN questions q ON q.id = a.question_id
        WHERE a.graded_at IS NULL ORDER BY a.id
    """
    params: list[object] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def record_grade(conn: sqlite3.Connection, answer_id: int, grade: Grade) -> None:
    conn.execute(
        "UPDATE answers SET score = ?, feedback = ?, missing_json = ?, grader = ?, graded_at = ? "
        "WHERE id = ?",
        (
            grade.score,
            grade.feedback,
            json.dumps(grade.missing),
            grade.grader,
            utcnow_iso(),
            answer_id,
        ),
    )


def pending_feedback(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Graded answers whose feedback has not been pushed back to the channel."""
    return list(
        conn.execute(
            """
            SELECT a.*, q.topic AS topic, q.text AS question_text
            FROM answers a JOIN questions q ON q.id = a.question_id
            WHERE a.graded_at IS NOT NULL AND a.feedback_sent = 0
            ORDER BY a.id
            """
        )
    )


def mark_feedback_sent(conn: sqlite3.Connection, answer_id: int) -> None:
    conn.execute("UPDATE answers SET feedback_sent = 1 WHERE id = ?", (answer_id,))


def scores_since(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT q.topic AS topic, a.score AS score, a.graded_at AS graded_at
            FROM answers a JOIN questions q ON q.id = a.question_id
            WHERE a.score IS NOT NULL AND a.graded_at >= ?
            ORDER BY a.graded_at
            """,
            (since_iso,),
        )
    )


def last_answer_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(received_at) AS t FROM answers").fetchone()
    return row["t"] if row else None


# --- topic_state ----------------------------------------------------------


def get_topic_state(conn: sqlite3.Connection, topic: str) -> TopicState:
    row = conn.execute("SELECT * FROM topic_state WHERE topic = ?", (topic,)).fetchone()
    if row is None:
        return TopicState(topic=topic)
    return TopicState(
        topic=row["topic"],
        asked_count=int(row["asked_count"] or 0),
        answered_count=int(row["answered_count"] or 0),
        avg_score=row["avg_score"],
        last_score=row["last_score"],
        last_asked=row["last_asked"],
        due_on=row["due_on"],
        interval_days=int(row["interval_days"] or 1),
        strong_streak=int(row["strong_streak"] or 0),
    )


def all_topic_state(conn: sqlite3.Connection) -> list[TopicState]:
    rows = conn.execute("SELECT topic FROM topic_state")
    return [get_topic_state(conn, r["topic"]) for r in rows]


def save_topic_state(conn: sqlite3.Connection, state: TopicState) -> None:
    conn.execute(
        """
        INSERT INTO topic_state
            (topic, asked_count, answered_count, avg_score, last_score, last_asked,
             due_on, interval_days, strong_streak)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(topic) DO UPDATE SET
            asked_count = excluded.asked_count,
            answered_count = excluded.answered_count,
            avg_score = excluded.avg_score,
            last_score = excluded.last_score,
            last_asked = excluded.last_asked,
            due_on = excluded.due_on,
            interval_days = excluded.interval_days,
            strong_streak = excluded.strong_streak
        """,
        (
            state.topic,
            state.asked_count,
            state.answered_count,
            state.avg_score,
            state.last_score,
            state.last_asked,
            state.due_on,
            state.interval_days,
            state.strong_streak,
        ),
    )


# --- context versions -----------------------------------------------------


def latest_context(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM context_versions ORDER BY version DESC LIMIT 1").fetchone()


def record_context(
    conn: sqlite3.Connection,
    *,
    system_prompt: str,
    prompt_hash: str,
    adaptive_json: str,
    reason: str | None,
) -> int | None:
    """Store a new context version. None means the prompt is unchanged.

    The version number is what every question row is stamped with, so you can
    always tell which edition of your own context produced a given question.
    """
    latest = latest_context(conn)
    if latest is not None and latest["prompt_hash"] == prompt_hash:
        return None
    version = (int(latest["version"]) + 1) if latest is not None else 1
    conn.execute(
        "INSERT INTO context_versions (version, created_at, prompt_hash, system_prompt, "
        "adaptive_json, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (version, utcnow_iso(), prompt_hash, system_prompt, adaptive_json, reason),
    )
    return version


def context_history(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT version, created_at, reason FROM context_versions "
            "ORDER BY version DESC LIMIT ?",
            (limit,),
        )
    )


# --- events ---------------------------------------------------------------


def record_event(conn: sqlite3.Connection, kind: str, payload: str) -> int:
    cur = conn.execute(
        "INSERT INTO events (kind, payload, created_at) VALUES (?, ?, ?)",
        (kind, payload, utcnow_iso()),
    )
    return int(cur.lastrowid)


def unapplied_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM events WHERE applied = 0 ORDER BY id"))


def mark_events_applied(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    conn.executemany("UPDATE events SET applied = 1 WHERE id = ?", [(i,) for i in ids])


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    tables = ("questions", "deliveries", "answers", "topic_state", "context_versions", "events")
    for table in tables:
        out[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    out["answers.graded"] = int(
        conn.execute("SELECT COUNT(*) AS c FROM answers WHERE graded_at IS NOT NULL").fetchone()[
            "c"
        ]
    )
    return out
