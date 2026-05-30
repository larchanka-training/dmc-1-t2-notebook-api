# FastAPI Template (MSD Course)

A FastAPI starter for the Modern Software Development course, organised
around a **multi-module architecture**: every domain module owns its
`controllers`, `services`, `schemas`, and — for modules backed by storage
— `models`, `repositories`, and a `dependencies` module that wires the
storage implementation.

See [`docs/domain-boundaries.md`](docs/domain-boundaries.md) for the
backend domain boundary spec (TARDIS-31): PostgreSQL schemas `users` /
`notebooks`, no cross-domain FK, repository protocol, layering rules.

## What is included

- FastAPI app with versioned API routing (`/api/v1`)
- Multi-module layout (`app/modules/<module>/{controllers,services,schemas,models,repositories}/` + per-module `dependencies.py`)
- Health module with **liveness** (`/health`) and **readiness** (`/health/ready`) probes
- Database layer scaffolding (SQLAlchemy 2, lazy engine, Liquibase changelogs)
- Structured logging via `structlog` (JSON-ready)
- Rich Swagger / OpenAPI documentation (`/docs`, `/redoc`, `/openapi.json`)
- Automated version bumping driven by OpenAPI schema changes
- Integration tests for app startup, routing and OpenAPI schema

## Project structure

```text
.
├── app
│   ├── core
│   │   ├── config.py          # Pydantic settings (env-driven)
│   │   ├── db.py              # SQLAlchemy engine + get_db dependency
│   │   └── logging.py         # structlog configuration
│   ├── modules
│   │   ├── health
│   │   │   ├── controllers/   # HTTP endpoints
│   │   │   ├── services/      # business logic
│   │   │   └── schemas/       # request / response contracts
│   │   ├── auth
│   │   │   ├── controllers/   # /auth/* HTTP endpoints
│   │   │   ├── dependencies.py # get_current_user DI factory
│   │   │   ├── models/        # SQLAlchemy ORM (users.users)
│   │   │   ├── repositories/  # UserRepository (DAL)
│   │   │   └── schemas/       # Pydantic DTOs
│   │   └── notebooks
│   │       ├── controllers/   # /notebooks/* HTTP endpoints (no SQLAlchemy)
│   │       ├── dependencies.py # get_notebook_service DI factory
│   │       ├── entities.py    # storage-neutral NotebookEntity
│   │       ├── models/        # SQLAlchemy ORM (notebooks.notebooks)
│   │       ├── repositories/  # NotebookRepository + NotebookRepositoryProtocol
│   │       ├── schemas/       # Pydantic DTOs
│   │       └── services/      # NotebookService (typed via protocol)
│   └── main.py                # FastAPI app, Swagger metadata
├── docs
│   ├── auth.md                # auth contract + placeholder-auth notes
│   ├── domain-boundaries.md   # TARDIS-31 spec: users/notebooks domains
│   ├── ci-cd.md
│   └── openapi.json           # committed OpenAPI snapshot
├── liquibase
│   ├── changelog/
│   │   ├── changelog-master.xml
│   │   └── changes/
│   │       ├── 0001-initial.xml             # historical (do not edit)
│   │       ├── 0002-users-notebooks.xml     # historical (do not edit)
│   │       ├── users/                       # domain-owned changesets
│   │       └── notebooks/                   # domain-owned changesets
│   └── liquibase.properties
├── scripts
│   └── openapi.py             # dump / bump tooling
├── tests
│   ├── test_health.py         # liveness contract test
│   └── test_startup.py        # integration: boot, routes, OpenAPI, readiness
├── .env.example
├── Dockerfile
├── pyproject.toml
└── requirements-dev.txt
```

## Quick start

1. Create and activate virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements-dev.txt
   ```

3. Copy env file:

   ```bash
   cp .env.example .env
   ```

4. Run the app:

   ```bash
   uvicorn app.main:app --reload
   ```

API docs:

- Swagger UI — `http://127.0.0.1:8000/docs`
- ReDoc — `http://127.0.0.1:8000/redoc`
- OpenAPI schema — `http://127.0.0.1:8000/openapi.json`

## Health endpoints

| Endpoint               | Purpose                                                    |
| ---------------------- | ---------------------------------------------------------- |
| `GET /api/v1/health`        | Liveness probe — process is alive (no external deps).      |
| `GET /api/v1/health/ready`  | Readiness probe — verifies DB connectivity; HTTP 200 with `status=degraded` when a component fails so traffic can be drained. |

Response envelope (`HealthResponse`):

```json
{
  "status": "ok",
  "app": "MSD FastAPI Template",
  "version": "0.1.0",
  "environment": "dev",
  "components": []
}
```

## Placeholder auth and notebooks

Issue #73 adds a dev-only placeholder user context and owner-scoped Notebook API.
Real OTP/JWT auth is a follow-up; during local development the API falls back to
the seeded dev user unless `X-User-Id` is provided.
When a valid `X-User-Id` does not exist yet, the placeholder dependency creates
a dev-only user row in `users.users` so the application-level
"owner exists before notebook is created" invariant holds. There is no
DB-level FK from `notebooks.notebooks.owner_id` — see
[`docs/domain-boundaries.md`](docs/domain-boundaries.md) §4.

