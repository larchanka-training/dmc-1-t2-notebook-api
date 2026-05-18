from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import configure_logging
from app.modules.health import router as health_router

configure_logging()


tags_metadata = [
    {
        "name": "Root",
        "description": "Service entry point.",
    },
    {
        "name": "Health",
        "description": (
            "Liveness and readiness probes used by orchestrators "
            "(Kubernetes, Docker Compose) to monitor the service."
        ),
    },
]


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "## MSD FastAPI Template\n\n"
        "Backend API for the JS Notebook project. Built on a multi-module "
        "architecture: each domain module owns its `controllers`, "
        "`services` and `schemas`.\n\n"
        "**Documentation:**\n"
        "- Swagger UI: [`/docs`](/docs)\n"
        "- ReDoc: [`/redoc`](/redoc)\n"
        "- OpenAPI schema: [`/openapi.json`](/openapi.json)\n"
    ),
    openapi_tags=tags_metadata,
    contact={
        "name": "MSD Course",
        "url": "https://github.com/larchanka-training/dmc-1-t2-notebook-api",
    },
    license_info={"name": "MIT"},
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.include_router(health_router, prefix=settings.api_prefix)


@app.get(
    "/",
    tags=["Root"],
    summary="Service welcome message",
    description="Returns a static welcome message; useful as a smoke test.",
)
def root() -> dict[str, str]:
    return {"message": "Welcome to MSD FastAPI Template"}
