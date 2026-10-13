"""Health check endpoints for the application."""

from fastapi import APIRouter

router = APIRouter(prefix="/health")


@router.get("/live")
async def health_check() -> dict[str, str]:
    """Return the application's liveness status."""
    return {"status": "ok"}
