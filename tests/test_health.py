from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def test_liveness_returns_ok(client: TestClient) -> None:
    response = client.get(f"{settings.api_prefix}/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app"] == settings.app_name
    assert payload["version"] == settings.app_version
    assert payload["environment"] == settings.app_env
    assert payload["components"] == []
