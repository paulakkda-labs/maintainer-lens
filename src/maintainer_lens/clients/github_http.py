"""Create HTTPX clients for authenticated GitHub API requests."""

import httpx
from pydantic import SecretStr

from maintainer_lens.clients.github import GITHUB_API_VERSION


def create_github_http_client(
    token: SecretStr | None = None,
) -> httpx.AsyncClient:
    """Create a reusable GitHub API client with optional authentication."""

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    if token is not None and token.get_secret_value():
        headers["Authorization"] = f"Bearer {token.get_secret_value()}"

    return httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
        ),
        follow_redirects=True,
        headers=headers,
    )
