from uuid import UUID

from fastapi import Header, HTTPException, status

from app.modules.auth.schemas.user_schemas import CurrentUser

DEV_USER = CurrentUser(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    email="dev@notebook.local",
    display_name="Dev User",
    roles=[],
)


def get_current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CurrentUser:
    if x_user_id is None:
        return DEV_USER

    try:
        return CurrentUser(id=UUID(x_user_id), roles=[])
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid X-User-Id"},
        ) from exc
