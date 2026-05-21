from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.modules.auth.models  # noqa: F401 — registers auth tables on Base.metadata
from app.core.config import settings
from app.core.db import Base, get_db
from app.main import app

PREFIX = settings.api_prefix


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    ).execution_options(schema_translate_map={"app": None})
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(engine)


def _register(client: TestClient, email: str = "user@example.com", password: str = "password123") -> None:
    response = client.post(f"{PREFIX}/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201


def _login(client: TestClient, email: str = "user@example.com", password: str = "password123") -> dict:
    response = client.post(f"{PREFIX}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()


def test_register_returns_user(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/auth/register",
        json={"email": "new@example.com", "password": "password123"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert "id" in body
    assert "password" not in body and "password_hash" not in body


def test_register_duplicate_email_conflict(client: TestClient) -> None:
    _register(client)
    response = client.post(
        f"{PREFIX}/auth/register",
        json={"email": "user@example.com", "password": "password123"},
    )
    assert response.status_code == 409


def test_register_short_password_rejected(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/auth/register",
        json={"email": "short@example.com", "password": "short"},
    )
    assert response.status_code == 422


def test_login_returns_tokens(client: TestClient) -> None:
    _register(client)
    tokens = _login(client)
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0


def test_login_wrong_password(client: TestClient) -> None:
    _register(client)
    response = client.post(
        f"{PREFIX}/auth/login",
        json={"email": "user@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_login_unknown_email(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/auth/login",
        json={"email": "ghost@example.com", "password": "password123"},
    )
    assert response.status_code == 401


def test_me_requires_token(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/auth/me")
    assert response.status_code == 401


def test_me_with_token(client: TestClient) -> None:
    _register(client)
    tokens = _login(client)
    response = client.get(
        f"{PREFIX}/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 200
    assert response.json()["email"] == "user@example.com"


def test_me_rejects_invalid_token(client: TestClient) -> None:
    response = client.get(
        f"{PREFIX}/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_refresh_rotates_token(client: TestClient) -> None:
    _register(client)
    tokens = _login(client)
    refreshed = client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    new_tokens = refreshed.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    reused = client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert reused.status_code == 401


def test_logout_revokes_refresh_token(client: TestClient) -> None:
    _register(client)
    tokens = _login(client)
    logout = client.post(
        f"{PREFIX}/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    response = client.post(
        f"{PREFIX}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401
