"""Run auth cleanup as a one-off operational command."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

# scripts/ ships outside the package; make ``app.*`` importable when invoked as
# ``python scripts/auth_cleanup.py``. Safe to drop once we move to a proper
# console_scripts entry point in pyproject.toml.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.core.db import get_session_factory  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.modules.auth.repositories import (  # noqa: E402
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
)
from app.modules.auth.services import AuthCleanupService  # noqa: E402

logger = get_logger(__name__)


def _build_service(db: Session) -> AuthCleanupService:
    return AuthCleanupService(
        otp_repository=OtpRepository(db),
        session_repository=AuthSessionRepository(db),
        refresh_token_repository=RefreshTokenRepository(db),
        otp_grace_seconds=settings.auth_cleanup_otp_grace_seconds,
        retention_seconds=settings.auth_cleanup_retention_seconds,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Run auth cleanup and print a JSON summary."""
    session_factory = get_session_factory()
    session = session_factory()
    try:
        service = _build_service(session)
        now = datetime.now(UTC)
        if args.dry_run:
            logger.info("auth.cleanup.cli.dry_run_started")
            result = service.preview(now=now)
            # No commit needed for SELECT-only preview, but rollback explicitly
            # to drop any implicit read-snapshot the session holds.
            session.rollback()
        else:
            logger.info("auth.cleanup.cli.started")
            result = service.cleanup(now=now)
            session.commit()
    except Exception:
        logger.exception("auth.cleanup.cli.failed")
        session.rollback()
        raise
    finally:
        session.close()

    payload = asdict(result)
    payload["dry_run"] = bool(args.dry_run)
    print(json.dumps(payload, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Clean expired OTPs and stale auth session/token history."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run auth cleanup")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report counts without deleting anything",
    )
    run_parser.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
