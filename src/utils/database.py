"""MongoDB connection lifecycle."""
from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from src.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_database: AsyncIOMotorDatabase | None = None


async def get_db() -> AsyncIOMotorDatabase:
    """Return initialized MongoDB database connection."""
    global _client, _database

    if _client is None:
        _client = AsyncIOMotorClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5_000,
        )

    if _database is None:
        await _client.admin.command("ping")
        _database = _client[settings.mongo_db]

        await _database.users.create_index("telegram_id", unique=True)
        await _database.users.create_index("monitoring_active")

        logger.info("MongoDB connected")

    return _database


async def is_db_available() -> bool:
    """Return whether MongoDB can be reached."""
    try:
        await get_db()
        return True
    except Exception:
        logger.exception("MongoDB unavailable")
        return False


async def close_db() -> None:
    """Close global MongoDB client."""
    global _client, _database

    if _client is not None:
        _client.close()

    _client = None
    _database = None
