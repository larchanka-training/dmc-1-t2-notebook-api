from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.db import get_db
from app.main import app


def _override_db_ok() -> Generator[MagicMock, None, None]:
    session = MagicMock()
    session.execute.return_value = None
    yield session


def _override_db_fail() -> Generator[MagicMock, None, None]:
    session = MagicMock()
    session.execute.side_effect = OperationalError("SELECT 1", {}, Exception("no db"))
    yield session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def test_app_boots_and_routes_registered(client: TestClient) -> None:
    paths = {route.path for route in app.routes}
    assert "/" in paths
    assert f"{settings.api_prefix}/health" in paths
    assert f"{settings.api_prefix}/health/ready" in paths


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to MSD FastAPI Template"}


def test_openapi_schema_available(client: TestClient) -> None:
    # Docs/schema live under the API prefix so they survive the CloudFront/ALB
    # proxy, which forwards only `{api_prefix}/*` to the API.
    response = client.get(f"{settings.api_prefix}/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == settings.app_name
    assert schema["info"]["version"] == settings.app_version


def test_docs_served_under_prefix_not_root(client: TestClient) -> None:
    assert client.get(f"{settings.api_prefix}/docs").status_code == 200
    assert client.get(f"{settings.api_prefix}/redoc").status_code == 200
    # Root paths must not serve docs/schema — those would reach the SPA on S3.
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_readiness_ok_with_mocked_db(client: TestClient) -> None:
    app.dependency_overrides[get_db] = _override_db_ok
    try:
        response = client.get(f"{settings.api_prefix}/health/ready")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert any(c["name"] == "database" and c["status"] == "ok" for c in payload["components"])
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_readiness_degraded_when_db_fails(client: TestClient) -> None:
    app.dependency_overrides[get_db] = _override_db_fail
    try:
        response = client.get(f"{settings.api_prefix}/health/ready")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "degraded"
        assert any(c["name"] == "database" and c["status"] == "fail" for c in payload["components"])
    finally:
        app.dependency_overrides.pop(get_db, None)
