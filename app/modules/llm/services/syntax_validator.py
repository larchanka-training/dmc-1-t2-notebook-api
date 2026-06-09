"""Syntax validation for generated JavaScript/TypeScript code via esbuild."""

from dataclasses import dataclass
import subprocess

from app.modules.llm.services.errors import LlmProviderNotConfiguredError


@dataclass(frozen=True)
class SyntaxValidationResult:
    """Result of a syntax validation attempt."""

    ok: bool
    error: str | None = None


class EsbuildSyntaxValidator:
    """Run esbuild as a subprocess without executing generated code.

    Notes
    -----
    Format choice
        We deliberately do **not** pass ``--format=esm``. The QuickJS
        sandbox used downstream (`docs/execution-architecture.md`)
        evaluates code as a classic script by default, which is
        sloppy-mode. Forcing ESM here would parse every input in strict
        mode and reject sloppy-mode constructs that QuickJS would
        happily accept. esbuild auto-detects script vs module from the
        presence of ``import``/``export`` statements when ``--format``
        is omitted, which matches the runtime behaviour.

    Missing binary
        ``FileNotFoundError`` from the subprocess call means the
        ``esbuild`` CLI is not installed in the runtime image — this is
        an environment misconfiguration, not user-code failure. The
        validator raises :class:`LlmProviderNotConfiguredError` so the
        endpoint returns ``503`` instead of misleading ``422``.
    """

    def __init__(self, command: str = "esbuild", timeout_seconds: float = 5.0) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds

    def validate(self, code: str, language: str) -> SyntaxValidationResult:
        """Validate code syntax with esbuild transform over stdin."""
        if not code.strip():
            return SyntaxValidationResult(ok=False, error="Generated code is empty")

        loader = "ts" if language == "typescript" else "js"
        try:
            completed = subprocess.run(
                [
                    self.command,
                    "--log-level=warning",
                    f"--loader={loader}",
                ],
                input=code,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            # esbuild binary is missing — env problem, not user code.
            # Surface as 503 so the operator notices, not as 422 against
            # the model.
            raise LlmProviderNotConfiguredError(
                f"esbuild command not found: {self.command}"
            ) from exc
        except subprocess.TimeoutExpired:
            return SyntaxValidationResult(
                ok=False,
                error=f"esbuild validation timed out after {self.timeout_seconds:g}s",
            )

        if completed.returncode == 0:
            return SyntaxValidationResult(ok=True)

        error = (completed.stderr or completed.stdout or "esbuild validation failed").strip()
        return SyntaxValidationResult(ok=False, error=error)
