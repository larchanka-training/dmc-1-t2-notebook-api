"""Notebooks domain module.

Главный домен сервиса: CRUD над «ноутбуками» (заметками) + offline-first
синхронизация ячеек. Внутри классический срез:

* ``models`` — SQLAlchemy ORM для таблицы ``notebooks.notebooks``;
* ``schemas`` — Pydantic-схемы запросов/ответов и константы лимитов;
* ``repositories`` — DAL поверх ``Session``;
* ``services`` — бизнес-логика (включая merge ячеек по LWW);
* ``controllers`` — HTTP-роуты, монтируются под ``/api/v1/notebooks``.
"""

from app.modules.notebooks.controllers import router

__all__ = ["router"]
