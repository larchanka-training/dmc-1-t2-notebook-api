from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.db import Base, get_db
from app.main import app
from app.modules.auth.models import User
from app.modules.auth.models.user import User as UserModel
from app.modules.notebooks.models import Notebook

_ = (User, Notebook)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def attach_app_schema(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS app")
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
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
