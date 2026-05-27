"""Pydantic schemas for auth-related responses.

Здесь живут только response-схемы (то, что мы отдаём клиенту). Сами
ORM-модели — в :mod:`app.modules.auth.models`.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CurrentUser(BaseModel):
    """Represents the current authenticated user.

    DTO, который возвращает ``GET /auth/me`` и который кладётся в
    зависимостях защищённых роутов. ``alias_generator=to_camel`` нужен,
    чтобы JSON получил ``displayName`` (camelCase), а Python-код мог
    обращаться к ``display_name`` (snake_case).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = Field(default_factory=list)
