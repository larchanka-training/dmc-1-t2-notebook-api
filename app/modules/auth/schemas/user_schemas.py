from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CurrentUser(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = Field(default_factory=list)
