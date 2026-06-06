"""Authentication module.

Здесь подключается auth router и живёт текущая модель авторизации:
email OTP login, Bearer JWT access token, opaque refresh token,
refresh rotation, logout и ``GET /auth/me``. Dev/test placeholder
``X-User-Id`` сохранён только как вспомогательная dependency
``get_placeholder_user`` и не используется защищёнными production
endpoint'ами.

Re-export ``router`` нужен, чтобы :mod:`app.main` мог подключить его
одной строкой: ``from app.modules.auth import router``.
"""

from app.modules.auth.controllers import router

__all__ = ["router"]
