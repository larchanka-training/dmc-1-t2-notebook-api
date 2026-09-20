"""Smoke and argument tests for ``scripts/reconcile_llm_usage.py`` CLI."""

from datetime import UTC, datetime
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.modules.llm.schemas.llm_schemas import LlmReconciliationSummary
from scripts import reconcile_llm_usage as cli


def test_build_parser_accepts_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["--stale-seconds", "600", "--limit", "50", "--dry-run"])
    assert args.stale_seconds == 600
    assert args.limit == 50
    assert args.dry_run is True


def test_build_parser_accepts_run_subcommand() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run is True


def test_cli_main_runs_and_outputs_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_factory = MagicMock(spec=sessionmaker)
    monkeypatch.setattr(cli, "get_session_factory", lambda: mock_factory)

    mock_summary = LlmReconciliationSummary(
        reconciled_reserved=2,
        reconciled_started=1,
        returned_cost_micros=30000,
        cutoff=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )

    mock_service = MagicMock()
    mock_service.reconcile_stale_reservations.return_value = mock_summary
    monkeypatch.setattr(cli, "LlmReconciliationService", lambda **kwargs: mock_service)

    exit_code = cli.main(["--dry-run", "--stale-seconds", "300"])
    assert exit_code == 0

    lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["reconciledReserved"] == 2
    assert payload["reconciledStarted"] == 1
    assert payload["returnedCostMicros"] == 30000
    assert payload["dry_run"] is True


def test_cli_main_rejects_non_positive_stale_seconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F3 Regression: CLI rejects negative and zero --stale-seconds with exit code 2."""
    for invalid_val in ["-1", "0", "-300"]:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--stale-seconds", invalid_val])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--stale-seconds must be a positive integer (> 0)" in err


def test_cli_main_rejects_non_positive_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F3 Regression: CLI rejects negative and zero --limit with exit code 2."""
    for invalid_val in ["-1", "0", "-100"]:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--limit", invalid_val])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--limit must be a positive integer (> 0)" in err
