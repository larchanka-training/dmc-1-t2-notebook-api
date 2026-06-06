"""Unit tests for the dev placeholder auth dependency.

``get_placeholder_user`` (the ``X-User-Id`` shortcut) is a dev/test-only
fallback; canonical ``get_current_user`` now validates a Bearer JWT
(see ``test_auth_me_jwt.py`` and ``test_notebooks_api.py``). The
placeholder is exercised here directly so its dev behaviour stays
documented and its prod-disable guard stays enforced.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.auth.dependencies import DEV_USER, get_placeholder_user


def test_placeholder_returns_dev_user_without_header(db_session: Session) -> None:
    assert get_placeholder_user(x_user_id=None, db=db_session) == DEV_USER


def test_placeholder_resolves_x_user_id(db_session: Session) -> None:
    user_id = uuid4()

    user = get_placeholder_user(x_user_id=str(user_id), db=db_session)

    assert str(user.id) == str(user_id)
    assert user.email == f"{user_id}@dev.notebook.local"


def test_placeholder_rejects_invalid_x_user_id(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        get_placeholder_user(x_user_id="bad", db=db_session)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["code"] == "UNAUTHORIZED"


def test_placeholder_disabled_in_production(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(HTTPException) as exc_info:
        get_placeholder_user(x_user_id=None, db=db_session)

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail["code"] == "AUTH_NOT_IMPLEMENTED"
