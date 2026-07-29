"""CLI smoke tests. Only commands that need no network are exercised here;
the pipeline stages themselves are covered in the module tests."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from radar import db
from radar.cli import app
from radar.models import Award, RawItem

runner = CliRunner()


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "radar.db"
    monkeypatch.setenv("DB_PATH", str(path))
    return path


def test_init_creates_the_db_and_loads_companies(db_file):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert db_file.exists()
    assert "loaded" in result.output

    with db.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] >= 16


def test_init_is_safe_to_rerun(db_file):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    with db.connect(db_file) as conn:
        before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    runner.invoke(app, ["init"])
    with db.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == before


def test_status_reports_counts(db_file):
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "raw_items" in result.output


def test_sources_list_needs_no_network(db_file):
    result = runner.invoke(app, ["sources", "list"])
    assert result.exit_code == 0
    assert "gnews_award_generic" in result.output
    # Nothing may claim to be live before `radar sources check` has run.
    assert "unverified" in result.output


def test_alert_dry_run_prints_and_records_nothing(db_file):
    runner.invoke(app, ["init"])
    with db.connect(db_file) as conn:
        item_id = db.insert_raw_item(
            conn,
            RawItem(
                source_id="s",
                url="https://x.test/a",
                url_hash="ha",
                title="ASGC awarded a Dubai contract",
            ),
        )
        db.insert_award(
            conn, Award(raw_item_id=item_id, contractor_raw="ASGC", emirate="Dubai", confidence=0.4)
        )

    result = runner.invoke(app, ["alert", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "ASGC" in result.output

    with db.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0


def test_alert_reports_unconfigured_channels(db_file, monkeypatch):
    monkeypatch.setenv("ALERT_CHANNELS", "telegram")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    runner.invoke(app, ["init"])

    result = runner.invoke(app, ["alert"])
    assert result.exit_code == 0
    assert "not configured" in result.output


def test_resolve_url_decodes_offline(db_file):
    gnews = (
        "https://news.google.com/rss/articles/CBMiWWh0dHBzOi8vd3d3LnRoZW5hdGlvbmFsbmV3cy5jb20"
        "vYnVzaW5lc3MvcHJvcGVydHkvMjAyNi8wNy8yNy9hc2djLWF3YXJkZWQtdG93ZXItY29udHJhY3QvMgtDQU"
        "lhQjFKVlFrbEQ?oc=5"
    )
    result = runner.invoke(app, ["resolve-url", gnews])
    assert result.exit_code == 0
    assert "thenationalnews.com" in result.output


def test_help_lists_the_phase_0_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "collect", "enrich", "extract", "alert", "run", "sources"):
        assert command in result.output
