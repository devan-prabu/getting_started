"""The `prep` CLI.

`prep run` is the cron path: poll → grade → adapt → ask. Every command is safely
re-runnable; running it hourly produces one drill a day.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from . import adapt as adapt_mod
from . import db, deliver
from .config import ProfileConfig, TopicsConfig
from .generate import BankGenerator, build_generator
from .grade import build_grader
from .models import Grade, Grader
from .prompt import render_context_summary
from .telegram import StdoutClient, TelegramClient

load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="A daily interview drill on Telegram that retunes itself to your goals.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    _setup_logging(verbose)


def _configs() -> tuple[ProfileConfig, TopicsConfig]:
    return ProfileConfig.load(), TopicsConfig.load()


def _client(dry_run: bool):  # noqa: ANN202 - either client, both duck-typed
    if dry_run:
        return StdoutClient()
    client = TelegramClient()
    if not client.configured():
        console.print(
            "[yellow]Telegram is not configured — printing instead. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.[/yellow]"
        )
        return StdoutClient()
    return client


@app.command()
def init(db_path: Path | None = typer.Option(None, "--db", help="Override PREP_DB_PATH.")) -> None:
    """Create the database and record the first context version."""
    path = db.init_db(db_path)
    profile_cfg, topics_cfg = _configs()
    with db.connect(path) as conn:
        version, _ = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        totals = db.counts(conn)
    console.print(f"[green]DB ready[/green] at {path}")
    console.print(
        f"profile: {profile_cfg.profile.identity.name} → {profile_cfg.profile.target.primary_role}"
    )
    console.print(
        f"{len(topics_cfg.topics)} topics · {topics_cfg.seed_count()} banked questions · "
        f"context edition {version}"
    )
    console.print(f"questions so far: {totals['questions']}")


@app.command()
def ask(
    count: int | None = typer.Option(None, "--count", "-n", help="Override questions per day."),
    force: bool = typer.Option(False, "--force", help="Send even if one already went out today."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print instead of sending."),
    bank: bool = typer.Option(False, "--bank", help="Use the seeded bank, never the API."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Send today's drill."""
    profile_cfg, topics_cfg = _configs()
    generator = BankGenerator() if bank else build_generator()
    client = _client(dry_run)
    with db.connect(db_path) as conn:
        version, system_prompt = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        report = deliver.ask(
            conn,
            profile_cfg,
            topics_cfg,
            client,
            generator,
            system_prompt=system_prompt,
            context_version=version,
            count=count,
            force=force or dry_run,
        )
    console.print(f"[bold]ask[/bold] {report.as_line()} (generator: {generator.name})")


@app.command()
def poll(db_path: Path | None = typer.Option(None, "--db")) -> None:
    """Read replies and commands from Telegram."""
    profile_cfg, topics_cfg = _configs()
    client = TelegramClient()
    if not client.configured():
        console.print("[red]Telegram is not configured — nothing to poll.[/red]")
        raise typer.Exit(1)
    with db.connect(db_path) as conn, client:
        report = deliver.poll(conn, client, profile_cfg, topics_cfg)
    console.print(f"[bold]poll[/bold] {report.as_line()}")


