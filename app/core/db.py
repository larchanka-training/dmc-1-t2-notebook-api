"""SQLAlchemy engine, session factory and FastAPI DB dependency.

Единая точка работы с базой. Здесь:

* :class:`Base` — общий declarative-base, от которого наследуются все
  ORM-модели (``Notebook``, ``User`` и т. д.);
* :func:`get_engine` и :func:`get_session_factory` — ленивые
  кешированные синглтоны (используется ``functools.lru_cache``),
  чтобы один Engine жил всё время процесса;
* :func:`get_db` — FastAPI-dependency, которая владеет жизненным
  циклом сессии и транзакции (см. Шаг 12 из разбора PR #29).
"""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """Common declarative base for all ORM models.

    Базовый класс для ORM-моделей. SQLAlchemy 2.0 требует, чтобы все
    мэппинги наследовались от одного ``DeclarativeBase`` — это даёт
    общий ``MetaData`` и одну точку для ``Base.metadata.create_all()``
    в тестах на SQLite.
    """


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Build (and cache) the SQLAlchemy ``Engine`` for the configured DB.

    Создаёт ``Engine`` ровно один раз на процесс. ``pool_pre_ping=True``
    нужен, чтобы пул отбрасывал «протухшие» соединения после restart
    Postgres. ``echo`` пробрасывается из настроек — удобно дебажить SQL.

    Returns:
        Готовый к использованию SQLAlchemy ``Engine``.
    """
    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory bound to the shared engine.

    Фабрика сессий с фиксированными параметрами:

    * ``autoflush=False`` — flush делаем явно, через ``session.flush()``;
    * ``autocommit=False`` — стандарт для SA 2.0, транзакцию открываем сами;
    * ``expire_on_commit=False`` — после ``commit`` объекты остаются
      пригодными для чтения (не нужно вызывать ``refresh``).

    Returns:
        ``sessionmaker``, который привязан к синглтон-``Engine``.
    """
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session as a FastAPI dependency.

    Единственный санкционированный способ получить ``Session`` в роуте.
    Управляет транзакционной границей на уровне HTTP-запроса:

    * успех роута → ``commit``;
    * любое исключение → ``rollback`` (с пробросом наверх);
    * всегда → ``close``.

    Репозитории внутри запроса вызывают ``self.db.flush()`` для отправки
    SQL без закрытия транзакции — это паттерн «unit of work».

    Yields:
        Открытая сессия, привязанная к одному HTTP-запросу.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
