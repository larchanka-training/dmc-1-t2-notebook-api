import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.core.config import settings
from app.modules.notebooks.services.notebook_service import MAX_FUTURE_SKEW_MS


def _payload(notebook_id: str | None = None, cell_id: str | None = None) -> dict:
    payload = {
        "title": "Smoke notebook",
        "formatVersion": 1,
        "cells": [
            {
                "id": cell_id or "22222222-2222-2222-2222-222222222222",
                "kind": "code",
                "content": "console.log(1)",
                "updatedAt": 1779367200000,
            }
        ],
    }
    if notebook_id:
        payload["id"] = notebook_id
    return payload


def test_create_notebook_without_id(client: TestClient) -> None:
    response = client.post(f"{settings.api_prefix}/notebooks", json=_payload())

    assert response.status_code == 201
    payload = response.json()
    assert UUID(payload["id"])
    assert payload["title"] == "Smoke notebook"
    assert payload["ownerId"] == "00000000-0000-0000-0000-000000000001"
    assert payload["formatVersion"] == 1
    assert payload["cells"][0]["kind"] == "code"
    assert payload["cells"][0]["updatedAt"] == 1779367200000


def test_top_level_updated_at_is_capped_by_server_time(client: TestClient) -> None:
    future_ms = 9_999_999_999_999
    payload = _payload()
    payload["cells"][0]["updatedAt"] = future_ms

    response = client.post(f"{settings.api_prefix}/notebooks", json=payload)

    assert response.status_code == 201
    assert response.json()["updatedAt"] < future_ms
    assert response.json()["updatedAt"] <= int(time.time() * 1000) + MAX_FUTURE_SKEW_MS
    assert response.json()["cells"][0]["updatedAt"] == future_ms


def test_create_notebook_with_client_id_is_idempotent(client: TestClient) -> None:
    notebook_id = str(uuid4())

    first = client.post(f"{settings.api_prefix}/notebooks", json=_payload(notebook_id))
    second = client.post(f"{settings.api_prefix}/notebooks", json=_payload(notebook_id))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == notebook_id


def test_create_existing_id_for_another_owner_returns_forbidden(client: TestClient) -> None:
    notebook_id = str(uuid4())
    client.post(f"{settings.api_prefix}/notebooks", json=_payload(notebook_id))

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers={"X-User-Id": str(uuid4())},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_create_with_new_x_user_id_creates_placeholder_owner(client: TestClient) -> None:
    user_id = str(uuid4())

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(str(uuid4())),
        headers={"X-User-Id": user_id},
    )

    assert response.status_code == 201
    assert response.json()["ownerId"] == user_id


def test_list_notebooks_is_owner_scoped_and_paginated(client: TestClient) -> None:
    own_id = str(uuid4())
    other_id = str(uuid4())
    other_owner = str(uuid4())
    client.post(f"{settings.api_prefix}/notebooks", json=_payload(own_id))
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(other_id, str(uuid4())),
        headers={"X-User-Id": other_owner},
    )

    response = client.get(f"{settings.api_prefix}/notebooks?limit=50&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 50
    assert payload["offset"] == 0
    assert payload["items"][0]["id"] == own_id
    assert payload["items"][0]["cellsCount"] == 1


def test_invalid_sort_returns_error_envelope(client: TestClient) -> None:
    response = client.get(f"{settings.api_prefix}/notebooks?sort=bad")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_get_notebook_owner_checks_and_missing(client: TestClient) -> None:
    notebook_id = str(uuid4())
    client.post(f"{settings.api_prefix}/notebooks", json=_payload(notebook_id))

    own = client.get(f"{settings.api_prefix}/notebooks/{notebook_id}")
    other = client.get(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        headers={"X-User-Id": str(uuid4())},
    )
    missing = client.get(f"{settings.api_prefix}/notebooks/{uuid4()}")

    assert own.status_code == 200
    assert own.json()["id"] == notebook_id
    assert other.status_code == 403
    assert other.json()["error"]["code"] == "FORBIDDEN"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOTEBOOK_NOT_FOUND"


def test_patch_notebook_merges_cells_and_deleted_cells(client: TestClient) -> None:
    notebook_id = str(uuid4())
    keep_id = "11111111-1111-1111-1111-111111111111"
    delete_id = "22222222-2222-2222-2222-222222222222"
    client.post(
        f"{settings.api_prefix}/notebooks",
        json={
            "id": notebook_id,
            "title": "Before",
            "formatVersion": 1,
            "cells": [
                {
                    "id": keep_id,
                    "kind": "code",
                    "content": "old",
                    "updatedAt": 1000,
                },
                {
                    "id": delete_id,
                    "kind": "markdown",
                    "content": "delete me",
                    "updatedAt": 1000,
                },
            ],
        },
    )

    response = client.patch(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        json={
            "title": "After",
            "formatVersion": 1,
            "cells": [
                {
                    "id": keep_id,
                    "kind": "code",
                    "content": "new",
                    "updatedAt": 2000,
                }
            ],
            "deletedCells": [{"id": delete_id, "deletedAt": 3000}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "After"
    assert payload["cells"] == [
        {
            "id": keep_id,
            "kind": "code",
            "content": "new",
            "updatedAt": 2000,
        }
    ]


def test_delete_soft_deletes_notebook(client: TestClient) -> None:
    notebook_id = str(uuid4())
    client.post(f"{settings.api_prefix}/notebooks", json=_payload(notebook_id))

    deleted = client.delete(f"{settings.api_prefix}/notebooks/{notebook_id}")
    fetched = client.get(f"{settings.api_prefix}/notebooks/{notebook_id}")
    listed = client.get(f"{settings.api_prefix}/notebooks")

    assert deleted.status_code == 204
    assert fetched.status_code == 404
    assert listed.json()["total"] == 0


def test_invalid_cell_kind_uses_error_envelope(client: TestClient) -> None:
    payload = _payload()
    payload["cells"][0]["kind"] = "text"

    response = client.post(f"{settings.api_prefix}/notebooks", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "cells[0].kind" in body["error"]["fields"]
