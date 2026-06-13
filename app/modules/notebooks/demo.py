"""Canonical feature-demo notebook identity.

У каждого пользователя есть один «стартовый» feature-demo notebook —
seed-ноутбук, который фронт создаёт сразу после первой авторизации. Бэку
нужно устойчиво отличать его от обычных пользовательских ноутбуков, чтобы
restore (см. :meth:`NotebookService.restore_features_demo`) работал именно
для него, а не превращался в общий restore произвольных записей.

Идентичность — **детерминированный per-user id**, выводимый из ``owner_id``::

    demo_id(owner_id) = uuidv5(DEMO_NAMESPACE, str(owner_id))

Почему так, а не явное поле в модели:

* не нужна Liquibase-миграция и не расширяется публичный create-контракт
  (модель уже на client-chosen id: ``notebook_id = payload.id or uuid4()``);
* demo рождает фронт (seed на boot → обычный ``POST``/``PATCH``); раз
  identity всё равно должны договорить фронт и бэк — детерминированный id
  и есть этот общий контракт без новых полей;
* в single-notebook реальности demo = единственный ноутбук пользователя,
  и этот же per-user id заменяет глобальный фронтовый ``LOCAL_NOTEBOOK_ID``.

``demo_id`` предсказуем (любой может вычислить чужой), но это не уязвимость
доступа: restore и все notebook-операции скоупятся по ``current_user`` +
owner-check, поэтому угадать id ≠ получить, удалить или изменить чужой
ноутбук.

Остаточный риск — **доступность**, не доступ. Другой пользователь может
заранее занять чужой ``demo_id`` обычным ``POST`` (id клиентский, PK
глобальный); тогда seed/restore жертвы упрётся в 403/404, пока слот не
освободят. Это принятый trade-off: детерминированный id — обязательный
контракт с фронтом (он сидит demo на том же id), и заменить его на
owner-scoped marker нельзя без расхождения с фронтом. Вероятность мала —
нужен чужой ``owner_id`` (UUIDv4), который cross-user в API не отдаётся, а
demo — некритичный стартовый контент.
"""

from typing import Final
from uuid import UUID, uuid5

#: Фиксированная UUID-константа namespace для :func:`demo_id`. **Общий
#: контракт backend ↔ frontend**: канонический источник — здесь (backend),
#: фронт (UI #67) повторяет ровно это значение. Менять нельзя — смена
#: namespace осиротит все существующие feature-demo notebooks.
DEMO_NAMESPACE: Final[UUID] = UUID("7f3a2b14-9c8d-4e6f-b1a2-c3d4e5f60718")


def demo_id(owner_id: UUID) -> UUID:
    """Return the canonical feature-demo notebook id for ``owner_id``.

    Детерминированный uuidv5: один и тот же ``owner_id`` всегда даёт один и
    тот же id, разные пользователи — разные id.

    Args:
        owner_id: UUID владельца (``users.users.id``).

    Returns:
        UUID feature-demo notebook этого пользователя.
    """
    return uuid5(DEMO_NAMESPACE, str(owner_id))
