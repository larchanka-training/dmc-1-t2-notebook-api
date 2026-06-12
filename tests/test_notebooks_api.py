import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.core.config import settings
from app.modules.notebooks.demo import demo_id
from app.modules.notebooks.services.notebook_service import MAX_FUTURE_SKEW_MS


def _login(
    client: TestClient, email: str = "owner@example.com"
) -> tuple[dict[str, str], str, str]:
    """Run the OTP flow and return ``(bearer_headers, user_id, refresh_token)``.

    The notebook endpoints now require a Bearer JWT access token (TARDIS-75
    cutover). This helper performs ``otp/request`` + ``otp/verify`` against
    the test app so each test gets a real authenticated session, identical
    to what the frontend will issue at runtime. The refresh token is
    returned so tests can also exercise the logout/revoked-session path.
    """
    otp = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": email},
    ).json()["otp"]
    body = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    ).json()
    return (
        {"Authorization": f"Bearer {body['accessToken']}"},
        body["user"]["id"],
        body["refreshToken"],
    )


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
    headers, user_id, _ = _login(client)

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(),
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert UUID(payload["id"])
    assert payload["title"] == "Smoke notebook"
    assert payload["ownerId"] == user_id
    assert payload["formatVersion"] == 1
    assert payload["cells"][0]["kind"] == "code"
    assert payload["cells"][0]["updatedAt"] == 1779367200000


def test_top_level_updated_at_is_capped_by_server_time(client: TestClient) -> None:
    headers, _, _ = _login(client)
    future_ms = 9_999_999_999_999
    payload = _payload()
    payload["cells"][0]["updatedAt"] = future_ms

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["updatedAt"] < future_ms
    assert response.json()["updatedAt"] <= int(time.time() * 1000) + MAX_FUTURE_SKEW_MS
    assert response.json()["cells"][0]["updatedAt"] == future_ms


def test_create_notebook_with_client_id_is_idempotent(client: TestClient) -> None:
    headers, _, _ = _login(client)
    notebook_id = str(uuid4())

    first = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=headers,
    )
    second = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == notebook_id


def test_create_existing_id_with_different_payload_returns_conflict(
    client: TestClient,
) -> None:
    headers, _, _ = _login(client)
    notebook_id = str(uuid4())
    first_payload = _payload(notebook_id)
    second_payload = _payload(notebook_id)
    second_payload["title"] = "Different title"

    first = client.post(
        f"{settings.api_prefix}/notebooks",
        json=first_payload,
        headers=headers,
    )
    second = client.post(
        f"{settings.api_prefix}/notebooks",
        json=second_payload,
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "NOTEBOOK_CONFLICT"


def test_create_existing_id_for_another_owner_returns_forbidden(
    client: TestClient,
) -> None:
    alice_headers, _, _ = _login(client, "alice@example.com")
    bob_headers, _, _ = _login(client, "bob@example.com")
    notebook_id = str(uuid4())

    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=alice_headers,
    )

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=bob_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_create_owner_id_matches_authenticated_user(client: TestClient) -> None:
    """OTP login materializes a User row; notebook.ownerId == that user's id."""
    headers, user_id, _ = _login(client, "fresh-user@example.com")

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(str(uuid4())),
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["ownerId"] == user_id


