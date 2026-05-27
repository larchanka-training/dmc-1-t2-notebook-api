# AGENTS.md

Project-level guidance for AI coding agents (Claude Code, Cursor,
Copilot, etc.) working in this `api/` codebase. This file is an
**index of pointers**, not full content — read the linked docs on
demand.

Human-facing documentation is in [`README.md`](./README.md) and
[`docs/`](./docs/).

> `api/` is a **git submodule** of the
> [`dmc-1-t2-notebook-mono`](https://github.com/larchanka-training/dmc-1-t2-notebook-mono)
> monorepo. Submodule push discipline applies: commit + push inside
> `api/` first, then bump the pointer in the monorepo (see the
> monorepo's `AGENTS.md` §2, §7).

---

## Skill

For any non-trivial task in this codebase, load the project-level
backend skill. It lives in the **monorepo root**, not inside this
submodule — so the path depends on where your working directory is.

**Path to the skill, by current directory:**

| Your cwd | Path to `notebook-api` skill |
|---|---|
| Monorepo root (`dmc-1-t2-notebook-mono/`) | `.agents/skills/notebook-api/SKILL.md` |
| This submodule (`dmc-1-t2-notebook-mono/api/`) | `../.agents/skills/notebook-api/SKILL.md` |

`api/` is a git submodule, so opening your terminal / IDE directly
in `dmc-1-t2-notebook-mono/api/` is a normal scenario — in that
case prefix the monorepo paths with `../`.

The skill and its references (resolve relative to the table above —
add `../` when you are inside `api/`):

- `.agents/skills/notebook-api/SKILL.md` — modular layout, Liquibase
  discipline, OpenAPI dump → ui sync, pytest with
  `dependency_overrides`, JWT + email-OTP auth model
- `.agents/skills/notebook-api/references/liquibase-migrations.md` —
  DB schema discipline
- `.agents/skills/notebook-api/references/openapi-sync.md` — the
  api → ui contract workflow

**Standalone clone of `api/` (without the monorepo):** those
monorepo files are not available at all — no `../` will reach them.
Treat this `AGENTS.md` as the local fallback and the rules below as
the minimal contract.

---

## Stack

- **Python 3.12**, **FastAPI** on a versioned `/api/v1`
- **SQLAlchemy 2.0**, **PostgreSQL 16**
- **Liquibase** for migrations (folder `liquibase/`)
- **JWT (HS256)** access + opaque refresh token, **email OTP** sign-in
  (target model in [`docs/auth.md`](./docs/auth.md))
- **structlog** for JSON-ready logging
- **pytest** via `TestClient`; `app.dependency_overrides` for stubbing
  `get_db` and other dependencies

Full stack list and the rationale: monorepo `AGENTS.md` §3,
[`README.md`](./README.md), and the monorepo's
`docs/backend-recommendations.md`.

---

## Module layout

Multi-module, one router per module:

```text
app/
├── core/                    # config, db, logging — project-wide
├── modules/<name>/
│   ├── __init__.py          # re-exports `router`
│   ├── controllers/         # FastAPI endpoints
│   ├── services/            # business logic
│   └── schemas/             # Pydantic DTOs
└── main.py                  # app factory + router includes
```

Module name = domain noun (`auth`, `notebooks`, `llm`), not
technical (`utils`, `helpers`). Cross-module dependencies go through
`services/` interfaces.

How to add a new module: [`README.md`](./README.md) → "How to add a
new module".

---

## Conventions that are easy to miss

These are the rules a generic FastAPI habit will violate:

- **Database changes ship as Liquibase changesets** under
  `liquibase/changelog/changes/`, included from
  `changelog-master.xml`. Append-only. No raw SQL outside a
  changeset; no schema mutations from app startup. The
  monorepo's `notebook-api/references/liquibase-migrations.md`
  documents the full procedure.
- **Any API contract change → `python scripts/openapi.py dump`** and
  commit `docs/openapi.json`. The PR-time
  `openapi.py bump --dry-run` check fails on stale snapshots. The ui
  consumes the snapshot via `pnpm api:generate`. See the monorepo's
  `notebook-api/references/openapi-sync.md`.
- **[`docs/auth.md`](./docs/auth.md) is the auth contract** — OTP →
  JWT (15 min) + refresh rotation with reuse-detection. The current
  `/auth/login` password endpoint is a temporary stub (§1) — do not
  extend the password path; replace it with the OTP flow.
- **`docs/auth.md` is paired with `ui/docs/auth.md`** in the ui
  submodule. Both must move together — never edit one without the
  other (monorepo `AGENTS.md` §10).
- **OTP code is never returned in `prod` mode** (`docs/auth.md` §6 —
  defence-in-depth). The handler must branch on `APP_ENV`.
- **`app.dependency_overrides` is a test-only affordance.** Do not
  use it as a runtime config switch.
- **`structlog`, not `print()`** in application code.
- **Residue from the pre-OTP design** (`oauth_name_*`,
  `token_ttl_seconds=86400`) is being migrated out — do not extend
  it; align with the env table in `docs/auth.md` §12.

---

## Local checks

```bash
ruff check .
pytest
```

CI runs the same via the monorepo's `.github/workflows/api-ci.yml`.

---

## OpenAPI

`docs/openapi.json` is the committed snapshot — the source of truth
for the ui's generated types.

```bash
python scripts/openapi.py dump            # refresh snapshot
python scripts/openapi.py bump --dry-run  # what CI runs on a PR
```

See [`README.md`](./README.md) → "OpenAPI-driven versioning" and the
monorepo's `notebook-api/references/openapi-sync.md`.

---

## Docs

- [`docs/auth.md`](./docs/auth.md) — auth, persistence, conflict
  resolution, versioning (the long one)
- [`docs/ci-cd.md`](./docs/ci-cd.md) — backend CI notes
- [`docs/openapi.json`](./docs/openapi.json) — generated; do not
  hand-edit
- [`README.md`](./README.md) — module layout, OpenAPI tooling, "How
  to add a new module"
