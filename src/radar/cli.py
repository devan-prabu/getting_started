"""The typer CLI — the only entry point.

Every command is safely re-runnable: `radar run` twice in a row produces zero
duplicate rows and zero duplicate alerts (brief §7).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import alert as alert_mod
from . import collect as collect_mod
from . import config as config_mod
from . import db, dedupe
from . import enrich as enrich_mod
from . import extract as extract_mod
from . import filters as filters_mod
from .fetcher import Fetcher, RobotsDisallowed
from .models import SourceStatus

load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="Watch UAE construction contract awards and alert on the hiring window.",
)
sources_app = typer.Typer(help="Inspect and verify config/sources.yml.")
app.add_typer(sources_app, name="sources")

console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    _setup_logging(verbose)


@app.command()
def init(
    db_path: Path | None = typer.Option(None, "--db", help="Override DB_PATH."),
    companies_file: Path | None = typer.Option(None, "--companies"),
) -> None:
    """Create the database and load companies.yml into the companies table."""
    path = db.init_db(db_path)
    companies = config_mod.load_companies(companies_file)
    with db.connect(path) as conn:
        loaded = db.load_companies(conn, companies)
        totals = db.counts(conn)
    console.print(f"[green]DB ready[/green] at {path}")
    console.print(
        f"loaded {loaded} companies · raw_items={totals['raw_items']} awards={totals['awards']}"
    )


@app.command()
def collect(
    source: str | None = typer.Option(None, "--source", help="Collect one source id."),
    db_path: Path | None = typer.Option(None, "--db"),
    max_resolve: int = typer.Option(
        collect_mod.DEFAULT_MAX_RESOLVE,
        "--max-resolve",
        help="Cap on network redirect resolutions per run.",
    ),
) -> None:
    """Fetch feeds into raw_items, deduped by URL and by title similarity."""
    cfg = config_mod.SourcesConfig.load()
    with db.connect(db_path) as conn, Fetcher() as fetcher:
        stats = collect_mod.collect_all(
            conn, cfg, fetcher, source_id=source, max_resolve=max_resolve
        )
    console.print(f"[bold]collect[/bold] {stats.as_line()}")
    for sid, err in stats.errors.items():
        console.print(f"  [red]{sid}[/red]: {err}")


@app.command()
def enrich(
    limit: int | None = typer.Option(None, "--limit"),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Pull clean article text with trafilatura for items with status=new."""
    with db.connect(db_path) as conn, Fetcher() as fetcher:
        stats = enrich_mod.enrich_new(conn, fetcher, limit=limit)
    console.print(f"[bold]enrich[/bold] {stats.as_line()}")
    if stats.attempted and stats.coverage < 0.8:
        console.print(f"[yellow]coverage {stats.coverage:.0%} is below the 80% target[/yellow]")


@app.command("filter")
def filter_items(db_path: Path | None = typer.Option(None, "--db")) -> None:
    """Apply the keyword gate to enriched items."""
    with db.connect(db_path) as conn:
        stats = filters_mod.filter_enriched(conn)
    console.print(f"[bold]filter[/bold] {stats.as_line()}")


