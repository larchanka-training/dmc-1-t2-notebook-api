"""Business logic for notebook CRUD and offline-first sync.

«Толстый» сервис домена ноутбуков. Контроллеры сюда передают
:class:`CurrentUser` и DTO, сервис делает:

* проверку владельца (owner-scoping);
* валидацию ``formatVersion`` (поддерживается до ``CURRENT``);
* идемпотентное создание (если ``id`` уже есть — сверка payload или 409);
* merge ячеек на ``PATCH`` через :func:`merge_cells`;
* мягкое удаление через ``deleted_at``.

Здесь же лежат фабрики-помощники для типовых HTTPException — это
гарантирует одинаковый ``error envelope`` во всех ответах сервиса.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.core.time import datetime_to_unix_ms, unix_ms_to_datetime
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.notebooks.demo import demo_id
from app.modules.notebooks.entities import NotebookEntity
from app.modules.notebooks.repositories.protocol import NotebookRepositoryProtocol
from app.modules.notebooks.schemas.notebook_schemas import (
    ALLOWED_ORDERS,
    ALLOWED_SORTS,
    CURRENT_FORMAT_VERSION,
    CellSchema,
    CellTombstone,
    NotebookCreate,
    NotebookListItem,
    NotebookListResponse,
    NotebookPatch,
    NotebookResponse,
)
from app.modules.notebooks.services.notebook_merge import merge_cells

#: Допуск «вперёд» для клиентского ``updatedAt`` при вычислении
#: top-level ``notebook.updated_at`` — защита от часов клиента,
#: убежавших в будущее.
MAX_FUTURE_SKEW_MS = 5_000


def notebook_not_found() -> HTTPException:
    """Build a 404 ``HTTPException`` with the standard error envelope."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOTEBOOK_NOT_FOUND", "message": "Notebook not found"},
    )


def forbidden() -> HTTPException:
    """Build a 403 ``HTTPException`` for owner-scope violations."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "FORBIDDEN", "message": "Forbidden"},
    )


def invalid_query(message: str) -> HTTPException:
    """Build a 400 ``HTTPException`` for malformed list-query params.

    Используется, когда клиент прислал недопустимое значение ``sort``
    или ``order`` — то, что не отлавливает Pydantic-валидация query.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "INVALID_QUERY", "message": message},
    )


def unsupported_format_version() -> HTTPException:
    """Build a 400 ``HTTPException`` when client format version is too new.

    Сценарий: фронт обновился раньше бэка и шлёт ``formatVersion``,
    которого сервер ещё не понимает. Лучше явно отказать, чем
    проглотить «незнакомые» поля.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "UNSUPPORTED_FORMAT_VERSION",
            "message": f"Only formatVersion <= {CURRENT_FORMAT_VERSION} is supported",
        },
    )


def notebook_conflict(message: str) -> HTTPException:
    """Build a 409 ``HTTPException`` for idempotency conflicts.

    Бросаем, когда клиент повторил ``POST`` с тем же ``id``, но другим
    содержимым — это уже не идемпотентность, это потерянный апдейт
    (Шаг 10 PR #29).
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "NOTEBOOK_CONFLICT", "message": message},
    )


