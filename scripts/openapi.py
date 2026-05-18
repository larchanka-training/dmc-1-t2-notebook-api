"""Tooling around the OpenAPI schema and its impact on the project version.

Two responsibilities:

  1. `dump`   — write the current OpenAPI schema to `docs/openapi.json`.
  2. `bump`   — compare current schema vs the committed snapshot and, if it
                 changed, bump the version in `pyproject.toml` according to
                 simple semver rules:
                   * MAJOR — removed path or removed required field
                            (breaking change)
                   * MINOR — added path or added optional field
                            (new functionality)
                   * PATCH — anything else (descriptions, examples, ...)

Exit codes for `bump`:
    0 — schema unchanged or version successfully bumped
    1 — internal error

CI wires this into a workflow that runs `bump`, then commits the updated
`docs/openapi.json` and `pyproject.toml`. Pushing those changes triggers
the existing Docker publish workflow, so a Swagger-visible change
auto-rebuilds the image with the new tag.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

ChangeKind = Literal["none", "patch", "minor", "major"]

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "docs" / "openapi.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_app_schema() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    from app.main import app  # noqa: WPS433 — late import is intentional

    return app.openapi()


def _read_snapshot() -> dict[str, Any] | None:
    if not SNAPSHOT_PATH.exists():
        return None
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _write_snapshot(schema: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _read_version() -> str:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Could not locate version in pyproject.toml")
    return match.group(1)


def _write_version(version: str) -> None:
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    new_text = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        rf'\1"{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    PYPROJECT_PATH.write_text(new_text, encoding="utf-8")


def _bump(version: str, kind: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _classify_change(old: dict[str, Any], new: dict[str, Any]) -> ChangeKind:
    if old == new:
        return "none"

    old_paths = set((old.get("paths") or {}).keys())
    new_paths = set((new.get("paths") or {}).keys())

    if old_paths - new_paths:
        return "major"

    old_schemas = (old.get("components") or {}).get("schemas") or {}
    new_schemas = (new.get("components") or {}).get("schemas") or {}
    for name, old_def in old_schemas.items():
        new_def = new_schemas.get(name)
        if new_def is None:
            return "major"
        old_required = set(old_def.get("required") or [])
        new_required = set(new_def.get("required") or [])
        if old_required - new_required:
            return "major"
        if new_required - old_required:
            return "major"

    if new_paths - old_paths:
        return "minor"

    return "patch"


def cmd_dump(_: argparse.Namespace) -> int:
    schema = _load_app_schema()
    _write_snapshot(schema)
    print(f"Wrote OpenAPI schema to {SNAPSHOT_PATH.relative_to(REPO_ROOT)}")
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    schema = _load_app_schema()
    old = _read_snapshot()
    if old is None:
        _write_snapshot(schema)
        print("No previous snapshot found; created baseline. Version unchanged.")
        return 0

    kind = _classify_change(old, schema)
    if kind == "none":
        print("OpenAPI schema unchanged. Version unchanged.")
        return 0

    old_version = _read_version()
    new_version = _bump(old_version, kind)
    print(f"OpenAPI changed ({kind}). Bumping {old_version} -> {new_version}")

    if args.dry_run:
        print("--dry-run: not writing snapshot or pyproject.toml")
        return 0

    _write_version(new_version)
    _write_snapshot(schema)
    print(f"Updated {PYPROJECT_PATH.name} and {SNAPSHOT_PATH.relative_to(REPO_ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dump", help="Write current OpenAPI schema to docs/openapi.json")

    bump = sub.add_parser(
        "bump",
        help="Bump version if OpenAPI schema diverges from docs/openapi.json",
    )
    bump.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the bump kind without writing any files",
    )

    args = parser.parse_args()
    handlers = {"dump": cmd_dump, "bump": cmd_bump}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
