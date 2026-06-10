"""Code runner abstraction + a subprocess-backed Node implementation.

The runner is the single seam that touches the operating system. Tests
inject a fake :class:`CodeRunner` (or monkeypatch ``subprocess.Popen``) so the
suite never needs a real Node runtime — see ``tests/test_execution.py``.

.. warning::

   :class:`SubprocessNodeRunner` is a **debug/fallback** runner, **not** a
   production-grade sandbox. It applies best-effort hardening (no shell, a
   scrubbed environment, an isolated temp working directory, a wall-clock
   timeout, a heap cap, **bounded** output capture, and — on POSIX — a
   CPU/file-size rlimit plus a new session so a timeout group-kills the whole
   process tree), but a plain ``node`` child process is **not** isolated from
   the host kernel, filesystem, or network. The production target remains the
   QuickJS Execution Worker described in ``docs/execution-architecture.md``
   §7.3. Keep ``ENABLE_EXECUTE=false`` unless a hardened runtime is in place.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger
from app.modules.execution.services.errors import RunnerUnavailableError

logger = get_logger(__name__)

# Size of each pipe read. Output beyond the byte cap is drained and discarded
# rather than buffered, so this only bounds syscall granularity, not memory.
_READ_CHUNK_BYTES = 8_192


@dataclass(frozen=True)
class RunnerOutput:
    """Raw outcome of a single code run, before mapping to the API contract."""

    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


class CodeRunner(Protocol):
    """Boundary used by the service and stubbed by tests."""

    def run(self, *, code: str, timeout_ms: int) -> RunnerOutput:
        """Execute ``code`` and return its raw output within ``timeout_ms``."""
        ...


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate ``text`` so its UTF-8 encoding fits ``max_bytes``."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class _BoundedStreamReader(threading.Thread):
    """Drain a binary pipe, retaining at most ``max_bytes`` bytes.

    The whole point is to keep the API process's memory bounded *while* the
    child runs: once the cap is reached we keep reading (so the child never
    blocks on a full pipe) but discard the overflow instead of buffering it.
    """

    def __init__(self, stream, max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._max_bytes = max_bytes
        self._buffer = bytearray()

    def run(self) -> None:  # pragma: no cover - exercised via SubprocessNodeRunner
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                remaining = self._max_bytes - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
                # Past the cap: drop the chunk but keep looping to drain the pipe.
        except (ValueError, OSError):
            # Pipe closed underneath us (e.g. after a group-kill) — stop quietly.
            pass

    @property
    def text(self) -> str:
        """Decode the retained bytes as UTF-8 (lossy on a split codepoint)."""
        return self._buffer.decode("utf-8", errors="ignore")


def _build_rlimit_preexec(cpu_seconds: int, max_output_bytes: int):
    """Return a POSIX ``preexec_fn`` that applies defense-in-depth rlimits.

    Best-effort only: a failure to set a limit must not break the run, and the
    hook is unavailable on non-POSIX platforms (returns ``None`` there).
    """
    if sys.platform.startswith("win"):
        return None

    import resource  # POSIX-only; imported lazily.

    def _set_limits() -> None:  # pragma: no cover - runs in the child process
        # Start a new session/process group so a timeout can kill the whole
        # tree (see _terminate_process_group), not just the direct child.
        os.setsid()
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except (ValueError, OSError):
            pass
        try:
            # Cap the size of any single file the child may write.
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (max_output_bytes, max_output_bytes)
            )
        except (ValueError, OSError):
            pass

    return _set_limits


def _scrubbed_env() -> dict[str, str]:
    """Return a minimal environment so user code cannot read host secrets."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NODE_OPTIONS": "",
        "NO_COLOR": "1",
    }


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Kill the child *and everything it spawned* after a timeout.

    The child is a session/group leader (``os.setsid`` on POSIX,
    ``start_new_session`` on platforms without a preexec hook), so on POSIX a
    single ``killpg`` reaps grandchildren that would otherwise outlive the
    timeout as orphans. Falls back to killing the direct child if the group
    signal is unavailable.
    """
    if not sys.platform.startswith("win"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


class SubprocessNodeRunner:
    """Run JavaScript by shelling out to a ``node`` child process.

    Not a sandbox — see the module docstring.
    """

    def __init__(
        self,
        *,
        node_command: str,
        max_output_bytes: int,
        max_memory_mb: int,
    ) -> None:
        self._node_command = node_command
        self._max_output_bytes = max_output_bytes
        self._max_memory_mb = max_memory_mb

    def run(self, *, code: str, timeout_ms: int) -> RunnerOutput:
        """Execute ``code`` with ``node`` under best-effort limits."""
        timeout_seconds = timeout_ms / 1000
        # Give the CPU rlimit a little slack over the wall clock so the
        # wall-clock timeout is the primary stop signal.
        cpu_seconds = math.ceil(timeout_seconds) + 1

        with tempfile.TemporaryDirectory(prefix="execute-") as workdir:
            script_path = os.path.join(workdir, "script.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(code)

            command = [
                self._node_command,
                f"--max-old-space-size={self._max_memory_mb}",
                script_path,
            ]
            preexec = _build_rlimit_preexec(cpu_seconds, self._max_output_bytes)

            try:
                # Binary pipes + bounded reader threads: stdout/stderr are
                # drained as they arrive and capped at ``max_output_bytes``, so
                # a chatty program cannot inflate the API process's memory the
                # way subprocess.run(capture_output=True) would (it buffers the
                # whole stream before any truncation).
                proc = subprocess.Popen(  # noqa: S603 - no shell, fixed argv
                    command,
                    cwd=workdir,
                    env=_scrubbed_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=preexec is None,
                    preexec_fn=preexec,
                )
            except FileNotFoundError as exc:
                logger.error(
                    "execution.runner.unavailable",
                    node_command=self._node_command,
                    error_type=type(exc).__name__,
                )
                raise RunnerUnavailableError(
                    "Code execution runtime is unavailable"
                ) from exc

            out_reader = _BoundedStreamReader(proc.stdout, self._max_output_bytes)
            err_reader = _BoundedStreamReader(proc.stderr, self._max_output_bytes)
            out_reader.start()
            err_reader.start()

            timed_out = False
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
                proc.wait()
            finally:
                # Readers finish once the pipes hit EOF (process fully reaped).
                out_reader.join()
                err_reader.join()

            return RunnerOutput(
                stdout=_truncate(out_reader.text, self._max_output_bytes),
                stderr=_truncate(err_reader.text, self._max_output_bytes),
                exit_code=None if timed_out else proc.returncode,
                timed_out=timed_out,
            )
