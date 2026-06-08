"""Syntax validation for generated JavaScript/TypeScript code via esbuild."""

from dataclasses import dataclass
import subprocess


@dataclass(frozen=True)
class SyntaxValidationResult:
    """Result of a syntax validation attempt."""

    ok: bool
    error: str | None = None


class EsbuildSyntaxValidator:
    """Run esbuild as a subprocess without executing generated code."""

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
                    "--format=esm",
                ],
                input=code,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError:
            return SyntaxValidationResult(
                ok=False,
                error=f"esbuild command not found: {self.command}",
            )
        except subprocess.TimeoutExpired:
            return SyntaxValidationResult(
                ok=False,
                error=f"esbuild validation timed out after {self.timeout_seconds:g}s",
            )

        if completed.returncode == 0:
            return SyntaxValidationResult(ok=True)

        error = (completed.stderr or completed.stdout or "esbuild validation failed").strip()
        return SyntaxValidationResult(ok=False, error=error)
