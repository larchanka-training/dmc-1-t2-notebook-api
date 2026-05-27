"""Health module — liveness and readiness probes.

Стандартный модуль для оркестратора (Kubernetes, Docker Compose):
``/health`` отвечает на «жив ли процесс», ``/health/ready`` — «готов ли
обслуживать трафик» (проверяет БД). Не зависит ни от auth, ни от
доменных модулей.
"""

from app.modules.health.controllers import router

__all__ = ["router"]
