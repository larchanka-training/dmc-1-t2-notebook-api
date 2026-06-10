"""Domain exceptions for the execution module.

Execution **errors of user code** are not transport errors: they are
reported inside :class:`~app.modules.execution.schemas.ExecuteResponse`
with a ``status``/``error`` item (docs/execution-architecture.md §9). The
exceptions here cover *infrastructure* failures of the runner itself
(e.g. the Node binary is missing), which map to HTTP error envelopes.
"""


class ExecutionError(Exception):
    """Base class for handled execution-infrastructure failures."""

    code = "execution_error"
    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code


class RunnerUnavailableError(ExecutionError):
    """The execution runtime (Node binary) could not be started."""

    code = "execution_runtime_unavailable"
    status_code = 503


class CodeTooLargeError(ExecutionError):
    """Submitted source exceeds the configured byte cap.

    Enforced in the service (not the request schema) so the single source of
    truth is ``settings.execute_max_code_bytes`` (docs/execution-architecture.md
    §7.4). Maps to HTTP 422 — a malformed request, like empty ``code``.
    """

    code = "code_too_large"
    status_code = 422
