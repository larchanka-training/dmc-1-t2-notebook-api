"""Data-access layer for the ``User`` aggregate.

Тонкий репозиторий поверх SQLAlchemy. Никакой бизнес-логики: только
``get`` и «получить или создать» placeholder-пользователя. Транзакцией
по-прежнему управляет :func:`app.core.db.get_db` — здесь только
``flush``, не ``commit``.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.auth.models.user import User


class UserRepository:
    """Repository for ``users.users`` rows.

    Делегирует все DB-операции внешней ``Session`` (``self.db``).
    Конкретно из-за этой делегации мы можем держать одну транзакцию
    на весь HTTP-запрос и не «коммитить» из репозитория.
    """

    def __init__(self, db: Session) -> None:
        """Bind the repository to a request-scoped SQLAlchemy session.

        Args:
            db: Сессия, полученная из :func:`get_db`.
        """
        self.db = db

    def get_by_id(self, user_id: UUID) -> User | None:
        """Fetch a user by primary key.

        Args:
            user_id: UUID пользователя.

        Returns:
            ``User`` или ``None``, если запись не найдена.
        """
        return self.db.get(User, user_id)

    def get_or_create_placeholder_user(
        self,
        user_id: UUID,
        email: str,
        display_name: str | None = None,
    ) -> User:
        """Return a user by id, creating a placeholder row if missing.

        Используется placeholder-авторизацией: при первом обращении с
        новым ``X-User-Id`` мы материализуем «дев-пользователя», чтобы
        FK у ноутбука был валиден. Email/имя берутся синтетические.

        Args:
            user_id: UUID пользователя.
            email: Email, который запишется при создании.
            display_name: Опциональное отображаемое имя.

        Returns:
            Существующий или только что созданный ``User``.
        """
        user = self.get_by_id(user_id)
        if user is not None:
            return user

        user = User(
            id=user_id,
            email=email,
            display_name=display_name,
            created_at=datetime.now(UTC),
        )
        self.db.add(user)
        self.db.flush()
        return user