@app.command()
def grade(
    limit: int | None = typer.Option(None, "--limit"),
    no_send: bool = typer.Option(False, "--no-send", help="Grade but do not push feedback back."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Grade answers that have arrived and send the verdicts back."""
    profile_cfg, topics_cfg = _configs()
    grader = build_grader()
    client = None if no_send else _client(False)
    with db.connect(db_path) as conn:
        _, system_prompt = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        report = deliver.grade_pending(
            conn, grader, topics_cfg, client, system_prompt=system_prompt, limit=limit
        )
    console.print(f"[bold]grade[/bold] {report.as_line()} (grader: {grader.name})")


@app.command()
def adapt(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the change, write nothing."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Rewrite the adaptive block and the system prompt from the evidence."""
    profile_cfg, topics_cfg = _configs()
    with db.connect(db_path) as conn:
        report = adapt_mod.adapt(conn, profile_cfg, topics_cfg, dry_run=dry_run)
    console.print(f"[bold]adapt[/bold] {report.as_line()}")
    console.print(f"  reason: {report.reason}")
    if dry_run:
        console.print("[yellow]dry run — profile.yml not written[/yellow]")


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run"),
    bank: bool = typer.Option(False, "--bank", help="Use the seeded bank, never the API."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """poll → grade → adapt → ask. The cron path."""
    profile_cfg, topics_cfg = _configs()
    generator = BankGenerator() if bank else build_generator()
    grader = build_grader()
    client = _client(dry_run)

    with db.connect(db_path) as conn:
        if hasattr(client, "get_updates") and not dry_run:
            p = deliver.poll(conn, client, profile_cfg, topics_cfg)
            console.print(f"[bold]poll[/bold]  {p.as_line()}")

        _, system_prompt = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        g = deliver.grade_pending(
            conn, grader, topics_cfg, None if dry_run else client, system_prompt=system_prompt
        )
        console.print(f"[bold]grade[/bold] {g.as_line()}")

        a = adapt_mod.adapt(conn, profile_cfg, topics_cfg, dry_run=dry_run)
        console.print(f"[bold]adapt[/bold] {a.as_line()}")

        # Re-read the context: adapt may have just changed it.
        version, system_prompt = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        r = deliver.ask(
            conn,
            profile_cfg,
            topics_cfg,
            client,
            generator,
            system_prompt=system_prompt,
            context_version=version,
            force=dry_run,
        )
        console.print(f"[bold]ask[/bold]   {r.as_line()}")


@app.command()
def answer(
    text: str = typer.Argument(..., help="Your answer to the newest open question."),
    question_id: int | None = typer.Option(None, "--question", help="Answer a specific question."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Answer from the terminal instead of the phone."""
    with db.connect(db_path) as conn:
        if question_id is None:
            rows = db.open_questions(conn, limit=1)
            if not rows:
                console.print("[yellow]Nothing open. Run `prep ask --force` first.[/yellow]")
                raise typer.Exit(1)
            question_id = int(rows[0]["id"])
            console.print(f"[dim]answering:[/dim] {rows[0]['text']}")
        db.insert_answer(conn, question_id, text)
    console.print("[green]recorded[/green] — run `prep grade` to have it marked")


@app.command("self-score")
def self_score(
    score: int = typer.Argument(..., min=0, max=100),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Score your own last answer, when you would rather judge it yourself."""
    from . import schedule

    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT a.id, q.topic AS topic FROM answers a JOIN questions q ON q.id = a.question_id "
            "ORDER BY a.id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            console.print("[yellow]No answer to score.[/yellow]")
            raise typer.Exit(1)
        db.record_grade(
            conn, int(row["id"]), Grade(score=score, feedback="Self-scored.", grader=Grader.SELF)
        )
        db.mark_feedback_sent(conn, int(row["id"]))
        db.save_topic_state(conn, schedule.next_due(score, db.get_topic_state(conn, row["topic"])))
    console.print(f"[green]{score}/100[/green] recorded on {row['topic']}")


@app.command()
def goal(
    text: str = typer.Argument(..., help="What you are aiming at now."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Tell the agent your goal changed. Applied on the next adapt."""
    with db.connect(db_path) as conn:
        db.record_event(conn, "goal", json.dumps({"command": "goal", "argument": text}))
    console.print("[green]queued[/green] — run `prep adapt` to fold it into the context")


@app.command()
def context(
    full: bool = typer.Option(False, "--full", help="Print the whole system prompt."),
    history: bool = typer.Option(False, "--history", help="Show how it has changed."),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Show the context the agent is running on."""
    profile_cfg, topics_cfg = _configs()
    with db.connect(db_path) as conn:
        version, system_prompt = adapt_mod.current_context(conn, profile_cfg, topics_cfg)
        rows = db.context_history(conn) if history else []

    if full:
        console.print(system_prompt)
        return
    console.print(f"[bold]context edition {version}[/bold]\n")
    console.print(render_context_summary(profile_cfg.profile))
    if history:
        table = Table("edition", "when", "why")
        for row in rows:
            table.add_row(str(row["version"]), (row["created_at"] or "")[:16], row["reason"] or "—")
        console.print("")
        console.print(table)


@app.command()
def status(db_path: Path | None = typer.Option(None, "--db")) -> None:
    """Scores by topic, and what the scheduler will reach for next."""
    _, topics_cfg = _configs()
    with db.connect(db_path) as conn:
        totals = db.counts(conn)
        states = {t.id: db.get_topic_state(conn, t.id) for t in topics_cfg.topics}

    table = Table("topic", "asked", "answered", "avg", "last", "due")
    for topic in topics_cfg.topics:
        s = states[topic.id]
        avg = f"{s.avg_score:.0f}" if s.avg_score is not None else "—"
        colour = (
            "green"
            if (s.avg_score or 0) >= 85
            else "red"
            if s.avg_score is not None and s.avg_score < 60
            else "white"
        )  # noqa: E501
        table.add_row(
            topic.id,
            str(s.asked_count),
            str(s.answered_count),
            f"[{colour}]{avg}[/{colour}]",
            s.last_asked or "—",
            s.due_on or "now",
        )
    console.print(table)
    console.print(
        f"questions={totals['questions']} answers={totals['answers']} "
        f"graded={totals['answers.graded']} editions={totals['context_versions']}"
    )


@app.command()
def topics() -> None:
    """List the topic ids you can /focus or /drop."""
    _, topics_cfg = _configs()
    table = Table("id", "name", "weight", "stages", "banked")
    for t in topics_cfg.topics:
        table.add_row(t.id, t.name, f"{t.weight:g}", ", ".join(t.stages), str(len(t.seeds)))
    console.print(table)


if __name__ == "__main__":
    app()
