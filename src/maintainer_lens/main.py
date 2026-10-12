from fastapi import FastAPI

from maintainer_lens.api.routes.health import router as health_router

app = FastAPI(title="MaintainerLens")

app.include_router(health_router)
