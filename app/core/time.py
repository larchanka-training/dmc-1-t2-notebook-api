from datetime import UTC, datetime


def datetime_to_unix_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def unix_ms_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def now_unix_ms() -> int:
    return datetime_to_unix_ms(datetime.now(UTC))
