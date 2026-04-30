"""Database setup with aiosqlite for async SQLite operations."""

import aiosqlite
import os
import logging

logger = logging.getLogger(__name__)

DB_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.path.join(DB_DIR, "panel.db")


async def get_db() -> aiosqlite.Connection:
    """Get a new database connection. Caller MUST close it or use `async with db_connection()`."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


class db_connection:
    """Async context manager for database connections.

    Usage:
        async with db_connection() as db:
            await db.execute(...)
    """

    def __init__(self):
        self.db = None

    async def __aenter__(self) -> aiosqlite.Connection:
        self.db = await get_db()
        return self.db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.db:
            await self.db.close()
        return False  # Don't suppress exceptions


async def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    async with db_connection() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL,
                token TEXT NOT NULL,
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                sim_slot TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL CHECK(direction IN ('sent','received')),
                raw_json TEXT DEFAULT '',
                sms_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sms_ts INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sms_device ON sms(device_id);
            CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms(phone);
            CREATE INDEX IF NOT EXISTS idx_sms_time ON sms(sms_time DESC);
            CREATE INDEX IF NOT EXISTS idx_sms_direction ON sms(direction);

            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                sim_slot TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
                action TEXT NOT NULL DEFAULT '',
                duration INTEGER DEFAULT 0,
                raw_json TEXT DEFAULT '',
                call_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id)
            );

            CREATE INDEX IF NOT EXISTS idx_call_device ON call_logs(device_id);
            CREATE INDEX IF NOT EXISTS idx_call_time ON call_logs(call_time DESC);

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
        """)
        # Migration: add sms_ts column if missing (for older DBs)
        try:
            await db.execute("ALTER TABLE sms ADD COLUMN sms_ts INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.commit()
        logger.info("Database initialized at %s", DB_PATH)
