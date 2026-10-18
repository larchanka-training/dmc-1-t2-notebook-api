"""Tests for scripts/openapi.py and OpenAPI CI gate logic."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from scripts import openapi


@pytest.fixture
def temp_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a temporary pyproject.toml and docs/openapi.json for isolated CLI tests."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "fastapi-template"\nversion = "0.3.0"\ndescription = "test"\n',
        encoding="utf-8",
    )
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    snapshot = docs_dir / "openapi.json"

    monkeypatch.setattr(openapi, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(openapi, "PYPROJECT_PATH", pyproject)
    monkeypatch.setattr(openapi, "SNAPSHOT_PATH", snapshot)

    return pyproject, snapshot


def test_classify_change_none() -> None:
    schema = {"openapi": "3.1.0", "paths": {"/a": {}}, "components": {}}
    assert openapi._classify_change(schema, schema) == "none"


def test_classify_change_minor_new_path() -> None:
    old = {"openapi": "3.1.0", "paths": {"/a": {}}}
    new = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    assert openapi._classify_change(old, new) == "minor"


def test_classify_change_major_removed_path() -> None:
    old = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    new = {"openapi": "3.1.0", "paths": {"/a": {}}}
    assert openapi._classify_change(old, new) == "major"


def test_classify_change_major_added_or_removed_required_field() -> None:
    old = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {"User": {"type": "object", "required": ["id"]}}
        },
    }
    new_added = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {"User": {"type": "object", "required": ["id", "email"]}}
        },
    }
    new_removed = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {"User": {"type": "object", "required": []}}
        },
    }
    assert openapi._classify_change(old, new_added) == "major"
    assert openapi._classify_change(old, new_removed) == "major"


def test_classify_change_major_removed_schema() -> None:
    old = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {"User": {"type": "object"}, "Item": {"type": "object"}}
        },
    }
    new = {
        "openapi": "3.1.0",
        "components": {"schemas": {"User": {"type": "object"}}},
    }
    assert openapi._classify_change(old, new) == "major"


def test_classify_change_patch_for_descriptions() -> None:
    old = {"openapi": "3.1.0", "info": {"description": "v1"}}
    new = {"openapi": "3.1.0", "info": {"description": "v2"}}
    assert openapi._classify_change(old, new) == "patch"


@pytest.mark.parametrize(
    ("version", "kind", "expected"),
    [
        ("0.3.0", "patch", "0.3.1"),
        ("0.3.0", "minor", "0.4.0"),
        ("0.3.0", "major", "1.0.0"),
    ],
)
def test_bump_version_rules(version: str, kind: str, expected: str) -> None:
    assert openapi._bump(version, kind) == expected


def test_cmd_dump_writes_snapshot(
    temp_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, snapshot = temp_repo
    sample_schema = {"openapi": "3.1.0", "info": {"title": "Test App"}}
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: sample_schema)

    exit_code = openapi.cmd_dump(argparse.Namespace())
    assert exit_code == 0
    assert snapshot.exists()
    assert json.loads(snapshot.read_text(encoding="utf-8")) == sample_schema
    captured = capsys.readouterr()
    assert "Wrote OpenAPI schema to docs/openapi.json" in captured.out


def test_cmd_bump_matching_schema_does_not_mutate(
    temp_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject, snapshot = temp_repo
    sample_schema = {"openapi": "3.1.0", "info": {"title": "Test App"}}
    snapshot.write_text(json.dumps(sample_schema), encoding="utf-8")
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: sample_schema)

    pyproject_before = pyproject.read_text(encoding="utf-8")
    snapshot_before = snapshot.read_text(encoding="utf-8")

    exit_code = openapi.cmd_bump(argparse.Namespace(dry_run=False))
    assert exit_code == 0
    assert pyproject.read_text(encoding="utf-8") == pyproject_before
    assert snapshot.read_text(encoding="utf-8") == snapshot_before

    captured = capsys.readouterr()
    assert "OpenAPI schema unchanged. Version unchanged." in captured.out


def test_cmd_bump_drifted_schema_dry_run_does_not_mutate(
    temp_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject, snapshot = temp_repo
    old_schema = {"openapi": "3.1.0", "paths": {"/a": {}}}
    new_schema = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    snapshot.write_text(json.dumps(old_schema), encoding="utf-8")
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: new_schema)

    pyproject_before = pyproject.read_text(encoding="utf-8")
    snapshot_before = snapshot.read_text(encoding="utf-8")

    exit_code = openapi.cmd_bump(argparse.Namespace(dry_run=True))
    assert exit_code == 0
    assert pyproject.read_text(encoding="utf-8") == pyproject_before
    assert snapshot.read_text(encoding="utf-8") == snapshot_before

    captured = capsys.readouterr()
    assert "OpenAPI changed (minor). Bumping 0.3.0 -> 0.4.0" in captured.out
    assert "--dry-run: not writing snapshot or pyproject.toml" in captured.out


def test_cmd_bump_drifted_schema_writes_version_and_snapshot(
    temp_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject, snapshot = temp_repo
    old_schema = {"openapi": "3.1.0", "paths": {"/a": {}}}
    new_schema = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    snapshot.write_text(json.dumps(old_schema), encoding="utf-8")
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: new_schema)

    exit_code = openapi.cmd_bump(argparse.Namespace(dry_run=False))
    assert exit_code == 0

    assert 'version = "0.4.0"' in pyproject.read_text(encoding="utf-8")
    assert json.loads(snapshot.read_text(encoding="utf-8")) == new_schema

    captured = capsys.readouterr()
    assert "OpenAPI changed (minor). Bumping 0.3.0 -> 0.4.0" in captured.out
    assert "Updated pyproject.toml and docs/openapi.json" in captured.out


def test_ci_pr_gate_drift_detection_contract(
    temp_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the CI PR gate contract:

    1. Matching schema -> bump produces zero changes (clean working directory).
    2. Stale snapshot / schema drift -> bump mutates pyproject.toml + docs/openapi.json,
       which would cause git diff to exit 1.
    """
    pyproject, snapshot = temp_repo
    old_schema = {"openapi": "3.1.0", "paths": {"/a": {}}}
    snapshot.write_text(
        json.dumps(old_schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # 1. Matching schema scenario
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: old_schema)
    pyproject_orig = pyproject.read_text(encoding="utf-8")
    snapshot_orig = snapshot.read_text(encoding="utf-8")

    exit_code = openapi.cmd_bump(argparse.Namespace(dry_run=False))
    assert exit_code == 0
    # Files are completely unchanged
    assert pyproject.read_text(encoding="utf-8") == pyproject_orig
    assert snapshot.read_text(encoding="utf-8") == snapshot_orig

    # 2. Schema drift scenario (new endpoint added in code)
    new_schema = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    monkeypatch.setattr(openapi, "_load_app_schema", lambda: new_schema)

    exit_code = openapi.cmd_bump(argparse.Namespace(dry_run=False))
    assert exit_code == 0
    # Files are now mutated, triggering git diff in CI
    assert pyproject.read_text(encoding="utf-8") != pyproject_orig
    assert snapshot.read_text(encoding="utf-8") != snapshot_orig
    assert 'version = "0.4.0"' in pyproject.read_text(encoding="utf-8")


def test_schema_generator_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the FastAPI app or generator fails to boot, the CLI must raise / fail."""
    def _failing_loader() -> dict[str, Any]:
        raise RuntimeError("FastAPI route loading syntax error")

    monkeypatch.setattr(openapi, "_load_app_schema", _failing_loader)

    with pytest.raises(RuntimeError, match="FastAPI route loading syntax error"):
        openapi.cmd_bump(argparse.Namespace(dry_run=False))


def test_main_cli_execution_failure_exits_nonzero() -> None:
    """The main entrypoint must return non-zero when an uncaught error occurs."""
    with patch.object(openapi, "_load_app_schema", side_effect=Exception("Generator crashed")):
        with patch.object(openapi.sys, "argv", ["openapi.py", "bump"]):
            with pytest.raises(Exception, match="Generator crashed"):
                openapi.main()
