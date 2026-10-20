"""Central registration of application API routes."""

from fastapi import APIRouter

from maintainer_lens.api.routes.health import router as health_router
from maintainer_lens.api.routes.investigations import (
    router as investigations_router,
)

router = APIRouter()

router.include_router(health_router)
router.include_router(investigations_router)