class NotebookService:
    """Use-cases for notebook CRUD + offline sync (PATCH merge).

    Слой бизнес-логики: знает про :class:`CurrentUser`, валидирует
    инварианты домена, делегирует persistence-операции репозиторию.
    Сюда не проникают ни ``Session``, ни ``Request``, ни ORM-модели —
    только DTO и storage-neutral entities.
    """

    def __init__(self, repository: NotebookRepositoryProtocol) -> None:
        """Bind the service to a notebook repository.

        Args:
            repository: Любая реализация :class:`NotebookRepositoryProtocol`
                (на MVP это SQL-репозиторий; будущая NoSQL-реализация
                подмешивается через DI без правок в сервисе).
        """
        self.repository = repository

    def create(
        self,
        current_user: CurrentUser,
        payload: NotebookCreate,
    ) -> tuple[NotebookResponse, bool]:
        """Create a notebook (idempotent on ``payload.id``).

        Семантика:

        * Если ``id`` ещё нет в БД → INSERT и возврат ``(notebook, True)``.
        * Если ``id`` уже есть **и принадлежит другому owner** → 403.
        * Если есть, но soft-deleted → 404 (как будто его нет).
        * Если есть, owner совпадает, payload **идентичен** → 200 с
          существующей записью и ``created=False``.
        * Если есть, owner совпадает, payload **отличается** → 409.

        Args:
            current_user: Авторизованный пользователь.
            payload: Тело запроса.

        Returns:
            Пара ``(NotebookResponse, created_flag)``.

        Raises:
            HTTPException: 403/404/409 в случаях выше; 400 при
                несовместимой ``formatVersion``.
        """
        notebook_id = payload.id or uuid4()
        existing = self.repository.get_by_id(notebook_id)
        if existing is not None:
            if existing.owner_id != current_user.id:
                raise forbidden()
            if existing.deleted_at is not None:
                raise notebook_not_found()
            if not self._matches_create_payload(existing, payload):
                raise notebook_conflict(
                    "Notebook with id already exists with different content"
                )

            return self.to_response(existing), False

        self._validate_format_version(payload.format_version)
        now = datetime.now(UTC)
        cells = self._cells_to_storage(payload.cells)
        updated_at = self._compute_updated_at(cells, now)
        notebook = NotebookEntity(
            id=notebook_id,
            owner_id=current_user.id,
            title=payload.title,
            format_version=payload.format_version,
            cells=cells,
            created_at=now,
            updated_at=updated_at,
            deleted_at=None,
        )
        return self.to_response(self.repository.save(notebook)), True

    def list(
        self,
        current_user: CurrentUser,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> NotebookListResponse:
        """List active notebooks of the current user with pagination.

        Прозрачно проксирует параметры в репозиторий, добавляя
        белый список значений ``sort``/``order``. Возвращает
        «лёгкую» проекцию (без поля ``cells``).

        Args:
            current_user: Авторизованный пользователь (фильтр по owner).
            limit: Размер страницы.
            offset: Смещение.
            sort: Поле сортировки (валидируется по ``ALLOWED_SORTS``).
            order: Направление (валидируется по ``ALLOWED_ORDERS``).

        Returns:
            Страница в виде :class:`NotebookListResponse`.

        Raises:
            HTTPException: 400, если ``sort`` или ``order`` вне whitelist.
        """
        if sort not in ALLOWED_SORTS:
            raise invalid_query("Unsupported sort field")
        if order not in ALLOWED_ORDERS:
            raise invalid_query("Unsupported order")

        items, total = self.repository.list_by_owner(
            current_user.id,
            limit,
            offset,
            sort,
            order,
        )
        return NotebookListResponse(
            items=[self.to_list_item(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get(self, current_user: CurrentUser, notebook_id: UUID) -> NotebookResponse:
        """Return a single active notebook owned by the current user.

        Args:
            current_user: Авторизованный пользователь.
            notebook_id: UUID ноутбука.

        Returns:
            Полная :class:`NotebookResponse`.

        Raises:
            HTTPException: 404, если нет/soft-deleted; 403, если чужой.
        """
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        return self.to_response(notebook)

    def patch(
        self,
        current_user: CurrentUser,
        notebook_id: UUID,
        payload: NotebookPatch,
    ) -> NotebookResponse:
        """Apply a sync document to a notebook and persist the merge result.

        «Сердечный» метод offline-first синхронизации. Берёт серверные
        ячейки, ячейки клиента и тумбстоуны, прогоняет через
        :func:`merge_cells`, пересчитывает top-level ``updated_at``
        (с защитой от clock-skew) и сохраняет результат.

        Args:
            current_user: Авторизованный пользователь.
            notebook_id: UUID ноутбука.
            payload: Полный sync-документ от клиента.

        Returns:
            Обновлённая :class:`NotebookResponse`.

        Raises:
            HTTPException: 404, если ноутбук не найден/удалён; 403, если
                чужой; 400 — несовместимая ``formatVersion``.
        """
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        self._validate_format_version(payload.format_version)

        client_cells = self._cells_to_storage(payload.cells)
        deleted_cells = self._tombstones_to_storage(payload.deleted_cells)
        merged_cells = merge_cells(notebook.cells or [], client_cells, deleted_cells)

        now = datetime.now(UTC)
        notebook.title = payload.title
        notebook.format_version = payload.format_version
        notebook.cells = merged_cells
        notebook.updated_at = self._compute_updated_at(merged_cells, now)
        return self.to_response(self.repository.save(notebook))

    def delete(self, current_user: CurrentUser, notebook_id: UUID) -> None:
        """Soft-delete a notebook owned by the current user.

        Args:
            current_user: Авторизованный пользователь.
            notebook_id: UUID ноутбука.

        Raises:
            HTTPException: 404, если уже удалён или не найден; 403, если
                принадлежит другому пользователю.
        """
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        self.repository.soft_delete(notebook, datetime.now(UTC))

    def restore_features_demo(self, current_user: CurrentUser) -> NotebookResponse:
        """Restore the current user's canonical feature-demo notebook.

        Resurrect-only. Эндпоинт не принимает id и не делает общий restore:
        он вычисляет :func:`demo_id` от ``current_user.id`` и работает только
        с этой записью:

        * soft-deleted → сбрасывает ``deleted_at``, сохраняя прежние
          ``cells`` и ``updated_at`` (точное воскрешение: логическое время
          правки намеренно не бампится);
        * active → идемпотентно возвращает существующую (без дублей);
        * отсутствует **или** принадлежит другому owner → 404.

        Owner-check здесь не декоративный: ``demo_id`` предсказуем, и другой
        пользователь мог заранее занять этот id обычным ``POST``. Без проверки
        restore вернул бы чужой ноутбук — поэтому foreign-owned запись
        трактуется как «demo нет».

        Backend намеренно НЕ создаёт seed-контент при отсутствии demo: его
        сидит фронт на boot, поэтому «никогда не создавался» — явная ошибка,
        а не повод выдумывать ноутбук.

        Args:
            current_user: Авторизованный пользователь.

        Returns:
            Восстановленный или уже активный :class:`NotebookResponse`.

        Raises:
            HTTPException: 404, если feature-demo notebook не найден.
        """
        notebook = self.repository.get_by_id(demo_id(current_user.id))
        if notebook is None or notebook.owner_id != current_user.id:
            raise notebook_not_found()
        if notebook.deleted_at is not None:
            notebook.deleted_at = None
            notebook = self.repository.save(notebook)
        return self.to_response(notebook)

    def _get_active_notebook(self, notebook_id: UUID) -> NotebookEntity:
        """Return notebook by id or raise 404 if missing/deleted.

        Args:
            notebook_id: UUID.

        Returns:
            Активная domain entity.

        Raises:
            HTTPException: 404 в обоих сценариях «нет» и «soft-deleted».
        """
        notebook = self.repository.get_by_id(notebook_id)
        if notebook is None or notebook.deleted_at is not None:
            raise notebook_not_found()
        return notebook

    def _ensure_owner(
        self, notebook: NotebookEntity, current_user: CurrentUser
    ) -> None:
        """Raise 403 if the notebook does not belong to the user.

        Args:
            notebook: Domain entity.
            current_user: Авторизованный пользователь.

        Raises:
            HTTPException: 403 при несовпадении ``owner_id``.
        """
        if notebook.owner_id != current_user.id:
            raise forbidden()

    def _validate_format_version(self, format_version: int) -> None:
        """Raise 400 if client uses a format version newer than the server.

        Args:
            format_version: Версия формата из payload.

        Raises:
            HTTPException: 400 при ``format_version > CURRENT_FORMAT_VERSION``.
        """
        if format_version > CURRENT_FORMAT_VERSION:
            raise unsupported_format_version()

    def _compute_updated_at(
        self,
        cells: list[dict],
        fallback: datetime,
    ) -> datetime:
        """Compute top-level ``notebook.updated_at`` with clock-skew clamp.

        Формула (см. ``docs/auth.md``)::

            latest = min(max(cell.updatedAt), now + MAX_FUTURE_SKEW_MS)
            latest = max(latest, now)

        То есть top-level время не может «убежать» в будущее дальше,
        чем на 5 секунд от серверного now, и не может быть «в прошлом»
        относительно текущего сохранения. ``fallback`` — общий источник
        времени (Шаг 16 PR #29: убрали второй ``time.time()``).

        Args:
            cells: Список ячеек в API-формате.
            fallback: Серверное «сейчас», переданное снаружи.

        Returns:
            Итоговая метка времени для ``notebook.updated_at``.
        """
        if not cells:
            return fallback
        latest_cell_ms = max(int(cell["updatedAt"]) for cell in cells)
        fallback_ms = datetime_to_unix_ms(fallback)
        latest = min(latest_cell_ms, fallback_ms + MAX_FUTURE_SKEW_MS)
        latest = max(latest, fallback_ms)
        return unix_ms_to_datetime(latest)

    def _cells_to_storage(self, cells: list[CellSchema]) -> list[dict]:
        """Serialize Pydantic cells to the camelCase shape stored in JSONB.

        FE и БД работают с camelCase (контракт), поэтому ``by_alias=True``.
        ``mode="json"`` нужен, чтобы UUID и datetime превратились в
        ``str``/``int`` — иначе сравнение «БД vs payload» давало бы
        ложный mismatch на одинаковых данных.

        Args:
            cells: Список Pydantic-моделей ячеек.

        Returns:
            Список словарей в API-формате.
        """
        return [cell.model_dump(by_alias=True, mode="json") for cell in cells]

    def _tombstones_to_storage(self, tombstones: list[CellTombstone]) -> list[dict]:
        """Serialize tombstones to the API/JSONB shape.

        Симметрично :meth:`_cells_to_storage`, но для надгробий.
        В БД эти данные не попадают (они нужны только для merge), но
        мы держим ту же сериализацию для единообразия.

        Args:
            tombstones: Pydantic-модели надгробий.

        Returns:
            Список словарей в API-формате.
        """
        return [
            tombstone.model_dump(by_alias=True, mode="json") for tombstone in tombstones
        ]

    def _matches_create_payload(
        self, notebook: NotebookEntity, payload: NotebookCreate
    ) -> bool:
        """Tell whether an existing row matches an idempotent POST payload.

        Сравниваем поля, которые формирует клиент: ``title``,
        ``format_version``, ``cells``. Системные ``created_at`` и
        ``owner_id`` намеренно вне сравнения.

        Args:
            notebook: Существующая domain entity.
            payload: Тело повторного POST.

        Returns:
            ``True`` если данные идентичны (можно вернуть существующий),
            ``False`` если есть расхождение (сервис бросит 409).
        """
        return (
            notebook.title == payload.title
            and notebook.format_version == payload.format_version
            and (notebook.cells or []) == self._cells_to_storage(payload.cells)
        )

    def to_response(self, notebook: NotebookEntity) -> NotebookResponse:
        """Map a domain ``NotebookEntity`` to a public :class:`NotebookResponse`.

        Времена переводятся в миллисекунды от эпохи, ``cells`` отдаются
        в API/JSON shape. Это — единственная точка проекции
        «domain entity → API» для деталки ноутбука.

        Args:
            notebook: Domain entity.

        Returns:
            DTO для ответа.
        """
        return NotebookResponse(
            id=notebook.id,
            owner_id=notebook.owner_id,
            title=notebook.title,
            format_version=notebook.format_version,
            cells=notebook.cells or [],
            created_at=datetime_to_unix_ms(notebook.created_at),
            updated_at=datetime_to_unix_ms(notebook.updated_at),
        )

    def to_list_item(self, notebook: NotebookEntity) -> NotebookListItem:
        """Map a domain ``NotebookEntity`` to a lightweight list item.

        В отличие от :meth:`to_response`, ``cells`` *не отдаются* — только
        их количество. Это бережёт пропускную способность для тяжёлых
        ноутбуков и не нагружает фронт лишним JSON.

        Args:
            notebook: Domain entity.

        Returns:
            «Тонкий» DTO для списка.
        """
        return NotebookListItem(
            id=notebook.id,
            title=notebook.title,
            format_version=notebook.format_version,
            created_at=datetime_to_unix_ms(notebook.created_at),
            updated_at=datetime_to_unix_ms(notebook.updated_at),
            cells_count=len(notebook.cells or []),
        )