```bash
curl http://127.0.0.1:8000/api/v1/auth/me
curl http://127.0.0.1:8000/api/v1/auth/me -H 'X-User-Id: 11111111-1111-1111-1111-111111111111'
```

Placeholder response:

```json
{
  "id": "00000000-0000-0000-0000-000000000001",
  "email": "dev@notebook.local",
  "displayName": "Dev User",
  "roles": []
}
```

Notebook endpoints:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/notebooks` | Create a notebook. Client-generated `id` is optional and supported for offline-first flows. |
| `GET /api/v1/notebooks` | List current user's active notebooks with `limit`, `offset`, `sort`, `order`. |
| `GET /api/v1/notebooks/{id}` | Return a full notebook including `cells`. |
| `PATCH /api/v1/notebooks/{id}` | Merge/update a notebook with LWW per-cell conflict handling. |
| `DELETE /api/v1/notebooks/{id}` | Soft-delete a notebook. |

Create example:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/notebooks \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Smoke",
    "formatVersion": 1,
    "cells": [
      {
        "id": "22222222-2222-2222-2222-222222222222",
        "kind": "code",
        "content": "console.log(1)",
        "updatedAt": 1779367200000
      }
    ]
  }'
```

Patch example with request-only tombstones:

```json
{
  "title": "Smoke patched",
  "formatVersion": 1,
  "cells": [
    {
      "id": "22222222-2222-2222-2222-222222222222",
      "kind": "code",
      "content": "console.log(2)",
      "updatedAt": 1779367500000
    }
  ],
  "deletedCells": [
    {
      "id": "33333333-3333-3333-3333-333333333333",
      "deletedAt": 1779367600000
    }
  ]
}
```

Notebook JSON uses `formatVersion`, `kind`, `content`, and Unix timestamps in
milliseconds. Execution output and UI runtime state are not persisted in
Notebook JSON v1.

New public API errors use a shared envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "fields": {}
  }
}
```

## Run tests

```bash
pytest
```

`tests/test_startup.py` is the integration suite: it boots the app via
`TestClient` (so the lifespan fires), checks that routes are registered,
the OpenAPI schema is generated, and the readiness probe behaves
correctly when the DB is healthy and when it fails (the `get_db`
dependency is overridden in-test, so no real Postgres is needed).

## OpenAPI-driven versioning

The project version (`pyproject.toml`) is treated as a contract version
for the public API. The committed snapshot at `docs/openapi.json` is the
source of truth; the helper `scripts/openapi.py` keeps it in sync:

```bash
# Refresh the committed snapshot from the running app
python scripts/openapi.py dump

# Detect drift and bump the version accordingly
python scripts/openapi.py bump            # writes pyproject.toml + snapshot
python scripts/openapi.py bump --dry-run  # report only
```

Semver rules applied by `bump`:

| Kind  | Trigger                                                             |
| ----- | ------------------------------------------------------------------- |
| MAJOR | Removed path **or** added/removed `required` field on a schema      |
| MINOR | New path added                                                      |
| PATCH | Anything else (descriptions, examples, response tweaks)             |

### Automatic rebuild on Swagger changes

The workflow `.github/workflows/openapi-version.yml` wires this up:

- **Pull requests** — runs `bump --dry-run`; fails the check if
  `docs/openapi.json` is stale (contributor must run `dump` and commit
  the diff).
- **Push to `main`** — runs `bump`, commits the new
  `pyproject.toml` + `docs/openapi.json`, tags the commit `vX.Y.Z`
  and pushes it.

The monorepo's `docker-publish.yml` listens for `v*.*.*` tags and
publishes the Docker image with tags `{{version}}` and
`{{major}}.{{minor}}` to GHCR, so a Swagger-visible change
auto-propagates into a new image without manual intervention.

## How to add a new module

1. Create the package skeleton:

   ```text
   app/modules/<module>/
   ├── __init__.py            # re-exports the module router
   ├── controllers/           # HTTP only, no SQLAlchemy imports
   ├── dependencies.py        # DI factories that wire repository → service
   ├── entities.py            # storage-neutral domain entities, if needed
   ├── models/                # SQLAlchemy ORM (if module owns storage)
   ├── repositories/          # DAL + repository Protocol (storage contract)
   ├── schemas/               # Pydantic request/response DTOs
   └── services/              # business rules, typed against entities + Protocol
   ```

   Controllers must depend only on `dependencies.get_<thing>` factories —
   never import `Session`, `select`, or a concrete repository class.
   Services must not import SQLAlchemy models; repository implementations map
   storage rows/documents to storage-neutral entities.
   See [`docs/domain-boundaries.md`](docs/domain-boundaries.md) §5–6 for
   the layering rules.

2. Re-export the router in `app/modules/<module>/__init__.py`.
3. Include it in `app/main.py`:

   ```python
   from app.modules.<module> import router as <module>_router
   app.include_router(<module>_router, prefix=settings.api_prefix)
   ```

4. Add a per-domain Liquibase changeset under
   `liquibase/changelog/changes/<domain>/` and wire it in via
   `changes/<domain>/changelog-<domain>.xml` included from
   `changelog-master.xml`. Historical files at `changes/` root are
   **append-only** — never edit or move them.
5. Add tests under `tests/` — use `app.dependency_overrides` to stub
   `get_db` and other dependencies.
