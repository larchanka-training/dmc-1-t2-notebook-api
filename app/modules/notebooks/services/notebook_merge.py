from collections.abc import Iterable
from typing import Any


def _normalize_cell(cell: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(cell)
    normalized["id"] = str(normalized["id"])
    return normalized


def merge_cells(
    server_cells: Iterable[dict[str, Any]],
    client_cells: Iterable[dict[str, Any]],
    deleted_cells: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    server_items = [_normalize_cell(cell) for cell in server_cells]
    client_items = [_normalize_cell(cell) for cell in client_cells]
    deleted_by_id = {
        str(item["id"]): int(item["deletedAt"])
        for item in deleted_cells
    }

    server_by_id = {cell["id"]: cell for cell in server_items}
    client_by_id = {cell["id"]: cell for cell in client_items}
    merged_by_id: dict[str, dict[str, Any]] = {}

    for cell_id in set(server_by_id) | set(client_by_id):
        server_cell = server_by_id.get(cell_id)
        client_cell = client_by_id.get(cell_id)
        deleted_at = deleted_by_id.get(cell_id)

        if deleted_at is not None:
            if server_cell and int(server_cell["updatedAt"]) > deleted_at:
                merged_by_id[cell_id] = server_cell
            continue

        if server_cell is None and client_cell is not None:
            merged_by_id[cell_id] = client_cell
        elif client_cell is None and server_cell is not None:
            merged_by_id[cell_id] = server_cell
        elif server_cell and client_cell:
            merged_by_id[cell_id] = (
                client_cell
                if int(client_cell["updatedAt"]) >= int(server_cell["updatedAt"])
                else server_cell
            )

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    for cell in client_items:
        cell_id = cell["id"]
        if cell_id in merged_by_id:
            ordered.append(merged_by_id[cell_id])
            seen.add(cell_id)

    for cell in server_items:
        cell_id = cell["id"]
        if cell_id in merged_by_id and cell_id not in seen:
            ordered.append(merged_by_id[cell_id])

    return ordered