def test_list_notebooks_is_owner_scoped_and_paginated(client: TestClient) -> None:
    alice_headers, _, _ = _login(client, "alice@example.com")
    bob_headers, _, _ = _login(client, "bob@example.com")
    own_id = str(uuid4())
    other_id = str(uuid4())

    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(own_id),
        headers=alice_headers,
    )
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(other_id, str(uuid4())),
        headers=bob_headers,
    )

    response = client.get(
        f"{settings.api_prefix}/notebooks?limit=50&offset=0",
        headers=alice_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 50
    assert payload["offset"] == 0
    assert payload["items"][0]["id"] == own_id
    assert payload["items"][0]["cellsCount"] == 1


def test_invalid_sort_returns_error_envelope(client: TestClient) -> None:
    headers, _, _ = _login(client)

    response = client.get(
        f"{settings.api_prefix}/notebooks?sort=bad",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_get_notebook_owner_checks_and_missing(client: TestClient) -> None:
    alice_headers, _, _ = _login(client, "alice@example.com")
    bob_headers, _, _ = _login(client, "bob@example.com")
    notebook_id = str(uuid4())
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=alice_headers,
    )

    own = client.get(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        headers=alice_headers,
    )
    other = client.get(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        headers=bob_headers,
    )
    missing = client.get(
        f"{settings.api_prefix}/notebooks/{uuid4()}",
        headers=alice_headers,
    )

    assert own.status_code == 200
    assert own.json()["id"] == notebook_id
    assert other.status_code == 403
    assert other.json()["error"]["code"] == "FORBIDDEN"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOTEBOOK_NOT_FOUND"


def test_patch_notebook_merges_cells_and_deleted_cells(client: TestClient) -> None:
    headers, _, _ = _login(client)
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
        headers=headers,
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
        headers=headers,
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
    headers, _, _ = _login(client)
    notebook_id = str(uuid4())
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=headers,
    )

    deleted = client.delete(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        headers=headers,
    )
    fetched = client.get(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        headers=headers,
    )
    listed = client.get(f"{settings.api_prefix}/notebooks", headers=headers)

    assert deleted.status_code == 204
    assert fetched.status_code == 404
    assert listed.json()["total"] == 0


def test_invalid_cell_kind_uses_error_envelope(client: TestClient) -> None:
    headers, _, _ = _login(client)
    payload = _payload()
    payload["cells"][0]["kind"] = "text"

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "cells[0].kind" in body["error"]["fields"]


def test_create_rejects_too_many_cells(client: TestClient) -> None:
    headers, _, _ = _login(client)
    payload = {
        "title": "huge",
        "formatVersion": 1,
        "cells": [
            {"id": str(uuid4()), "kind": "code", "content": "", "updatedAt": 1}
            for _ in range(501)
        ],
    }
    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 422


