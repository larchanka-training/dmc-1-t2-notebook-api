"""Domain modules of the application.

Каждый подпакет здесь — отдельный домен (``auth``, ``health``,
``notebooks``). Внутри одна и та же структура: ``controllers`` →
``services`` → ``repositories`` → ``models`` + ``schemas``. Это
позволяет читать модуль как срез одной сущности и не размазывать её
логику по проекту.
"""
