"""Serve the two Web entry points and compiled assets without API side effects."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def configure_web(app: FastAPI, directory: Path | None = None) -> None:
    """Docker and source-checkout runs build assets into WORKDIR/web/dist."""
    root = directory if directory is not None else Path.cwd() / "web" / "dist"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="web-assets")

    async def index() -> FileResponse:
        if not (root / "index.html").is_file():
            raise HTTPException(status_code=503, detail="Web interface is unavailable")
        return FileResponse(
            root / "index.html",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
            },
        )

    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/logs", index, methods=["GET"], include_in_schema=False)

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        if not (root / "favicon.svg").is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(root / "favicon.svg", media_type="image/svg+xml")
