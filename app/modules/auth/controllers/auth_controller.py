"""HTTP controller for the ``auth`` module.

Один-единственный роут ``GET /auth/me``: его задача — отдать клиенту
текущего пользователя. Вся «магия» происходит в dependency
:func:`get_current_user`; контроллер только пробрасывает результат.
"""

from fastapi import APIRouter, Depends

from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="Get current user",
    description="Returns the current placeholder user for local development.",
)
def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return the authenticated user resolved by the dependency.

    Эндпоинт-маркер: используется фронтом, чтобы убедиться, что
    заголовок ``X-User-Id`` валиден и сервер согласен с этим
    пользователем. В будущем заменится реальной /me на базе JWT.

    Args:
        current_user: Внедряется ``Depends(get_current_user)``.

    Returns:
        Тот же :class:`CurrentUser`, без модификаций.
    """
    return current_user
