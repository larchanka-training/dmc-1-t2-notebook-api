# Backend Domain Boundaries — `users` / `notebooks`

> Architecture document. Describes backend `api` domain boundaries after
> splitting PostgreSQL schemas into `users` and `notebooks` and introducing
> `NotebookRepositoryProtocol`. Matches **TARDIS-31** requirements.

## Table of Contents

1. [Goals and Context](#1-goals-and-context)
2. [Domains and Responsibilities](#2-domains-and-responsibilities)
3. [Physical PostgreSQL Schema Split](#3-physical-postgresql-schema-split)
4. [Domain Link: `owner_id` Only](#4-domain-link-owner_id-only)
5. [Layering: controller -> service -> repository](#5-layering-controller---service---repository)
6. [Storage Boundary: `NotebookRepositoryProtocol`](#6-storage-boundary-notebookrepositoryprotocol)
7. [Liquibase: Append-Only Strategy](#7-liquibase-append-only-strategy)
8. [Terminology: `auth` Module vs `users` Schema](#8-terminology-auth-module-vs-users-schema)
9. [Out of Scope](#9-out-of-scope)
10. [Acceptance Criteria Traceability](#10-acceptance-criteria-traceability)

---

## 1. Goals and Context

After the MVP from PR #29, backend `api` already has separate `auth` and
`notebooks` modules in code, but both database tables (`app.users` and
`app.notebooks`) still live in the shared PostgreSQL schema `app` and are
connected by a database-level FK: `notebooks.owner_id -> users.id`.

This document records the Tech Lead decision from **2026-05-30**:

- physically split PostgreSQL schemas;
- remove the database-level FK;
- prepare `notebooks` for a possible future move to NoSQL without rewriting
  the public API.

Goals:

- the database physically reflects domain boundaries;
- `notebooks` storage can be replaced (SQL -> NoSQL) without changing
  controllers, services, or the public API;
- future `auth` development (OTP / JWT / sessions / refresh tokens) does not
  affect `notebooks` tables and code, and vice versa.

Out of scope is listed explicitly in [section 9](#9-out-of-scope): this task
does not implement real auth, NoSQL infrastructure, or API changes.

## 2. Domains and Responsibilities

| Domain | DB schema | Code module | Responsibility |
|---|---|---|---|
| **users** (identity) | `users` | `app/modules/auth/` | User, email, display name; future OTP, sessions, refresh tokens |
| **notebooks** | `notebooks` | `app/modules/notebooks/` | Notebook documents, cells (JSONB), LWW merge, soft delete; future execution metadata |

Shared infrastructure lives in `app/core/`:

- `core/db.py` — SQLAlchemy engine, session factory, `get_db` dependency;
- `core/errors.py` — error envelope and handlers;
- `core/time.py` — Unix-ms / datetime conversion;
- `core/config.py` — Pydantic settings;
- `core/logging.py` — structlog setup.

`main.py` assembles the app and includes routers. It is application wiring, not
a domain.

## 3. Physical PostgreSQL Schema Split

**Target state:**

```text
users.users
notebooks.notebooks
```

**Why physical schemas instead of one schema plus careful code namespaces:**

- the database clearly shows which domain owns which table; `\dt users.*` and
  `\dt notebooks.*` are enough to inspect ownership;
- PostgreSQL permissions can be granted per schema, for example
  `GRANT ... ON SCHEMA notebooks TO ...`; this becomes useful if `users` and
  `notebooks` later become separate services or are read by separate roles;
- `pg_dump --schema=notebooks` gives an independent domain backup;
- accidental joins across domains become less likely, especially after the FK
  is removed (see section 4).

**Costs and why we accept them:**

- ad-hoc SQL needs the full name, for example
  `SELECT * FROM notebooks.notebooks` instead of `SELECT * FROM notebooks`;
  this is minor;
- `users.users` looks repetitive, but this is normal PostgreSQL practice: the
  first `users` is the namespace/schema, the second `users` is the table.

## 4. Domain Link: `owner_id` Only

`notebooks.notebooks.owner_id` is a plain `uuid NOT NULL` column **without a
database-level FK** to `users.users.id`. SQLAlchemy `relationship()` between
`User` and `Notebook` **must not be added**.

**Why the FK is removed:**

- the `notebooks` domain is being prepared for a possible future move to NoSQL
  (Mongo/Dynamo/document store). Cross-database FKs do not exist there;
- keeping the FK would make the future move require rewriting migrations,
  constraint behavior, and related integration tests;
- by enforcing "integrity at the application layer" now, we avoid technical
  debt that would surface during the NoSQL transition.

**How `owner_id` integrity is enforced now:**

- the placeholder auth flow materializes a user on every request through
  `UserRepository.get_or_create_placeholder_user`; therefore by the time
  `NotebookService.create` uses `current_user.id` as `owner_id`, the row
  already exists in `users.users`;
- real auth (OTP / JWT) is a separate task (see `docs/auth.md`), but its
  contract requires a user to exist in the database before that user can write
  anything into `notebooks`.

**What we lose without the FK:**

- PostgreSQL no longer guarantees that every `owner_id` has a matching row in
  `users.users`. If the application violates its invariant, orphan notebooks
  may appear. This is an intentional compromise.

## 5. Layering: controller -> service -> repository

```text
HTTP request
  -> Controller (HTTP-only: URL, status, query/body, Depends)
  -> Schema/DTO (Pydantic validation)
  -> Service (business rules: owner check, formatVersion, merge)
  -> Entity (storage-neutral NotebookEntity)
  -> Repository (data access; maps entity <-> storage model)
  -> ORM model (table mapping)
```

Hard rules:

- **Controller does not import SQLAlchemy**: no `Session`, `select`, `flush`,
  etc. It also does not import a concrete repository class.
- **Service does not import SQLAlchemy**: it uses Pydantic schemas,
  `NotebookEntity`, and the repository protocol.
- **Repository is the only place that owns SQLAlchemy access.**

This is not overengineering. These rules exist so that a future SQL -> NoSQL
replacement touches only the repository layer.

## 6. Storage Boundary: `NotebookRepositoryProtocol`

`NotebookService` depends on the structural contract
`NotebookRepositoryProtocol` (PEP 544), not on the concrete
`NotebookRepository` class. Any class with methods matching the protocol is a
valid implementation; explicit inheritance from `Protocol` is not required.

```python
from typing import Protocol

class NotebookRepositoryProtocol(Protocol):
    def get_by_id(self, notebook_id: UUID) -> NotebookEntity | None: ...
    def list_by_owner(
        self, owner_id: UUID, limit: int, offset: int, sort: str, order: str
    ) -> tuple[list[NotebookEntity], int]: ...
    def save(self, notebook: NotebookEntity) -> NotebookEntity: ...
    def soft_delete(
        self, notebook: NotebookEntity, deleted_at: datetime
    ) -> NotebookEntity: ...
```

```python
class NotebookService:
    def __init__(self, repository: NotebookRepositoryProtocol) -> None:
        self.repository = repository
```

`NotebookRepository` (SQLAlchemy) remains the concrete MVP implementation, but
it maps ORM rows to `NotebookEntity` before returning data to the service. In
the future, another implementation such as `MongoNotebookRepository` can be
added, and the DI factory will choose which one to inject. The service and
controllers continue to work with the same entity/protocol boundary.

**Location of the `get_notebook_service` DI factory:** it lives in
`app/modules/notebooks/dependencies.py`, so controllers import only a ready
dependency and do not know about `Session` or a concrete repository class.
This supports the ticket acceptance criterion that controllers and services
must not import SQLAlchemy or storage-specific details.

**Transactional invariant:** current operations are atomic only at the
single-notebook level. A notebook is stored as one document-like aggregate
(`cells` JSONB today; one document in a future NoSQL store), and operations
create, patch, or soft-delete that one aggregate. Cross-notebook or
cross-document transactions are out of scope and must not become a hidden
requirement for future NoSQL implementations.

## 7. Liquibase: Append-Only Strategy

`0002-users-notebooks.xml` may already have been applied in a local database,
CI, or another shared environment. Liquibase stores checksums for applied
changesets in `DATABASECHANGELOG`; editing an applied file causes validation
errors such as `Validation Failed: 1 changesets check sum`.

Therefore:

- **`0001-initial.xml` and `0002-users-notebooks.xml` are not edited**; they
  are historical files;
- new changesets are added on top and describe the difference between the
  current database state and the target state.

Target changelog structure:

```text
api/liquibase/changelog/
├── changelog-master.xml
└── changes/
    ├── 0001-initial.xml                                    # historical
    ├── 0002-users-notebooks.xml                            # historical
    ├── users/
    │   ├── changelog-users.xml
    │   └── 0003-move-users-schema.xml
    └── notebooks/
        ├── changelog-notebooks.xml
        └── 0003-move-notebooks-schema.xml
```

The master changelog includes files in dependency order:

```xml
<include file="changes/0001-initial.xml" relativeToChangelogFile="true"/>
<include file="changes/0002-users-notebooks.xml" relativeToChangelogFile="true"/>
<include file="changes/users/changelog-users.xml" relativeToChangelogFile="true"/>
<include file="changes/notebooks/changelog-notebooks.xml" relativeToChangelogFile="true"/>
```

The new changesets do the following:

```sql
-- users/0003-move-users-schema.xml
CREATE SCHEMA IF NOT EXISTS users;
ALTER TABLE app.users SET SCHEMA users;

-- notebooks/0003-move-notebooks-schema.xml
ALTER TABLE app.notebooks DROP CONSTRAINT IF EXISTS notebooks_owner_id_fkey;
CREATE SCHEMA IF NOT EXISTS notebooks;
ALTER TABLE app.notebooks SET SCHEMA notebooks;
```

`notebooks_owner_id_fkey` is the standard PostgreSQL-generated name for an
inline `REFERENCES` constraint on the `owner_id` column of the `notebooks`
table. `IF EXISTS` protects the migration if the constraint has already been
dropped manually.

The `app` schema remains empty after migration. This PR **does not drop
`app`**, so the step stays reversible and independent. Dropping `app` can be a
small follow-up changeset if needed.

## 8. Terminology: `auth` Module vs `users` Schema

- `app/modules/auth/` is the Python module that implements `/auth/me` and
  current-user logic. Its name follows the module's API role.
- `users` is the PostgreSQL schema that stores identity tables. Its name
  follows schema contents.

These names are intentionally different. Over time, the `auth` module may own
multiple tables in the `users` schema: `users.users`, `users.otps`,
`users.sessions`, `users.refresh_tokens`.

Conversely, not every table in the `users` schema automatically belongs to the
`auth` module. In the future, the `users` domain may grow a dedicated
`app/modules/users/` module with user CRUD endpoints.

## 9. Out of Scope

TARDIS-31 does **not** implement:

- real OTP / JWT auth;
- MongoDB / DynamoDB / other NoSQL infrastructure;
- any public Notebook API changes: paths, request/response fields, status
  codes;
- `/cells` endpoints;
- SQLAlchemy `relationship()` between `User` and `Notebook`;
- physical data migration to another database;
- `DROP SCHEMA app` (follow-up only).

## 10. Acceptance Criteria Traceability

| Ticket AC | Implemented in |
|---|---|
| Domain-boundaries spec exists | this file (`api/docs/domain-boundaries.md`) |
| Liquibase reflects `users` and `notebooks` schemas | `changes/users/`, `changes/notebooks/` |
| `0002-users-notebooks.xml` is append-only | not edited; new changesets are added on top |
| `User.__table_args__ = {"schema": "users"}` | `app/modules/auth/models/user.py` |
| `Notebook.__table_args__ = {"schema": "notebooks"}` | `app/modules/notebooks/models/notebook.py` |
| `Notebook.owner_id` has no DB FK | `app/modules/notebooks/models/notebook.py` (no `ForeignKey`) |
| `NotebookService` is typed with `NotebookRepositoryProtocol` | `app/modules/notebooks/services/notebook_service.py` |
| Controllers do not import SQLAlchemy | `app/modules/notebooks/dependencies.py` encapsulates DI |
| Public OpenAPI does not change | OpenAPI drift check in CI |
| `ruff check .` / `pytest` are green | CI run |
