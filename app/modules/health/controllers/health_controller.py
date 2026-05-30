"""HTTP controller for ``/health`` and ``/health/ready``.

Probe-эндпоинты для оркестратора. Логику собирают функции из
:mod:`app.modules.health.services`, контроллер только адаптирует
коды состояния (200/503).
"""

from fastapi import APIRouter, Depends, Response, status

from app.modules.health.dependencies import get_readiness
from app.modules.health.schemas import HealthResponse
from app.modules.health.services import build_liveness

router = APIRouter(prefix="/health", tags=["Health"])


@router.get(
    "",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Returns basic application liveness information without checking "
        "external dependencies. Use this endpoint for Kubernetes "
        "`livenessProbe` to determine whether the process is alive."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "Service is alive",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "app": "MSD FastAPI Template",
                        "version": "0.1.0",
                        "environment": "dev",
                        "components": [],
                    }
                }
            },
        }
    },
)
def healthcheck() -> HealthResponse:
    """``GET /health`` — liveness probe.

    Не лезет в БД и в зависимости. Достаточно для ``livenessProbe``
    Kubernetes.

    Returns:
        :class:`HealthResponse` со статусом ``"ok"``.
    """
    return build_liveness()


@router.get(
    "/ready",
    response_model=HealthResponse,
    summary="Readiness probe",
    description=(
        "Verifies that the application and its critical dependencies "
        "(database) are reachable. Use this endpoint for Kubernetes "
        "`readinessProbe`. Returns HTTP 200 when all critical dependencies "
        "are healthy and HTTP 503 when a dependency is unavailable."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        200: {
            "description": "All components healthy",
            "content": {
                "application/json": {
                    "example": {
                        "status": "ok",
                        "app": "MSD FastAPI Template",
                        "version": "0.1.0",
                        "environment": "dev",
                        "components": [
                            {"name": "database", "status": "ok", "detail": None}
                        ],
                    }
                }
            },
        },
        503: {
            "description": "One or more critical dependencies are unreachable",
            "content": {
                "application/json": {
                    "example": {
                        "status": "degraded",
                        "app": "MSD FastAPI Template",
                        "version": "0.1.0",
                        "environment": "dev",
                        "components": [
                            {
                                "name": "database",
                                "status": "fail",
                                "detail": "connection refused",
                            }
                        ],
                    }
                }
            },
        }
    },
)
def readiness(
    response: Response,
    result: HealthResponse = Depends(get_readiness),
) -> HealthResponse:
    """``GET /health/ready`` — readiness probe (checks the database).

    При сбое любого критичного компонента статус ответа меняется на
    503, чтобы оркестратор временно выкинул pod из балансировки.

    Args:
        response: FastAPI-объект ответа (нужен для смены статуса).
        result: Готовый :class:`HealthResponse`, собранный DI-фабрикой
            :func:`get_readiness`. Storage-зависимость (БД) живёт
            внутри фабрики, контроллер о ней не знает.

    Returns:
        Заполненный :class:`HealthResponse`.
    """
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
