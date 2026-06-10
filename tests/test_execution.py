"""Integration + unit tests for ``POST /api/v1/execute``.

Subprocess execution is always mocked: tests inject a fake
:class:`CodeRunner` or monkeypatch ``subprocess.Popen`` so the suite never
needs a real Node runtime in CI (acceptance criteria).
"""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.execution.dependencies import get_execution_service
from app.modules.execution.services.errors import RunnerUnavailableError
from app.modules.execution.services.execution_service import ExecutionService
from app.modules.execution.services.runner import (
    RunnerOutput,
    SubprocessNodeRunner,
)


def _login(client: TestClient, email: str = "exec-user@example.com") -> dict[str, str]:
    otp = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": email},
    ).json()["otp"]
    body = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    ).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


@dataclass
class FakeRunner:
    """A :class:`CodeRunner` stub that records its calls."""

    output: RunnerOutput
    calls: list[dict[str, object]] = field(default_factory=list)

    def run(self, *, code: str, timeout_ms: int) -> RunnerOutput:
        self.calls.append({"code": code, "timeout_ms": timeout_ms})
        return self.output


def _service_with(output: RunnerOutput, runner_box: list[FakeRunner] | None = None):
    """Build an override returning a real service wired to a fake runner."""

    def _override() -> ExecutionService:
        runner = FakeRunner(output=output)
        if runner_box is not None:
            runner_box.append(runner)
        return ExecutionService(
            runner,
            default_timeout_ms=settings.execute_default_timeout_ms,
            max_timeout_ms=settings.execute_max_timeout_ms,
            max_code_bytes=settings.execute_max_code_bytes,
        )

    return _override


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the execution feature flag for the duration of a test."""
    monkeypatch.setattr(settings, "enable_execute", True)


# ─── Feature-flag gate ────────────────────────────────────────────────────────


def test_execute_disabled_returns_503(client: TestClient) -> None:
    # Default config has ENABLE_EXECUTE=false; the gate fires before auth.
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": "console.log(1)"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "execute_disabled"


def test_execute_requires_auth_when_enabled(
    client: TestClient, enabled: None
) -> None:
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": "console.log(1)"},
    )
    assert response.status_code == 401


# ─── Status mapping (ok / error / timeout / unsupported_language) ─────────────


def test_execute_ok_returns_stdout_outputs(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)
    output = RunnerOutput(stdout="hello\n", stderr="", exit_code=0, timed_out=False)
    app.dependency_overrides[get_execution_service] = _service_with(output)
    try:
        response = client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "javascript", "code": "console.log('hello')"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["executedOn"] == "backend"
    # outputs mirror cell.outputs (UI runtime OutputItem) exactly.
    assert payload["outputs"] == [{"type": "stdout", "text": "hello\n"}]
    assert payload["stats"]["durationMs"] >= 0


def test_execute_runtime_error_returns_error_status(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)
    output = RunnerOutput(
        stdout="",
        stderr="ReferenceError: x is not defined",
        exit_code=1,
        timed_out=False,
    )
    app.dependency_overrides[get_execution_service] = _service_with(output)
    try:
        response = client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "javascript", "code": "x"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "error"
    types = [item["type"] for item in payload["outputs"]]
    assert types == ["stderr", "error"]
    assert payload["outputs"][-1]["message"] == "ReferenceError: x is not defined"


def test_execute_timeout_returns_timeout_status(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)
    output = RunnerOutput(stdout="partial", stderr="", exit_code=None, timed_out=True)
    app.dependency_overrides[get_execution_service] = _service_with(output)
    try:
        response = client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "javascript", "code": "while(true){}"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "timeout"
    assert payload["outputs"] == [{"type": "stdout", "text": "partial"}]


def test_execute_unsupported_language(client: TestClient, enabled: None) -> None:
    headers = _login(client)
    box: list[FakeRunner] = []
    output = RunnerOutput(stdout="", stderr="", exit_code=0, timed_out=False)
    app.dependency_overrides[get_execution_service] = _service_with(output, box)
    try:
        response = client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "python", "code": "print(1)"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "unsupported_language"
    assert payload["outputs"] == []
    # The runner must not run non-JavaScript code.
    assert box[0].calls == []


# ─── Timeout resolution / clamping ───────────────────────────────────────────


def test_execute_clamps_timeout_to_max(client: TestClient, enabled: None) -> None:
    headers = _login(client)
    box: list[FakeRunner] = []
    output = RunnerOutput(stdout="", stderr="", exit_code=0, timed_out=False)
    app.dependency_overrides[get_execution_service] = _service_with(output, box)
    try:
        client.post(
            f"{settings.api_prefix}/execute",
            json={
                "language": "javascript",
                "code": "1",
                "timeoutMs": 10_000_000,
            },
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    assert box[0].calls[0]["timeout_ms"] == settings.execute_max_timeout_ms


def test_execute_defaults_timeout_when_absent(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)
    box: list[FakeRunner] = []
    output = RunnerOutput(stdout="", stderr="", exit_code=0, timed_out=False)
    app.dependency_overrides[get_execution_service] = _service_with(output, box)
    try:
        client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "javascript", "code": "1"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    assert box[0].calls[0]["timeout_ms"] == settings.execute_default_timeout_ms


# ─── Request validation (422) ────────────────────────────────────────────────


def test_execute_rejects_empty_code(client: TestClient, enabled: None) -> None:
    headers = _login(client)
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": ""},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_execute_rejects_nonpositive_timeout(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": "1", "timeoutMs": 0},
        headers=headers,
    )
    assert response.status_code == 422


def test_execute_rejects_oversized_code(client: TestClient, enabled: None) -> None:
    headers = _login(client)
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": "a" * (262_144 + 1)},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "code_too_large"


def test_execute_code_size_limit_honours_setting(
    client: TestClient, enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cap is read from settings.execute_max_code_bytes at runtime, not a
    # hardcoded constant: lowering it rejects code far below the 256 KiB default.
    monkeypatch.setattr(settings, "execute_max_code_bytes", 16)
    headers = _login(client)
    response = client.post(
        f"{settings.api_prefix}/execute",
        json={"language": "javascript", "code": "a" * 17},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "code_too_large"


def test_execute_runtime_unavailable_returns_503(
    client: TestClient, enabled: None
) -> None:
    headers = _login(client)

    class BrokenRunner:
        def run(self, *, code: str, timeout_ms: int) -> RunnerOutput:
            raise RunnerUnavailableError("Code execution runtime is unavailable")

    def _override() -> ExecutionService:
        return ExecutionService(
            BrokenRunner(),
            default_timeout_ms=settings.execute_default_timeout_ms,
            max_timeout_ms=settings.execute_max_timeout_ms,
            max_code_bytes=settings.execute_max_code_bytes,
        )

    app.dependency_overrides[get_execution_service] = _override
    try:
        response = client.post(
            f"{settings.api_prefix}/execute",
            json={"language": "javascript", "code": "1"},
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_execution_service, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "execution_runtime_unavailable"


# ─── SubprocessNodeRunner unit tests (subprocess.Popen mocked) ────────────────


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for the runner unit tests.

    ``stdout``/``stderr`` are binary streams (the runner opens the pipes in
    binary mode and decodes itself), matching the real Popen contract the
    bounded reader threads consume.
    """

    def __init__(
        self,
        command: list[str],
        kwargs: dict[str, object],
        *,
        stdout: bytes,
        stderr: bytes,
        returncode: int,
        timeout: bool,
    ) -> None:
        self.command = command
        self.kwargs = kwargs
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._returncode = returncode
        self._timeout = timeout
        self.pid = 0x7FFFFFF0  # never a real pid in the test runner's session
        self.returncode: int | None = None
        self.kill_count = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._timeout and timeout is not None:
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self.kill_count += 1
        self.returncode = -9


