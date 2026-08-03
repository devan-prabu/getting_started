from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from prep import db
from prep.cli import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """An isolated DB and a writable copy of the real config."""
    config_src = Path(__file__).parents[1] / "config"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("profile.yml", "topics.yml"):
        (config_dir / name).write_text(
            (config_src / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    monkeypatch.setenv("PREP_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PREP_DB_PATH", str(tmp_path / "prep.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    # config.CONFIG_DIR is read at import time; point it at the copy.
    import prep.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    return tmp_path


def invoke(*args: str):
    return runner.invoke(app, list(args))


class TestInit:
    def test_it_creates_the_db_and_first_edition(self, workspace):
        result = invoke("init")
        assert result.exit_code == 0, result.output
        assert "DB ready" in result.output
        assert (workspace / "prep.db").exists()

    def test_it_is_re_runnable(self, workspace):
        invoke("init")
        assert invoke("init").exit_code == 0


class TestAsk:
    def test_dry_run_prints_a_drill_and_sends_nothing(self, workspace):
        invoke("init")
        result = invoke("ask", "--dry-run", "--bank", "-n", "2")
        assert result.exit_code == 0
        assert "Drill 1/2" in result.output
        assert "Reply to this message" in result.output

    def test_it_falls_back_to_printing_when_telegram_is_unset(self, workspace):
        invoke("init")
        result = invoke("ask", "--force", "--bank", "-n", "1")
        assert result.exit_code == 0
        assert "not configured" in result.output


class TestAnswerAndGrade:
    def test_answer_then_grade_then_status(self, workspace):
        invoke("init")
        invoke("ask", "--force", "--bank", "-n", "1")

        answered = invoke(
            "answer",
            "A hold point means work cannot proceed until the consultant has "
            "witnessed and signed the inspection.",
        )
        assert answered.exit_code == 0
        assert "recorded" in answered.output

        graded = invoke("grade", "--no-send")
        assert graded.exit_code == 0
        assert "graded=1" in graded.output

        status = invoke("status")
        assert status.exit_code == 0
        assert "answers=1" in status.output

    def test_answering_with_nothing_open_exits_nonzero(self, workspace):
        invoke("init")
        result = invoke("answer", "an answer to nothing")
        assert result.exit_code == 1

    def test_self_score_records_a_grade(self, workspace):
        invoke("init")
        invoke("ask", "--force", "--bank", "-n", "1")
        invoke("answer", "something")
        result = invoke("self-score", "75")
        assert result.exit_code == 0
        assert "75/100" in result.output


class TestContextAndAdapt:
    def test_context_shows_the_edition(self, workspace):
        invoke("init")
        result = invoke("context")
        assert result.exit_code == 0
        assert "context edition" in result.output

    def test_context_full_prints_the_system_prompt(self, workspace):
        invoke("init")
        result = invoke("context", "--full")
        assert "You are the interview coach" in result.output
        assert "Proscape" in result.output

    def test_goal_is_queued_then_folded_in_by_adapt(self, workspace):
        invoke("init")
        assert invoke("goal", "chase the KEO landscape inspector role").exit_code == 0

        with db.connect(workspace / "prep.db") as conn:
            assert len(db.unapplied_events(conn)) == 1

        result = invoke("adapt")
        assert result.exit_code == 0
        with db.connect(workspace / "prep.db") as conn:
            assert db.unapplied_events(conn) == []

        assert "KEO landscape inspector" in invoke("context", "--full").output

    def test_adapt_dry_run_writes_nothing(self, workspace):
        invoke("init")
        profile = Path(workspace / "config" / "profile.yml")
        before = profile.read_text(encoding="utf-8")
        result = invoke("adapt", "--dry-run")
        assert result.exit_code == 0
        assert profile.read_text(encoding="utf-8") == before

    def test_context_history_lists_editions(self, workspace):
        invoke("init")
        invoke("goal", "target Stantec")
        invoke("adapt")
        result = invoke("context", "--history")
        assert result.exit_code == 0
        assert "edition" in result.output


class TestRun:
    def test_the_cron_path_runs_end_to_end_offline(self, workspace):
        invoke("init")
        result = invoke("run", "--dry-run", "--bank")
        assert result.exit_code == 0, result.output
        for stage in ("grade", "adapt", "ask"):
            assert stage in result.output

    def test_run_twice_sends_one_drill(self, workspace):
        invoke("init")
        invoke("run", "--bank")
        second = invoke("run", "--bank")
        assert "already sent today" in second.output


class TestInspection:
    def test_topics_lists_the_ids(self, workspace):
        result = invoke("topics")
        assert result.exit_code == 0
        assert "consultancy_side" in result.output

    def test_poll_without_telegram_exits_nonzero(self, workspace):
        invoke("init")
        result = invoke("poll")
        assert result.exit_code == 1
