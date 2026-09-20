"""Run stale LLM reservation reconciliation as a one-off operational command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# scripts/ ships outside the package; make ``app.*`` importable when invoked as
# ``python scripts/reconcile_llm_usage.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.core.db import get_session_factory  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.modules.llm.services.reconciliation_service import (  # noqa: E402
    LlmReconciliationService,
)

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Reconcile stale or orphaned LLM usage reservations."
    )
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=None,
        help="age threshold in seconds before a reservation is considered stale",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum number of stale reservations to process in this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="report counts without mutating reservations or quota counters",
    )

    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="run reconciliation")
    run_parser.add_argument(
        "--stale-seconds",
        type=int,
        default=argparse.SUPPRESS,
        help="age threshold in seconds before a reservation is considered stale",
    )
    run_parser.add_argument(
        "--limit",
        type=int,
        default=argparse.SUPPRESS,
        help="maximum number of stale reservations to process in this run",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="report counts without mutating reservations or quota counters",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.stale_seconds is not None and args.stale_seconds <= 0:
        parser.error("--stale-seconds must be a positive integer (> 0)")

    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer (> 0)")

    session_factory = get_session_factory()
    service = LlmReconciliationService(
        session_factory=session_factory,
        settings=settings,
    )

    if args.dry_run:
        logger.info("llm.reconcile.cli.dry_run_started")
    else:
        logger.info("llm.reconcile.cli.started")

    result = service.reconcile_stale_reservations(
        stale_seconds=args.stale_seconds,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    payload = result.model_dump(by_alias=True, mode="json")
    payload["dry_run"] = bool(args.dry_run)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
