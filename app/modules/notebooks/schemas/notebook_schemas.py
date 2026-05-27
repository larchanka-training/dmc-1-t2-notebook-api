"""Pydantic schemas and shared constants for the notebooks API.

Здесь живут все DTO (request/response модели) и публичные «ручки»
лимитов/допустимых значений. Сами лимиты вынесены в константы, чтобы:

* их можно было импортировать в тестах и `auth.md`;
* при изменении не править несколько разбросанных по коду чисел.

Все схемы сериализуются в camelCase — это контракт с фронтом
(``alias_generator=to_camel``). В Python-коде продолжаем писать
``updated_at``, при сериализации получаем ``updatedAt``.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

#: Текущая версия формата ноутбука. Сервер отказывает в ``> CURRENT``.
CURRENT_FORMAT_VERSION = 1
#: Поля, по которым разрешено сортировать список ноутбуков.
ALLOWED_SORTS = {"updatedAt", "createdAt", "title"}
#: Допустимые направления сортировки.
ALLOWED_ORDERS = {"asc", "desc"}
#: Максимальный размер ``cell.content`` в байтах/символах (DoS guard).
MAX_CELL_CONTENT_BYTES = 256 * 1024
#: Максимальное число ячеек на один ноутбук (DoS guard).
MAX_CELLS_PER_NOTEBOOK = 500


class CellSchema(BaseModel):
    """Single notebook cell as exchanged over the API.

    Атомарная ячейка: код или markdown. ``updated_at`` ставит сам
    клиент (миллисекунды от эпохи) — сервер его не переписывает.
    Это и есть основа LWW-синхронизации.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    kind: Literal["code", "markdown"]
    content: str = Field(default="", max_length=MAX_CELL_CONTENT_BYTES)
    updated_at: int = Field(..., ge=0)


class CellTombstone(BaseModel):
    """Tombstone record for a cell deleted on the client.

    Клиент шлёт «надгробие», чтобы сервер при merge мог понять: эта
    ячейка удалена локально с такой-то меткой времени. Если на сервере
    та же ячейка обновилась *позже* ``deleted_at`` — удаление
    игнорируется (см. :func:`merge_cells`).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    deleted_at: int = Field(..., ge=0)


class NotebookBase(BaseModel):
    """Common shape shared by create/patch/response models.

    Здесь общие поля и валидаторы. Любая попытка прислать дубликаты
    ``cell.id`` отлавливается на уровне Pydantic и превращается в 422
    через стандартный handler (см. :mod:`app.core.errors`).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=255)
    format_version: int = Field(default=CURRENT_FORMAT_VERSION, ge=1)
    cells: list[CellSchema] = Field(
        default_factory=list, max_length=MAX_CELLS_PER_NOTEBOOK
    )

    @model_validator(mode="after")
    def validate_unique_cell_ids(self) -> "NotebookBase":
        """Reject payloads where two cells share an id.

        Без этой проверки два клиентских изменения с одним ``id``
        молча схлопнулись бы в словаре merge'а — «silent data
        corruption» (Шаг 15 PR #29).

        Raises:
            ValueError: При наличии дубликата ``cell.id``.

        Returns:
            ``self`` (требование Pydantic-валидатора).
        """
        ids = [cell.id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("cells must have unique ids")
        return self


class NotebookCreate(NotebookBase):
    """Request body for ``POST /notebooks``.

    Отличается от ``NotebookBase`` опциональным ``id``: если клиент
    его не присылает, сервер сгенерирует ``uuid4`` сам. Если присылает —
    создание становится идемпотентным (см. ``NotebookService.create``).
    """

    id: UUID | None = None


class NotebookPatch(NotebookBase):
    """Request body for ``PATCH /notebooks/{id}``.

    Сейчас это «full sync document», а не классический partial PATCH:
    клиент шлёт полный массив активных ``cells`` плюс ``deletedCells``
    (надгробия) — сервер делает LWW-merge. Семантически это ближе к
    PUT; решение оставить как PATCH было сознательным (Шаг 9 PR #29).
    """

    deleted_cells: list[CellTombstone] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_deleted_cell_ids(self) -> "NotebookPatch":
        """Reject payloads where two tombstones share an id.

        Симметрично :meth:`validate_unique_cell_ids`, но для массива
        ``deleted_cells``.

        Raises:
            ValueError: При дубликате id в ``deleted_cells``.

        Returns:
            ``self`` для дальнейшей валидации.
        """
        ids = [cell.id for cell in self.deleted_cells]
        if len(ids) != len(set(ids)):
            raise ValueError("deleted_cells must have unique ids")
        return self


class NotebookResponse(NotebookBase):
    """Detailed notebook representation returned by single-item endpoints.

    Используется в ответах ``POST/GET/PATCH /notebooks/{id}``. Времена
    отдаются в миллисекундах от эпохи (UTC) — единый формат с фронтом.
    """

    id: UUID
    owner_id: UUID
    created_at: int
    updated_at: int


class NotebookListItem(BaseModel):
    """Lightweight notebook row for list endpoints.

    Не содержит ``cells`` — только их количество. Это сильно сокращает
    payload листинга и предотвращает выгрузку больших JSONB в ответ
    на ``GET /notebooks``.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    title: str
    format_version: int
    created_at: int
    updated_at: int
    cells_count: int


class NotebookListResponse(BaseModel):
    """Paginated response wrapper for ``GET /notebooks``.

    Стандартная «обёртка» с метаданными пагинации. ``total`` считается
    отдельным запросом и нужен фронту для построения нумерации страниц.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[NotebookListItem]
    total: int
    limit: int
    offset: int
