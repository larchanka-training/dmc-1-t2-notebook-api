"""Time conversion helpers used across the notebook service.

Утилиты для перевода между Python ``datetime`` и Unix-временем в
миллисекундах. В контракте API клиент шлёт ``updatedAt``/``deletedAt``
как ``int`` в миллисекундах с эпохи (UTC), а БД хранит ``timestamptz``.
Все функции здесь — чистые: никаких сайд-эффектов, всё в UTC.
"""

from datetime import UTC, datetime


def datetime_to_unix_ms(value: datetime) -> int:
    """Convert a timezone-aware ``datetime`` to Unix milliseconds.

    Преобразует ``datetime`` в целое число миллисекунд от 1970-01-01 UTC.
    Если ``value`` пришёл без таймзоны (naive), мы безопасно считаем его
    UTC, чтобы избежать неявных сдвигов по локальной зоне сервера.

    Args:
        value: Метка времени; может быть aware или naive.

    Returns:
        Количество миллисекунд от эпохи (UTC) как ``int``.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def unix_ms_to_datetime(value: int) -> datetime:
    """Convert Unix milliseconds back to an aware UTC ``datetime``.

    Обратное преобразование к :func:`datetime_to_unix_ms`. Возвращаемый
    объект всегда aware и привязан к UTC — это инвариант, на который
    опирается merge-логика ноутбуков.

    Args:
        value: Число миллисекунд от эпохи (UTC).

    Returns:
        ``datetime`` в зоне UTC.
    """
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def now_unix_ms() -> int:
    """Return the current UTC time as Unix milliseconds.

    Короткая обёртка над ``datetime.now(UTC)`` плюс конвертация. Удобно
    дёргать в тестах и сервисах, где нужен единый источник «сейчас».

    Returns:
        Текущее время в миллисекундах от эпохи (UTC).
    """
    return datetime_to_unix_ms(datetime.now(UTC))
