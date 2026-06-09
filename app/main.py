"""FastAPI application factory and top-level wiring.

Точка входа приложения. Здесь происходит вся «сборка»:

* настройка логирования (:func:`configure_logging`);
* создание ``FastAPI`` с OpenAPI-метаданными и тегами;
* регистрация обработчиков ошибок (:func:`install_error_handlers`);
* подключение CORS-middleware с разрешёнными origin;
* монтаж роутеров модулей (``health``, ``auth``, ``notebooks``);
* служебный ``GET /`` — простая «приветственная» проверка живости.

Модуль не содержит бизнес-логики: всё, что относится к доменам,
живёт в :mod:`app.modules`.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.modules.auth import router as auth_router
from app.modules.health import router as health_router
from app.modules.llm import router as llm_router
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
        "description": (
            "Email OTP sign-in, Bearer current-user restoration, "
            "refresh-token rotation, and logout endpoints."
        ),
    },
    {
        "name": "Notebooks",
        "description": "Owner-scoped Notebook CRUD and offline-first sync endpoints.",
    },
    {
        "name": "LLM",
        "description": (
            "Cloud code-generation endpoint backed by AWS Bedrock, protected "
            "by Bearer auth, prompt guard checks, rate limiting, and output validation."
        ),
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
        f"- Swagger UI: [`{settings.api_prefix}/docs`]({settings.api_prefix}/docs)\n"
        f"- ReDoc: [`{settings.api_prefix}/redoc`]({settings.api_prefix}/redoc)\n"
        f"- OpenAPI schema: [`{settings.api_prefix}/openapi.json`]"
        f"({settings.api_prefix}/openapi.json)\n"
    ),
    openapi_tags=tags_metadata,
    contact={
        "name": "MSD Course",
        "url": "https://github.com/larchanka-training/dmc-1-t2-notebook-api",
    },
    license_info={"name": "MIT"},
    # Docs/schema are served under the same prefix as the routers so they are
    # reachable behind the CloudFront/ALB proxy, which forwards only
    # `/api/v1/*` to the API (root `/docs` would hit the SPA on S3). The proxy
    # does not strip the prefix, so `root_path` is intentionally not used —
    # it would double-prefix the already-prefixed routes in "Try it out".
    docs_url=f"{settings.api_prefix}/docs",
    redoc_url=f"{settings.api_prefix}/redoc",
    openapi_url=f"{settings.api_prefix}/openapi.json",
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
app.include_router(llm_router, prefix=settings.api_prefix)


@app.get(
    "/",
    tags=["Root"],
    summary="Service welcome message",
    description="Returns a static welcome message; useful as a smoke test.",
)
def root() -> dict[str, str]:
    """Return a static welcome payload at the service root.

    Самый дешёвый smoke-эндпоинт: не лезет в БД, не требует auth,
    подходит для healthcheck из docker-compose и для ручной проверки
    «жив ли вообще процесс?».

    Returns:
        Словарь с единственным ключом ``"message"``.
    """
    return {"message": "Welcome to MSD FastAPI Template"}
