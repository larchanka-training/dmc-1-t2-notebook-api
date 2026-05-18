from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.modules.health.schemas import HealthResponse
from app.modules.health.services import build_liveness, build_readiness

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
def readiness(response: Response, db: Session = Depends(get_db)) -> HealthResponse:
    result = build_readiness(db)
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
