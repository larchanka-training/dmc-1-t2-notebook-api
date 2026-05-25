from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.auth.models.user import User


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, user_id: UUID) -> User | None:
        return self.db.get(User, user_id)

    def get_or_create_placeholder_user(
        self,
        user_id: UUID,
        email: str,
        display_name: str | None = None,
    ) -> User:
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
        self.db.commit()
        self.db.refresh(user)
        return user
