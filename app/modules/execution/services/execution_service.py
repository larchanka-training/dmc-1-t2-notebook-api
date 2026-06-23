"""Execution orchestration: validate language, run code, map to the contract.

The service is storage-free and stateless. It turns an
:class:`~app.modules.execution.schemas.ExecuteRequest` into an
:class:`~app.modules.execution.schemas.ExecuteResponse` whose ``outputs``
mirror the UI ``cell.outputs`` format, and whose top-level ``status`` is one
of ``ok | error | timeout | unsupported_language`` (acceptance criteria).
"""

from __future__ import annotations

from time import perf_counter

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.execution.schemas.execution_schemas import (
    ErrorItem,
    ExecuteRequest,
    ExecuteResponse,
    ExecutionStats,
    OutputItem,
    StderrItem,
    StdoutItem,
)
from app.modules.execution.services.errors import CodeTooLargeError
from app.modules.execution.services.runner import CodeRunner, RunnerOutput

logger = get_logger(__name__)

# Aliases accepted as "JavaScript". TypeScript is intentionally excluded: the
# subprocess runner does not strip types, so it would be misleading to accept
# it. Anything else yields status ``unsupported_language``.
_JAVASCRIPT_ALIASES = {"javascript", "js"}


class ExecutionService:
    """Run user code through a :class:`CodeRunner` and shape the result."""

    def __init__(
        self,
        runner: CodeRunner,
        *,
        default_timeout_ms: int,
        max_timeout_ms: int,
        max_code_bytes: int,
    ) -> None:
        self._runner = runner
        self._default_timeout_ms = default_timeout_ms
        self._max_timeout_ms = max_timeout_ms
        self._max_code_bytes = max_code_bytes

    def execute(self, payload: ExecuteRequest, user: CurrentUser) -> ExecuteResponse:
        """Execute ``payload.code`` and return a unified result."""
        code_bytes = payload.code_byte_length()
        if code_bytes > self._max_code_bytes:
            # Enforced here (not in the schema) so the limit honours the
            # operator knob settings.execute_max_code_bytes — a single source of
            # truth (docs/execution-architecture.md §7.4). Maps to HTTP 422.
            raise CodeTooLargeError(
                f"code exceeds the {self._max_code_bytes // 1024} KiB "
                "source-size limit"
            )

        language = payload.language.strip().lower()
        if language not in _JAVASCRIPT_ALIASES:
            logger.info(
                "execution.unsupported_language",
                user_id=str(user.id),
                language=payload.language,
            )
            return ExecuteResponse(status="unsupported_language", outputs=[])

        timeout_ms = self._resolve_timeout(payload.timeout_ms)
        start = perf_counter()
        result = self._runner.run(code=payload.code, timeout_ms=timeout_ms)
        duration_ms = int((perf_counter() - start) * 1000)

        status, outputs = self._map_outcome(result)
        logger.info("cell_executed", user_id=str(user.id), status=status)
        if status == "error":
            logger.info("execution_error", user_id=str(user.id))
        logger.info(
            "execution.completed",
            user_id=str(user.id),
            language=language,
            status=status,
            timeout_ms=timeout_ms,
            duration_ms=duration_ms,
            code_bytes=payload.code_byte_length(),
        )
        return ExecuteResponse(
            status=status,
            outputs=outputs,
            stats=ExecutionStats(duration_ms=duration_ms),
        )

    def _resolve_timeout(self, requested_ms: int | None) -> int:
        """Apply the default when unset and clamp to the configured maximum."""
        # Explicit ``is None`` (not ``or``): only a *missing* value falls back to
        # the default. The schema rejects non-positive values today, but this
        # must not silently turn a future ``0`` into the default 5 s.
        timeout_ms = (
            self._default_timeout_ms if requested_ms is None else requested_ms
        )
        return min(timeout_ms, self._max_timeout_ms)

    def _map_outcome(
        self, result: RunnerOutput
    ) -> tuple[str, list[OutputItem]]:
        """Translate a :class:`RunnerOutput` into ``(status, outputs)``."""
        outputs: list[OutputItem] = []
        if result.stdout:
            outputs.append(StdoutItem(text=result.stdout))
        if result.stderr:
            outputs.append(StderrItem(text=result.stderr))

        if result.timed_out:
            return "timeout", outputs

        if result.exit_code == 0:
            return "ok", outputs

        # Non-zero exit: surface a structured error item alongside the raw
        # stderr so the FE can render it like a thrown error in cell.outputs.
        outputs.append(
            ErrorItem(
                name="ExecutionError",
                message=(result.stderr or "Code execution failed").strip()
                or "Code execution failed",
            )
        )
        return "error", outputs


def build_execution_service() -> ExecutionService:
    """Construct the production execution service from settings."""
    from app.modules.execution.services.runner import SubprocessNodeRunner

    runner = SubprocessNodeRunner(
        node_command=settings.execute_node_command,
        max_output_bytes=settings.execute_max_output_bytes,
        max_memory_mb=settings.execute_max_memory_mb,
    )
    return ExecutionService(
        runner,
        default_timeout_ms=settings.execute_default_timeout_ms,
        max_timeout_ms=settings.execute_max_timeout_ms,
        max_code_bytes=settings.execute_max_code_bytes,
    )
