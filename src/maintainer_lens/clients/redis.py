"""Asynchronous Redis client configuration."""

from redis.asyncio import Redis


def create_redis_client(redis_url: str) -> Redis:
    """Create an asynchronous Redis client with connection pooling."""
    client = Redis.from_url(
        redis_url,
        decode_responses=True,
    )
    return client
