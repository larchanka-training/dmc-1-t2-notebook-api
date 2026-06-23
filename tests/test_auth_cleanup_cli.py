"""Smoke tests for ``scripts/auth_cleanup.py`` CLI."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.modules.auth.repositories import OtpRepository
from scripts import auth_cleanup as cli


def _swap_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> MagicMock:
    """Return a session factory that yields ``db_session`` with tracking.

    The real CLI closes the session in ``finally``; for tests we keep the
    fixture session alive and only record that commit/rollback/close were
    invoked, so we can assert the right path was taken.
    """
    mock_session = MagicMock(wraps=db_session)
    # close() must not actually close the fixture session — the fixture
    # finalizer will. Leave commit/rollback wrapped so SQL still flows.
    mock_session.close = MagicMock()
    factory = MagicMock(return_value=mock_session)
    monkeypatch.setattr(cli, "get_session_factory", lambda: factory)
    return mock_session


def test_build_parser_accepts_run_subcommand() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    assert args.command == "run"
    assert args.dry_run is False
    assert args.func is cli.cmd_run


def test_build_parser_accepts_dry_run_flag() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--dry-run"])
    assert args.dry_run is True


def test_build_parser_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_cli_run_commits_and_emits_summary(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    OtpRepository(db_session).create(
        email="user@example.com",
        otp_hash="old",
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
    )
    db_session.commit()
    mock_session = _swap_session_factory(monkeypatch, db_session)

    exit_code = cli.main(["run"])

    # configure_logging() may also write JSON log lines to stdout; the CLI
    # summary is always the last line.
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert exit_code == 0
    assert payload["otps_deleted"] == 1
    assert payload["dry_run"] is False
    mock_session.commit.assert_called_once()
    mock_session.rollback.assert_not_called()
    mock_session.close.assert_called_once()


def test_cli_dry_run_does_not_delete(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    otp = OtpRepository(db_session).create(
        email="user@example.com",
        otp_hash="old",
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
    )
    db_session.commit()
    mock_session = _swap_session_factory(monkeypatch, db_session)

    exit_code = cli.main(["run", "--dry-run"])

    # configure_logging() may also write JSON log lines to stdout; the CLI
    # summary is always the last line.
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert exit_code == 0
    assert payload["otps_deleted"] == 1
    assert payload["dry_run"] is True
    # Preview must not commit and must not delete.
    mock_session.commit.assert_not_called()
    mock_session.rollback.assert_called_once()
    # Row is still there.
    refreshed = OtpRepository(db_session).count_expired_before(now)
    assert refreshed == 1
    assert otp.id is not None


def test_cli_rolls_back_on_service_error(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    """A failing service must trigger rollback and propagate the exception."""
    mock_session = _swap_session_factory(monkeypatch, db_session)

    boom_service = MagicMock()
    boom_service.cleanup.side_effect = RuntimeError("boom")
    monkeypatch.setattr(cli, "_build_service", lambda _db: boom_service)

    with pytest.raises(RuntimeError, match="boom"):
        cli.main(["run"])

    mock_session.commit.assert_not_called()
    mock_session.rollback.assert_called_once()
    mock_session.close.assert_called_once()
