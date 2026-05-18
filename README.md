# FastAPI Template (MSD Course)

A FastAPI starter for the Modern Software Development course, organised
around a **multi-module architecture**: every domain module owns its
`controllers`, `services` and `schemas`.

## What is included

- FastAPI app with versioned API routing (`/api/v1`)
- Multi-module layout (`app/modules/<module>/{controllers,services,schemas}/`)
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
│   │   └── health
│   │       ├── controllers/   # HTTP endpoints
│   │       ├── services/      # business logic
│   │       └── schemas/       # request / response contracts
│   └── main.py                # FastAPI app, Swagger metadata
├── docs
│   └── openapi.json           # committed OpenAPI snapshot
├── liquibase
│   ├── changelog/             # master + per-module changesets
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
   ├── controllers/
   ├── services/
   └── schemas/
   ```

2. Re-export the router in `app/modules/<module>/__init__.py`.
3. Include it in `app/main.py`:

   ```python
   from app.modules.<module> import router as <module>_router
   app.include_router(<module>_router, prefix=settings.api_prefix)
   ```

4. Add a per-module Liquibase changeset under
   `liquibase/changelog/changes/` and include it from
   `changelog-master.xml`.
5. Add tests under `tests/` — use `app.dependency_overrides` to stub
   `get_db` and other dependencies.
