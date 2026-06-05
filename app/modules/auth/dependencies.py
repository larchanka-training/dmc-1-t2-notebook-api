"""FastAPI dependencies for resolving the current user.

Здесь живёт ``get_current_user`` — самая «горячая» dependency: каждый
защищённый роут получает текущего пользователя через неё. Реализация —
Bearer JWT (HS256 access token, выпущенный ``POST /auth/otp/verify``
или ``/auth/refresh``) плюс проверка, что соответствующая
``users.sessions`` не отозвана и не истекла.

Для локальной разработки и тестов рядом живёт ``get_placeholder_user``
— старая X-User-Id схема. Она доступна **только** в dev/test/local
(``settings.placeholder_auth_enabled``); в prod-like окружениях
возвращает 501. Используется как удобный shortcut, когда не нужно
гонять OTP-flow, а не как продакшн-механизм авторизации.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.repositories.session_repository import AuthSessionRepository
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.auth.services.token_service import AccessTokenError, AccessTokenService

DEV_USER = CurrentUser(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    email="dev@notebook.local",
    display_name="Dev User",
    roles=[],
)

# auto_error=False: we raise our own 401 in the standard error envelope instead
# of FastAPI's default {"detail": "Not authenticated"} shape.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_placeholder_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Resolve the current user from the ``X-User-Id`` header (dev only).

    Поведение зависит от ``settings.app_env``:

    * **dev/test/local**: при отсутствии заголовка возвращается
      «дев-пользователь» (фиксированный UUID), при наличии — UUID из
      заголовка валидируется и создаётся/получается ``User`` в БД.
    * **production/staging/прочее**: запрос немедленно отклоняется с
      ``501 AUTH_NOT_IMPLEMENTED`` — это страховка от того, что
      placeholder-схема случайно «утечёт» в прод.

    Args:
        x_user_id: Значение заголовка ``X-User-Id`` (опционально).
        db: SQLAlchemy-сессия, предоставленная :func:`get_db`.

    Returns:
        :class:`CurrentUser` для текущего запроса.

    Raises:
        HTTPException: 501, если ``app_env`` не dev/test/local;
            401, если ``X-User-Id`` не парсится как UUID.
    """
    # Placeholder auth is dev-only until real OTP/JWT auth lands.
    if not settings.placeholder_auth_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "code": "AUTH_NOT_IMPLEMENTED",
                "message": "Placeholder authentication is disabled outside dev/test environments.",
            },
        )
    if x_user_id is None:
        UserRepository(db).get_or_create_placeholder_user(
            DEV_USER.id,
            DEV_USER.email or "dev@notebook.local",
            DEV_USER.display_name,
        )
        return DEV_USER

    try:
        user_id = UUID(x_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Invalid X-User-Id"},
        ) from exc

    user = UserRepository(db).get_or_create_placeholder_user(
        user_id,
        f"{user_id}@dev.notebook.local",
    )
    return CurrentUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=[],
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """Resolve the current user from a Bearer JWT access token.

    Каноническая авторизация для всех защищённых endpoint'ов: проверяет
    подпись и срок HS256-токена, выданного ``POST /auth/otp/verify``
    или ``/auth/refresh``, и достаёт по ``sub`` пользователя из БД.
    Отдаёт ``401`` при отсутствии токена, порче подписи или истечении —
    это включает на фронте single-flight refresh и восстановление
    сессии.

    Дополнительно сверяет, что сессия из ``sessionId``-claim ещё
    активна в БД (не отозвана через logout / reuse-detection / истёкший
    срок) и принадлежит тому же пользователю, что ``sub``. Иначе
    access-token, выпущенный до logout, продолжал бы работать до
    ``exp``. См. ``api/docs/auth.md §6``.

    Args:
        credentials: ``Authorization: Bearer <token>`` (опционально —
            ``auto_error=False``, чтобы отдавать единый error-envelope).
        db: SQLAlchemy-сессия из :func:`get_db`.

    Returns:
        :class:`CurrentUser` владельца токена.

    Raises:
        HTTPException: 401 с кодом ``invalid_token``, если токен
            отсутствует, не Bearer, не проходит проверку, сессия из
            claim уже отозвана/истекла, принадлежит другому
            пользователю или пользователь не найден.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Missing bearer token"},
        )

    try:
        claims = AccessTokenService(settings).verify_access_token(credentials.credentials)
    except AccessTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Invalid or expired access token"},
        ) from exc

    active_session = AuthSessionRepository(db).get_active_by_id(
        claims.session_id,
        datetime.now(UTC),
    )
    if active_session is None or active_session.user_id != claims.user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_token",
                "message": "Session is revoked, expired, or mismatched",
            },
        )

    user = UserRepository(db).get_by_id(claims.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "User not found"},
        )

    return CurrentUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=[],
    )