@app.command()
def extract(
    limit: int | None = typer.Option(None, "--limit"),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Phase 0 regex extraction of filtered items into the awards table."""
    with db.connect(db_path) as conn:
        stats = extract_mod.extract_pending(conn, limit=limit)
    console.print(f"[bold]extract[/bold] {stats.as_line()}")


@app.command()
def alert(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print to stdout, record nothing."),
    limit: int | None = typer.Option(None, "--limit"),
    db_path: Path | None = typer.Option(None, "--db"),
) -> None:
    """Send unsent awards on every configured channel."""
    channels = alert_mod.channels_from_env()
    with db.connect(db_path) as conn:
        stats = alert_mod.send_alerts(conn, channels, dry_run=dry_run, limit=limit)
    console.print(f"[bold]alert[/bold] {stats.as_line()}")
    for name in stats.unconfigured:
        console.print(f"  [yellow]{name} not configured — see .env.example[/yellow]")


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run"),
    db_path: Path | None = typer.Option(None, "--db"),
    max_resolve: int = typer.Option(collect_mod.DEFAULT_MAX_RESOLVE, "--max-resolve"),
) -> None:
    """collect -> enrich -> filter -> extract -> alert. The cron path."""
    cfg = config_mod.SourcesConfig.load()
    channels = alert_mod.channels_from_env()

    with db.connect(db_path) as conn:
        with Fetcher() as fetcher:
            c = collect_mod.collect_all(conn, cfg, fetcher, max_resolve=max_resolve)
            console.print(f"[bold]collect[/bold] {c.as_line()}")
            for sid, err in c.errors.items():
                console.print(f"  [red]{sid}[/red]: {err}")

            e = enrich_mod.enrich_new(conn, fetcher)
            console.print(f"[bold]enrich[/bold]  {e.as_line()}")

        f = filters_mod.filter_enriched(conn)
        console.print(f"[bold]filter[/bold]  {f.as_line()}")

        x = extract_mod.extract_pending(conn)
        console.print(f"[bold]extract[/bold] {x.as_line()}")

        a = alert_mod.send_alerts(conn, channels, dry_run=dry_run)
        console.print(f"[bold]alert[/bold]   {a.as_line()}")
        for name in a.unconfigured:
            console.print(f"  [yellow]{name} not configured — see .env.example[/yellow]")


@sources_app.command("check")
def sources_check(
    write: bool = typer.Option(
        True, "--write/--no-write", help="Update status and last_checked in sources.yml."
    ),
) -> None:
    """Fetch every source and record whether it is live or dead.

    This is what turns the shipped `status: unverified` entries into honest
    values. It needs real internet access (brief §0.2).
    """
    cfg = config_mod.SourcesConfig.load()
    updates: dict[str, tuple[str, str, str | None]] = {}
    today = date.today().isoformat()

    table = Table("source", "status", "detail")
    with Fetcher() as fetcher:
        for source in cfg.sources:
            url = cfg.build_url(source)
            status, detail = _probe(fetcher, url)
            updates[source.id] = (status, today, _note_for(source.notes, status, detail))
            colour = {"live": "green", "dead": "red"}.get(status, "yellow")
            table.add_row(source.id, f"[{colour}]{status}[/{colour}]", detail)

    console.print(table)
    if write:
        cfg.write_status(updates)
        console.print(f"[green]updated[/green] {cfg.path}")


def _probe(fetcher: Fetcher, url: str) -> tuple[str, str]:
    try:
        result = fetcher.get(url)
    except RobotsDisallowed:
        return str(SourceStatus.DEAD), "robots.txt disallows this path"
    except Exception as exc:  # noqa: BLE001
        return str(SourceStatus.DEAD), f"{type(exc).__name__}: {exc}"[:120]

    if result.status_code != 200:
        return str(SourceStatus.DEAD), f"HTTP {result.status_code}"
    try:
        entries = collect_mod.parse_feed(result.text)
    except ValueError as exc:
        return str(SourceStatus.DEAD), str(exc)[:120]
    if not entries:
        # Parses but empty: not broken, just nothing matched. Worth flagging.
        return str(SourceStatus.LIVE), "0 entries — query may be too narrow"
    return str(SourceStatus.LIVE), f"{len(entries)} entries"


def _note_for(existing: str | None, status: str, detail: str) -> str:
    base = (existing or "").split(" [checked")[0].strip()
    return f"{base} [checked {date.today().isoformat()}: {status} — {detail}]".strip()


@sources_app.command("list")
def sources_list() -> None:
    """Show what is in sources.yml without touching the network."""
    cfg = config_mod.SourcesConfig.load()
    table = Table("id", "type", "enabled", "status", "last checked")
    for s in cfg.sources:
        table.add_row(
            s.id, s.type, "yes" if s.enabled else "no", str(s.status), s.last_checked or "—"
        )
    console.print(table)


@app.command()
def status(db_path: Path | None = typer.Option(None, "--db")) -> None:
    """Row counts by table and by item status."""
    with db.connect(db_path) as conn:
        totals = db.counts(conn)
    table = Table("metric", "count")
    for key, value in sorted(totals.items()):
        table.add_row(key, str(value))
    console.print(table)


@app.command("resolve-url")
def resolve_url_cmd(url: str) -> None:
    """Show what a Google News link resolves to. Useful when dedupe misbehaves."""
    offline = dedupe.decode_google_news_url(url)
    console.print(f"offline decode : {offline or '[yellow]not decodable[/yellow]'}")
    with Fetcher() as fetcher:
        resolved = dedupe.resolve_url(url, fetcher)
    console.print(f"resolved       : {resolved}")
    console.print(f"canonical      : {dedupe.canonicalise(resolved)}")
    console.print(f"sha256         : {dedupe.url_hash(resolved)}")


if __name__ == "__main__":
    app()
