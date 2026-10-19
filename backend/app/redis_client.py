from redis.asyncio import Redis

from app.config import get_settings

settings = get_settings()
redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)


async def check_redis() -> bool:
    try:
        return bool(await redis.ping())
    except Exception:
        return False
