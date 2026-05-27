"""Authentication module.

Сейчас тут живёт *placeholder-авторизация* для локальной разработки:
сервер доверяет HTTP-заголовку ``X-User-Id`` и при необходимости
создаёт «фантомного» пользователя. Реальный OTP/JWT — отдельная
будущая задача (см. follow-up в разборе PR #29, Шаг 2).

Re-export ``router`` нужен, чтобы :mod:`app.main` мог подключить его
одной строкой: ``from app.modules.auth import router``.
"""

from app.modules.auth.controllers import router

__all__ = ["router"]
