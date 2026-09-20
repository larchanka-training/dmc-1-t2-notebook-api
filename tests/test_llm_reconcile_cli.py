"""Smoke and argument tests for ``scripts/reconcile_llm_usage.py`` CLI."""

from datetime import UTC, date, datetime, timedelta
import json
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.modules.auth.models.user import User as UserModel
from app.modules.llm.repositories.usage_repository import LlmUsageRepository
from app.modules.llm.schemas.llm_schemas import LlmReconciliationSummary
from scripts import reconcile_llm_usage as cli


def test_build_parser_option_ordering_and_defaults() -> None:
    """F5: Verify options placed before or after 'run' subcommand retain their values."""
    parser = cli.build_parser()

    # --dry-run variations
    assert parser.parse_args(["--dry-run", "run"]).dry_run is True
    assert parser.parse_args(["run", "--dry-run"]).dry_run is True
    assert parser.parse_args(["--dry-run"]).dry_run is True
    assert parser.parse_args(["run"]).dry_run is False
    assert parser.parse_args([]).dry_run is False

    # --stale-seconds variations
    assert parser.parse_args(["--stale-seconds", "500", "run"]).stale_seconds == 500
    assert parser.parse_args(["run", "--stale-seconds", "500"]).stale_seconds == 500
    assert parser.parse_args(["--stale-seconds", "500"]).stale_seconds == 500

    # --limit variations
    assert parser.parse_args(["--limit", "25", "run"]).limit == 25
    assert parser.parse_args(["run", "--limit", "25"]).limit == 25
    assert parser.parse_args(["--limit", "25"]).limit == 25

    # Subparser argument takes precedence over root if duplicated
    assert (
        parser.parse_args(
            ["--stale-seconds", "100", "run", "--stale-seconds", "200"]
        ).stale_seconds
        == 200
    )


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


def test_cli_main_dry_run_forms_invoke_service_with_dry_run_true(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F5: Verify all dry-run invocation forms pass dry_run=True to reconciliation service."""
    mock_factory = MagicMock(spec=sessionmaker)
    monkeypatch.setattr(cli, "get_session_factory", lambda: mock_factory)

    mock_summary = LlmReconciliationSummary(
        reconciled_reserved=0,
        reconciled_started=0,
        returned_cost_micros=0,
        cutoff=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )

    for argv in [
        ["--dry-run", "run"],
        ["run", "--dry-run"],
        ["--dry-run"],
    ]:
        mock_service = MagicMock()
        mock_service.reconcile_stale_reservations.return_value = mock_summary
        monkeypatch.setattr(cli, "LlmReconciliationService", lambda **kwargs: mock_service)

        exit_code = cli.main(argv)
        assert exit_code == 0
        mock_service.reconcile_stale_reservations.assert_called_once()
        _, kwargs = mock_service.reconcile_stale_reservations.call_args
        assert kwargs["dry_run"] is True
        capsys.readouterr()  # Clear stdout


def test_cli_main_rejects_non_positive_stale_seconds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F3/F5 Regression: CLI rejects non-positive --stale-seconds before opening DB session."""
    def _exploding_factory() -> None:
        pytest.fail("get_session_factory must not be called when validation fails")

    monkeypatch.setattr(cli, "get_session_factory", _exploding_factory)

    for invalid_val in ["-1", "0", "-300"]:
        for argv in [
            ["--stale-seconds", invalid_val],
            ["--stale-seconds", invalid_val, "run"],
            ["run", "--stale-seconds", invalid_val],
        ]:
            with pytest.raises(SystemExit) as exc:
                cli.main(argv)
            assert exc.value.code == 2
            err = capsys.readouterr().err
            assert "--stale-seconds must be a positive integer (> 0)" in err


def test_cli_main_rejects_non_positive_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F3/F5 Regression: CLI rejects non-positive --limit before opening DB session."""
    def _exploding_factory() -> None:
        pytest.fail("get_session_factory must not be called when validation fails")

    monkeypatch.setattr(cli, "get_session_factory", _exploding_factory)

    for invalid_val in ["-1", "0", "-100"]:
        for argv in [
            ["--limit", invalid_val],
            ["--limit", invalid_val, "run"],
            ["run", "--limit", invalid_val],
        ]:
            with pytest.raises(SystemExit) as exc:
                cli.main(argv)
            assert exc.value.code == 2
            err = capsys.readouterr().err
            assert "--limit must be a positive integer (> 0)" in err


def test_cli_main_on_postgresql_dry_run_preserves_state_and_counters(
    pg_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F5: Verifies on PostgreSQL that '--dry-run run' and 'run --dry-run' preserve DB state.

    Neither dry-run form mutates reservations or quota counters. Subsequent non-dry-run
    successfully executes reconciliation.
    """
    monkeypatch.setattr(cli, "get_session_factory", lambda: pg_session_factory)
    test_settings = Settings(
        _env_file=None,
        jwt_secret_key="x" * 32,
        llm_admin_emails="admin@example.com",
        llm_reconciliation_stale_seconds=300,
    )
    monkeypatch.setattr(cli, "settings", test_settings)

    now = datetime.now(UTC)
    stale_time = now - timedelta(minutes=10)
    day_start = date(stale_time.year, stale_time.month, stale_time.day)
    month_start = date(stale_time.year, stale_time.month, 1)

    cost_reserved = 15000
    with pg_session_factory() as session:
        user = UserModel(
            id=uuid4(),
            email=f"user_{uuid4().hex[:8]}@example.com",
            display_name="CLI User",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()
        user_id = user.id

        repo = LlmUsageRepository(session)
        res = repo.create_reservation(
            user_id=user_id,
            request_id=uuid4(),
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_time,
        )
        res_id = res.id

        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", str(user_id), "day", day_start),
            ("user", str(user_id), "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    # Form 1: 'run --dry-run'
    exit_code1 = cli.main(["run", "--dry-run"])
    assert exit_code1 == 0
    payload1 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload1["reconciledReserved"] == 1
    assert payload1["dry_run"] is True

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "reserved"
        ctr = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert ctr.calls_reserved == 1
        assert ctr.cost_reserved_micros == cost_reserved

    # Form 2: '--dry-run run' (the bug reported by reviewer)
    exit_code2 = cli.main(["--dry-run", "run"])
    assert exit_code2 == 0
    payload2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload2["reconciledReserved"] == 1
    assert payload2["dry_run"] is True

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "reserved"
        ctr = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert ctr.calls_reserved == 1
        assert ctr.cost_reserved_micros == cost_reserved

    # Form 3: bare '--dry-run'
    exit_code3 = cli.main(["--dry-run"])
    assert exit_code3 == 0
    payload3 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload3["reconciledReserved"] == 1
    assert payload3["dry_run"] is True

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "reserved"

    # Form 4: non-dry-run 'run' executes reconciliation and mutates state/counters
    exit_code4 = cli.main(["run"])
    assert exit_code4 == 0
    payload4 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload4["reconciledReserved"] == 1
    assert payload4["dry_run"] is False

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "released"
        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr.calls_reserved == 0
            assert ctr.cost_reserved_micros == 0
