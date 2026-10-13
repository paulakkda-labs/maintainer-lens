"""Tests for application health check endpoints."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from maintainer_lens.api.router import router as api_router


def test_health_liveness() -> None:
    """Verify that the liveness endpoint returns a successful response."""
    test_app = FastAPI()
    test_app.include_router(api_router)

    with TestClient(test_app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
