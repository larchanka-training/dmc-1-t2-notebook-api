"""Unit tests for the dev placeholder auth dependency.

``get_current_user`` (the ``X-User-Id`` placeholder) no longer backs
``GET /auth/me`` — that route now validates a Bearer JWT (see
``test_auth_me_jwt.py``). The placeholder still backs the notebooks routes,
so it is exercised here directly and end-to-end in ``test_notebooks_api.py``.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.auth.dependencies import DEV_USER, get_current_user


def test_placeholder_returns_dev_user_without_header(db_session: Session) -> None:
    assert get_current_user(x_user_id=None, db=db_session) == DEV_USER


def test_placeholder_resolves_x_user_id(db_session: Session) -> None:
    user_id = uuid4()

    user = get_current_user(x_user_id=str(user_id), db=db_session)

    assert str(user.id) == str(user_id)
    assert user.email == f"{user_id}@dev.notebook.local"


def test_placeholder_rejects_invalid_x_user_id(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(x_user_id="bad", db=db_session)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "UNAUTHORIZED"


def test_placeholder_disabled_in_production(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(x_user_id=None, db=db_session)

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail["code"] == "AUTH_NOT_IMPLEMENTED"
