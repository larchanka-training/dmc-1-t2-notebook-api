from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.modules.auth import router as auth_router
from app.modules.health import router as health_router
from app.modules.notebooks import router as notebooks_router

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
    {
        "name": "Auth",
        "description": "Placeholder current-user endpoint for local development.",
    },
    {
        "name": "Notebooks",
        "description": "Owner-scoped Notebook CRUD and offline-first sync endpoints.",
    },
]


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "## JS Notebook API\n\n"
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

install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_origin_regex=settings.cors_allowed_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User-Id"],
)

app.include_router(health_router, prefix=settings.api_prefix)
app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(notebooks_router, prefix=settings.api_prefix)


@app.get(
    "/",
    tags=["Root"],
    summary="Service welcome message",
    description="Returns a static welcome message; useful as a smoke test.",
)
def root() -> dict[str, str]:
    return {"message": "Welcome to MSD FastAPI Template"}
