"""Service-level tests for ``NotebookService.restore_features_demo``.

Exercise the restore use-case directly against the real repository over the
SQLite ``db_session`` fixture (no HTTP layer). The matching controller/HTTP
tests live in ``test_notebooks_api.py``.
"""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.notebooks.demo import demo_id
from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.schemas.notebook_schemas import (
    CellSchema,
    NotebookCreate,
    NotebookResponse,
)
from app.modules.notebooks.services.notebook_service import NotebookService


def _service(db_session: Session) -> NotebookService:
    return NotebookService(NotebookRepository(db_session))


def _user(owner_id: UUID | None = None) -> CurrentUser:
    return CurrentUser(id=owner_id or uuid4())


def _seed_demo(service: NotebookService, user: CurrentUser) -> NotebookResponse:
    """Create the user's feature-demo notebook at its canonical id."""
    payload = NotebookCreate(
        id=demo_id(user.id),
        title="Feature demo",
        cells=[
            CellSchema(
                id=UUID("44444444-4444-4444-4444-444444444444"),
                kind="code",
                content="console.log('demo')",
                updated_at=1000,
            )
        ],
    )
    notebook, _ = service.create(user, payload)
    return notebook


def test_restore_resurrects_soft_deleted_demo_preserving_cells(
    db_session: Session,
) -> None:
    service = _service(db_session)
    user = _user()
    created = _seed_demo(service, user)
    service.delete(user, demo_id(user.id))

    restored = service.restore_features_demo(user)

    assert restored.id == demo_id(user.id)
    assert restored.cells == created.cells  # прежние ячейки сохранены


def test_restore_is_idempotent_for_active_demo(db_session: Session) -> None:
    service = _service(db_session)
    user = _user()
    _seed_demo(service, user)

    first = service.restore_features_demo(user)
    second = service.restore_features_demo(user)

    assert first.id == second.id == demo_id(user.id)
    _, total = NotebookRepository(db_session).list_by_owner(
        user.id, 50, 0, "updatedAt", "desc"
    )
    assert total == 1  # без дублей


def test_restore_404_when_demo_never_created(db_session: Session) -> None:
    service = _service(db_session)

    with pytest.raises(HTTPException) as exc:
        service.restore_features_demo(_user())

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "NOTEBOOK_NOT_FOUND"


def test_restore_is_owner_isolated(db_session: Session) -> None:
    service = _service(db_session)
    alice = _user()
    bob = _user()
    _seed_demo(service, alice)
    service.delete(alice, demo_id(alice.id))

    # Bob has no demo of his own → 404, never touches Alice's.
    with pytest.raises(HTTPException) as exc:
        service.restore_features_demo(bob)
    assert exc.value.status_code == 404

    # Alice still restores hers.
    assert service.restore_features_demo(alice).id == demo_id(alice.id)


def test_restore_ignores_foreign_owned_record_at_demo_id(db_session: Session) -> None:
    """Squat guard: a notebook another owner parked at the victim's demo_id
    must not be returned to the victim by restore."""
    service = _service(db_session)
    victim = _user()
    attacker = _user()
    service.create(
        attacker,
        NotebookCreate(id=demo_id(victim.id), title="squat", cells=[]),
    )

    with pytest.raises(HTTPException) as exc:
        service.restore_features_demo(victim)

    assert exc.value.status_code == 404


def test_restore_does_not_resurrect_a_regular_deleted_notebook(
    db_session: Session,
) -> None:
    service = _service(db_session)
    user = _user()
    regular_id = uuid4()  # заведомо != demo_id(user.id)
    service.create(user, NotebookCreate(id=regular_id, title="regular", cells=[]))
    service.delete(user, regular_id)

    # No demo → restore 404; the regular notebook stays soft-deleted.
    with pytest.raises(HTTPException) as exc:
        service.restore_features_demo(user)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        service.get(user, regular_id)
