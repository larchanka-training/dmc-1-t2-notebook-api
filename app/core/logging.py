"""Application-wide logging configuration based on ``structlog``.

Единая настройка логирования. Мы хотим:

* в dev — цветной человекочитаемый вывод;
* в prod — JSON-логи в stdout, чтобы их забирал агрегатор (Loki/ELK);
* единый формат timestamp (ISO, UTC) и поле ``message`` вместо ``event``;
* подавление дублирующих handler'ов uvicorn.

Вызывать :func:`configure_logging` нужно один раз при старте приложения
(делается в :mod:`app.main`).
"""

import logging
import sys
from typing import Any

import structlog

from app.core.config import settings


def _build_shared_processors() -> list[Any]:
    """Return the chain of structlog processors shared by all renderers.

    Список процессоров, через которые проходит каждое сообщение перед
    финальным рендером. Отсюда же берётся ISO-timestamp и переименование
    стандартного ``event`` в ``message`` (удобнее для агрегаторов).

    Returns:
        Список ``structlog`` процессоров.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
        structlog.processors.EventRenamer("message"),
    ]


def configure_logging() -> None:
    """Configure ``structlog`` and stdlib ``logging`` for the whole app.

    Включает один общий ``Handler`` на ``stdout``, выставляет уровень
    из ``settings.log_level``, рендер выбирает по ``settings.log_json``
    (JSON для prod / colored console для dev). Сбрасывает handler'ы у
    шумных uvicorn-логгеров и пускает их по общей цепочке.

    Returns:
        ``None``. Эффект — побочный (изменяет глобальное состояние
        стандартного ``logging``).
    """
    shared_processors = _build_shared_processors()

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a ``structlog`` bound logger, optionally namespaced.

    Тонкая обёртка над ``structlog.get_logger``. Использовать так::

        logger = get_logger(__name__)
        logger.info("notebook.created", notebook_id=str(notebook.id))

    Args:
        name: Имя логгера (обычно ``__name__`` модуля).

    Returns:
        Связанный логгер, готовый к использованию.
    """
    return structlog.get_logger(name)
