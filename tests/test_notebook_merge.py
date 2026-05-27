from app.modules.notebooks.services.notebook_merge import merge_cells


def test_merge_adds_client_only_cell() -> None:
    result = merge_cells(
        [],
        [{"id": "cell-1", "kind": "code", "content": "new", "updatedAt": 1000}],
        [],
    )

    assert [cell["id"] for cell in result] == ["cell-1"]


def test_merge_preserves_server_only_cell() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "server", "updatedAt": 1000}],
        [],
        [],
    )

    assert result[0]["content"] == "server"


def test_merge_uses_newer_client_cell() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "old", "updatedAt": 1000}],
        [{"id": "cell-1", "kind": "code", "content": "new", "updatedAt": 2000}],
        [],
    )

    assert result[0]["content"] == "new"


def test_merge_uses_newer_server_cell() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "server", "updatedAt": 3000}],
        [{"id": "cell-1", "kind": "code", "content": "client", "updatedAt": 2000}],
        [],
    )

    assert result[0]["content"] == "server"


def test_merge_delete_wins_when_tombstone_is_newer() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "server", "updatedAt": 1000}],
        [],
        [{"id": "cell-1", "deletedAt": 2000}],
    )

    assert result == []


def test_merge_edit_wins_when_server_is_newer_than_tombstone() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "server", "updatedAt": 3000}],
        [],
        [{"id": "cell-1", "deletedAt": 2000}],
    )

    assert result[0]["content"] == "server"


def test_merge_preserves_client_order_and_appends_server_only() -> None:
    result = merge_cells(
        [{"id": "cell-3", "kind": "code", "content": "server", "updatedAt": 1000}],
        [
            {"id": "cell-2", "kind": "code", "content": "client 2", "updatedAt": 1000},
            {"id": "cell-1", "kind": "code", "content": "client 1", "updatedAt": 1000},
        ],
        [],
    )

    assert [cell["id"] for cell in result] == ["cell-2", "cell-1", "cell-3"]


def test_merge_equal_timestamps_server_wins() -> None:
    result = merge_cells(
        [{"id": "cell-1", "kind": "code", "content": "server", "updatedAt": 2000}],
        [{"id": "cell-1", "kind": "code", "content": "client", "updatedAt": 2000}],
        [],
    )

    assert result[0]["content"] == "server"
