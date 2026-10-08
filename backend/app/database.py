from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

engine = create_async_engine(settings.postgres_url)
redis_client = Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)
