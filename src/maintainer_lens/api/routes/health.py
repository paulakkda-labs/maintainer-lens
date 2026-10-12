from fastapi import APIRouter

router = APIRouter(prefix="/health")


@router.get("/live")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