def _patch_popen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    timeout: bool = False,
) -> list[_FakePopen]:
    """Replace ``runner.subprocess.Popen`` with a fake; return spawned fakes."""
    import app.modules.execution.services.runner as runner_module

    spawned: list[_FakePopen] = []

    def _factory(command, **kwargs):  # type: ignore[no-untyped-def]
        popen = _FakePopen(
            command,
            kwargs,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            timeout=timeout,
        )
        spawned.append(popen)
        return popen

    monkeypatch.setattr(runner_module.subprocess, "Popen", _factory)
    return spawned


def test_subprocess_runner_maps_completed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = _patch_popen(monkeypatch, stdout=b"out", stderr=b"err", returncode=0)
    runner = SubprocessNodeRunner(
        node_command="node", max_output_bytes=1024, max_memory_mb=64
    )
    result = runner.run(code="console.log('hi')", timeout_ms=5000)

    assert result == RunnerOutput(
        stdout="out", stderr="err", exit_code=0, timed_out=False
    )
    # Hardening: fixed argv (no shell), scrubbed env, isolated cwd, bounded pipes.
    kwargs = spawned[0].kwargs
    assert spawned[0].command[0] == "node"
    assert kwargs.get("shell", False) is False
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert "JWT_SECRET" not in kwargs["env"]
    assert set(kwargs["env"]).issubset({"PATH", "NODE_OPTIONS", "NO_COLOR"})
    assert kwargs["env"]["NODE_OPTIONS"] == ""


def test_subprocess_runner_group_kills_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.execution.services.runner as runner_module

    # Isolate the real signal call: assert the runner *invokes* the group-kill
    # on timeout rather than firing SIGKILL at a real process group.
    killed: list[object] = []
    monkeypatch.setattr(
        runner_module, "_terminate_process_group", lambda proc: killed.append(proc)
    )
    _patch_popen(monkeypatch, stdout=b"half", timeout=True)
    runner = SubprocessNodeRunner(
        node_command="node", max_output_bytes=1024, max_memory_mb=64
    )
    result = runner.run(code="while(true){}", timeout_ms=5000)

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.stdout == "half"
    # The whole process group is killed on timeout (no orphaned grandchildren).
    assert len(killed) == 1


def test_subprocess_runner_missing_node_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.modules.execution.services.runner as runner_module

    def _factory(command, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("node")

    monkeypatch.setattr(runner_module.subprocess, "Popen", _factory)
    runner = SubprocessNodeRunner(
        node_command="node", max_output_bytes=1024, max_memory_mb=64
    )
    with pytest.raises(RunnerUnavailableError):
        runner.run(code="1", timeout_ms=5000)


def test_subprocess_runner_truncates_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_popen(monkeypatch, stdout=b"x" * 100, returncode=0)
    runner = SubprocessNodeRunner(
        node_command="node", max_output_bytes=10, max_memory_mb=64
    )
    result = runner.run(code="1", timeout_ms=5000)
    assert len(result.stdout.encode("utf-8")) == 10


def test_bounded_reader_caps_memory_and_drains_pipe() -> None:
    # Proves the fix: the reader retains at most max_bytes *during* the run, yet
    # keeps draining the pipe so a chatty child never blocks on a full buffer.
    from app.modules.execution.services.runner import _BoundedStreamReader

    stream = io.BytesIO(b"y" * 10_000)
    reader = _BoundedStreamReader(stream, max_bytes=100)
    reader.run()  # run synchronously for a deterministic assertion

    assert len(reader._buffer) == 100  # noqa: SLF001 - white-box memory check
    assert reader.text == "y" * 100
    # The entire 10 KB stream was consumed even though only 100 B was retained.
    assert stream.read() == b""