def test_create_rejects_duplicate_cell_ids(client: TestClient) -> None:
    headers, _, _ = _login(client)
    cell_id = str(uuid4())
    payload = {
        "title": "duplicate cells",
        "formatVersion": 1,
        "cells": [
            {
                "id": cell_id,
                "kind": "code",
                "content": "first",
                "updatedAt": 1000,
            },
            {
                "id": cell_id,
                "kind": "markdown",
                "content": "second",
                "updatedAt": 2000,
            },
        ],
    }

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_rejects_duplicate_deleted_cell_ids(client: TestClient) -> None:
    headers, _, _ = _login(client)
    notebook_id = str(uuid4())
    cell_id = str(uuid4())

    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id, cell_id),
        headers=headers,
    )

    response = client.patch(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        json={
            "title": "duplicate tombstones",
            "formatVersion": 1,
            "cells": [],
            "deletedCells": [
                {"id": cell_id, "deletedAt": 1000},
                {"id": cell_id, "deletedAt": 2000},
            ],
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_rejects_title_too_long(client: TestClient) -> None:
    headers, _, _ = _login(client)
    payload = _payload()
    payload["title"] = "x" * 256

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_rejects_title_too_long(client: TestClient) -> None:
    headers, _, _ = _login(client)
    notebook_id = str(uuid4())
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(notebook_id),
        headers=headers,
    )

    payload = _payload()
    payload["title"] = "x" * 256

    response = client.patch(
        f"{settings.api_prefix}/notebooks/{notebook_id}",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_rejects_format_version_below_one(client: TestClient) -> None:
    headers, _, _ = _login(client)
    payload = _payload()
    payload["formatVersion"] = 0

    response = client.post(
        f"{settings.api_prefix}/notebooks",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Bearer / session negative cases (TARDIS-75 cutover)
# ---------------------------------------------------------------------------


def test_create_without_bearer_returns_401(client: TestClient) -> None:
    response = client.post(f"{settings.api_prefix}/notebooks", json=_payload())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_list_with_malformed_bearer_returns_401(client: TestClient) -> None:
    response = client.get(
        f"{settings.api_prefix}/notebooks",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_x_user_id_header_alone_is_no_longer_accepted(client: TestClient) -> None:
    """The placeholder X-User-Id shortcut no longer authorizes notebook calls."""
    response = client.get(
        f"{settings.api_prefix}/notebooks",
        headers={"X-User-Id": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_list_after_logout_returns_401(client: TestClient) -> None:
    """Logout revokes the session; the still-unexpired access token must fail."""
    headers, _, refresh_token = _login(client, "alice@example.com")

    # Sanity: token works before logout.
    pre = client.get(f"{settings.api_prefix}/notebooks", headers=headers)
    assert pre.status_code == 200

    logout = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": refresh_token},
    )
    assert logout.status_code == 204

    response = client.get(f"{settings.api_prefix}/notebooks", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# Feature-demo restore (TARDIS-61)
# ---------------------------------------------------------------------------

_RESTORE_PATH = "/notebooks/features-demo/restore"


def test_restore_features_demo_resurrects_soft_deleted(client: TestClient) -> None:
    headers, user_id, _ = _login(client)
    nb_id = str(demo_id(UUID(user_id)))
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(nb_id),
        headers=headers,
    )
    deleted = client.delete(
        f"{settings.api_prefix}/notebooks/{nb_id}",
        headers=headers,
    )
    assert deleted.status_code == 204

    restored = client.post(
        f"{settings.api_prefix}{_RESTORE_PATH}",
        headers=headers,
    )

    assert restored.status_code == 200
    body = restored.json()
    assert body["id"] == nb_id
    assert body["cells"][0]["content"] == "console.log(1)"

    # Visible again afterwards.
    fetched = client.get(
        f"{settings.api_prefix}/notebooks/{nb_id}",
        headers=headers,
    )
    assert fetched.status_code == 200


def test_restore_features_demo_is_idempotent_when_active(client: TestClient) -> None:
    headers, user_id, _ = _login(client)
    nb_id = str(demo_id(UUID(user_id)))
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(nb_id),
        headers=headers,
    )

    first = client.post(f"{settings.api_prefix}{_RESTORE_PATH}", headers=headers)
    second = client.post(f"{settings.api_prefix}{_RESTORE_PATH}", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == nb_id
    listed = client.get(f"{settings.api_prefix}/notebooks", headers=headers)
    assert listed.json()["total"] == 1


def test_restore_features_demo_404_when_never_created(client: TestClient) -> None:
    headers, _, _ = _login(client, "no-demo@example.com")

    response = client.post(f"{settings.api_prefix}{_RESTORE_PATH}", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOTEBOOK_NOT_FOUND"


def test_restore_features_demo_is_owner_isolated(client: TestClient) -> None:
    alice_headers, alice_id, _ = _login(client, "alice@example.com")
    bob_headers, _, _ = _login(client, "bob@example.com")
    alice_demo = str(demo_id(UUID(alice_id)))
    client.post(
        f"{settings.api_prefix}/notebooks",
        json=_payload(alice_demo),
        headers=alice_headers,
    )
    client.delete(
        f"{settings.api_prefix}/notebooks/{alice_demo}",
        headers=alice_headers,
    )

    # Bob's restore only ever targets his own (absent) demo → 404.
    bob_restore = client.post(
        f"{settings.api_prefix}{_RESTORE_PATH}",
        headers=bob_headers,
    )
    assert bob_restore.status_code == 404

    # Alice can still restore hers.
    alice_restore = client.post(
        f"{settings.api_prefix}{_RESTORE_PATH}",
        headers=alice_headers,
    )
    assert alice_restore.status_code == 200
    assert alice_restore.json()["id"] == alice_demo


def test_restore_features_demo_requires_bearer(client: TestClient) -> None:
    response = client.post(f"{settings.api_prefix}{_RESTORE_PATH}")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
