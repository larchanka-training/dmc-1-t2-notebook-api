"""Health module: liveness and readiness probes.

Layout follows the multi-module architecture:
    controllers/ — HTTP endpoints
    services/    — business logic
    schemas/     — request/response contracts
"""

from app.modules.health.controllers import router

__all__ = ["router"]
