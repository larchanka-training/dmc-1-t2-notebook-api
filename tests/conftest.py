import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4
import xml.etree.ElementTree as ET

import psycopg2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app
from app.modules.ai_context.models import NotebookAiContext
from app.modules.auth.models import User
from app.modules.auth.models.user import User as UserModel
from app.modules.llm.models import (
    LlmEntitlement,
    LlmUsageCounter,
    LlmUsageEvent,
    LlmUsageReservation,
)
from app.modules.notebooks.models import Notebook

_ = (
    User,
    Notebook,
    NotebookAiContext,
    LlmEntitlement,
    LlmUsageCounter,
    LlmUsageEvent,
    LlmUsageReservation,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def attach_domain_schemas(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        # SQLite не знает PostgreSQL-schemas. Эмулируем их через
        # ATTACH DATABASE: каждое имя становится псевдо-schema, и
        # SQLAlchemy выдаёт DDL вида `CREATE TABLE users.users (...)`
        # против присоединённой базы. Имена должны совпадать с
        # __table_args__={"schema": ...} в ORM-моделях.
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS users")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS notebooks")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    session = factory()
    session.add(
        UserModel(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            email="dev@notebook.local",
            display_name="Dev User",
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session_factory(db_session: Session) -> sessionmaker[Session]:
    bind = db_session.get_bind()
    return sessionmaker(
        bind=bind,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


MIGRATION_0007_SQL = """
CREATE TABLE IF NOT EXISTS users.llm_usage_event (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users.users(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    reservation_id uuid NOT NULL,
    call_kind text NOT NULL CHECK (call_kind IN ('guard', 'generator', 'repair')),
    provider text NOT NULL CHECK (provider IN ('openrouter', 'bedrock')),
    model_id text,
    status text NOT NULL CHECK (status IN ('ok', 'provider_error', 'timeout', 'unknown')),
    prompt_tokens integer NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens integer NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    estimated_cost_micros bigint NOT NULL DEFAULT 0 CHECK (estimated_cost_micros >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_usage_event_user_created_idx
    ON users.llm_usage_event(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS llm_usage_event_request_idx
    ON users.llm_usage_event(request_id);

CREATE INDEX IF NOT EXISTS llm_usage_event_reservation_idx
    ON users.llm_usage_event(reservation_id);

CREATE TABLE IF NOT EXISTS users.llm_usage_reservation (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users.users(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    call_kind text NOT NULL CHECK (call_kind IN ('guard', 'generator', 'repair')),
    provider text NOT NULL CHECK (provider IN ('openrouter', 'bedrock')),
    state text NOT NULL CHECK (state IN ('reserved', 'started', 'settled', 'released', 'unknown')),
    cost_reserved_micros bigint NOT NULL CHECK (cost_reserved_micros >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    closed_at timestamptz
);

CREATE INDEX IF NOT EXISTS llm_usage_reservation_state_created_idx
    ON users.llm_usage_reservation(state, created_at);

CREATE INDEX IF NOT EXISTS llm_usage_reservation_request_idx
    ON users.llm_usage_reservation(request_id);

CREATE INDEX IF NOT EXISTS llm_usage_reservation_user_idx
    ON users.llm_usage_reservation(user_id);

CREATE TABLE IF NOT EXISTS users.llm_usage_counter (
    scope text NOT NULL CHECK (scope IN ('user', 'global')),
    scope_key text NOT NULL,
    window_kind text NOT NULL CHECK (window_kind IN ('day', 'month')),
    window_start date NOT NULL,
    calls_reserved integer NOT NULL DEFAULT 0 CHECK (calls_reserved >= 0),
    calls_settled integer NOT NULL DEFAULT 0 CHECK (calls_settled >= 0),
    cost_reserved_micros bigint NOT NULL DEFAULT 0 CHECK (cost_reserved_micros >= 0),
    cost_micros bigint NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    PRIMARY KEY (scope, scope_key, window_kind, window_start)
);

CREATE TABLE IF NOT EXISTS users.llm_entitlement (
    user_id uuid PRIMARY KEY REFERENCES users.users(id) ON DELETE CASCADE,
    tier text NOT NULL CHECK (tier IN ('free', 'developer', 'paid')),
    daily_call_limit integer CHECK (daily_call_limit IS NULL OR daily_call_limit >= 0),
    monthly_call_limit integer CHECK (monthly_call_limit IS NULL OR monthly_call_limit >= 0),
    valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


def load_migration_0007_sql() -> str:
    """Load SQL from Liquibase changeset 0007-llm-usage-controls.xml if available."""
    xml_path = Path(__file__).parent.parent / "liquibase" / "changelog" / "changes" / "users" / "0007-llm-usage-controls.xml"
    if xml_path.exists():
        try:
            tree = ET.parse(xml_path)
            for elem in tree.iter():
                if elem.tag.endswith("sql") and "CREATE TABLE" in (elem.text or ""):
                    return elem.text.strip()
        except Exception:
            pass
    return MIGRATION_0007_SQL.strip()


def _resolve_postgres_cluster_url() -> str | None:
    """Resolve PostgreSQL cluster URL exclusively from explicit TEST_DATABASE_URL.

    F6 Invariant:
    DATABASE_URL is explicitly NEVER used for test discovery or test database provisioning.
    Guessed local addresses (e.g. localhost:5432) are NEVER probed without explicit opt-in.
    """
    env_url = os.environ.get("TEST_DATABASE_URL")
    if env_url and ("postgres" in env_url):
        return env_url
    return None


@pytest.fixture(scope="session")
def postgres_cluster_url() -> Generator[str | None, None, None]:
    """Provide a running PostgreSQL cluster URL, strictly from TEST_DATABASE_URL or ephemeral cluster.

    Safety:
    1. If TEST_DATABASE_URL is set, use it.
    2. In CI, if TEST_DATABASE_URL is missing, raise RuntimeError immediately.
    3. In local dev without TEST_DATABASE_URL, spin up an isolated ephemeral cluster on a random port
       if initdb and pg_ctl are present, verifying clean shutdown before directory cleanup.
    4. Otherwise yield None (tests will skip explicitly with a clear message).
    """
    url = _resolve_postgres_cluster_url()
    if url:
        yield url
        return

    # In CI, PostgreSQL is strictly mandatory for the test suite
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError(
            "TEST_DATABASE_URL environment variable is required in CI environment"
        )

    # Local developer fallback: ephemeral cluster on random port
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if initdb and pg_ctl:
        pg_dir = tempfile.mkdtemp(prefix="pg_jsnb_", dir="/tmp")
        log_file = os.path.join(pg_dir, "server.log")
        s = socket.socket()
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()

        try:
            subprocess.run(
                [initdb, "-D", pg_dir, "-U", "postgres", "-A", "trust", "--no-instructions"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [pg_ctl, "-D", pg_dir, "-l", log_file, "-o", f"-k /tmp -p {port} -h 127.0.0.1", "start"],
                check=True,
                capture_output=True,
            )
            time.sleep(0.3)
            yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
        finally:
            # S3: Verify successful stop before deleting directory
            stop_res = subprocess.run([pg_ctl, "-D", pg_dir, "-m", "immediate", "stop"], capture_output=True)
            if stop_res.returncode == 0:
                shutil.rmtree(pg_dir, ignore_errors=True)
        return

    yield None


@pytest.fixture
def pg_session_factory(postgres_cluster_url: str | None) -> Generator[sessionmaker[Session], None, None]:
    """Provision an isolated disposable database for PostgreSQL tests with real constraints."""
    if not postgres_cluster_url:
        pytest.skip("PostgreSQL is not configured (set TEST_DATABASE_URL to run PostgreSQL tests)")

    disposable_db = f"jsnb_test_{uuid4().hex[:10]}"
    admin_conn = psycopg2.connect(postgres_cluster_url)
    admin_conn.autocommit = True
    cur = admin_conn.cursor()
    cur.execute(f'CREATE DATABASE "{disposable_db}";')
    cur.close()
    admin_conn.close()

    parsed = urlparse(postgres_cluster_url)
    db_url = urlunparse(parsed._replace(path=f"/{disposable_db}"))
    engine = None

    # S3: Register cleanup immediately after database creation in try/finally
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        c = conn.cursor()
        c.execute("CREATE SCHEMA IF NOT EXISTS users;")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users.users (
                id uuid PRIMARY KEY,
                email text NOT NULL,
                display_name text,
                created_at timestamptz NOT NULL DEFAULT now()
            );
        """)
        c.execute(load_migration_0007_sql())
        c.close()
        conn.close()

        engine = create_engine(
            db_url.replace("postgresql://", "postgresql+psycopg2://"),
            pool_size=10,
            max_overflow=20,
            future=True,
        )
        factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        setattr(factory, "db_url", db_url)
        yield factory
    finally:
        if engine is not None:
            engine.dispose()
        try:
            admin_conn = psycopg2.connect(postgres_cluster_url)
            admin_conn.autocommit = True
            cur = admin_conn.cursor()
            cur.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = '{disposable_db}' AND pid <> pg_backend_pid();
            """)
            cur.execute(f'DROP DATABASE IF EXISTS "{disposable_db}";')
            cur.close()
            admin_conn.close()
        except Exception:
            pass
