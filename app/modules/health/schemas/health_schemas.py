from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComponentStatus(BaseModel):
    name: str = Field(..., description="Component identifier", examples=["database"])
    status: Literal["ok", "fail"] = Field(..., description="Component status")
    detail: str | None = Field(
        default=None,
        description="Optional diagnostic message when the component is unhealthy",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"name": "database", "status": "ok", "detail": None},
                {"name": "database", "status": "fail", "detail": "connection refused"},
            ]
        }
    )


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(..., description="Overall service status")
    app: str = Field(..., description="Application name", examples=["MSD FastAPI Template"])
    version: str = Field(..., description="Application version", examples=["0.1.0"])
    environment: str = Field(..., description="Runtime environment", examples=["dev", "prod"])
    components: list[ComponentStatus] = Field(
        default_factory=list,
        description="Per-component health checks (e.g. database)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "app": "MSD FastAPI Template",
                    "version": "0.1.0",
                    "environment": "dev",
                    "components": [],
                }
            ]
        }
    )
