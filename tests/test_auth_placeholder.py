from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import settings


def test_get_me_returns_dev_user_without_header(client: TestClient) -> None:
    response = client.get(f"{settings.api_prefix}/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "id": "00000000-0000-0000-0000-000000000001",
        "email": "dev@notebook.local",
        "displayName": "Dev User",
        "roles": [],
    }


def test_get_me_uses_x_user_id_header(client: TestClient) -> None:
    user_id = uuid4()

    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user_id)
    assert response.json()["email"] == f"{user_id}@dev.notebook.local"


def test_get_me_rejects_invalid_x_user_id(client: TestClient) -> None:
    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"X-User-Id": "bad"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
