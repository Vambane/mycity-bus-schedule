"""FastAPI dependencies for database connections and caching."""
import duckdb
from cachetools import TTLCache
from fastapi import HTTPException
import logging
from pathlib import Path
from .config import settings

logger = logging.getLogger(__name__)

# Connection pool (list of read-only DuckDB connections)
_connection_pool: list[duckdb.DuckDBPyConnection] = []

# In-memory cache with TTL
_cache = TTLCache(maxsize=100, ttl=settings.cache_ttl)

SNAPSHOT_DIR = Path(settings.db_path).parent / "snapshot"


def _ensure_database() -> None:
    """Rebuild the DB from Parquet snapshots if it doesn't exist, then run migrations."""
    db_path = Path(settings.db_path)

    if not db_path.exists():
        snapshots = sorted(SNAPSHOT_DIR.glob("*.parquet"))
        if not snapshots:
            raise FileNotFoundError(
                f"Database not found at {db_path} and no snapshot in {SNAPSHOT_DIR}."
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Building database from Parquet snapshots...")
        con = duckdb.connect(str(db_path))
        try:
            for pq in snapshots:
                con.execute(
                    f"CREATE TABLE IF NOT EXISTS {pq.stem} AS "
                    "SELECT * FROM read_parquet(?)",
                    [str(pq)],
                )
        finally:
            con.close()
        logger.info("Database rebuilt from snapshots.")

    # Run idempotent migrations so older databases gain new columns.
    from etl.load_db import MIGRATIONS
    con = duckdb.connect(str(db_path))
    try:
        for stmt in MIGRATIONS:
            try:
                con.execute(stmt)
            except duckdb.CatalogException:
                pass  # Table doesn't exist yet — safe to skip
        logger.info("Schema migrations applied.")
    finally:
        con.close()


def init_connection_pool() -> None:
    """Initialize DuckDB connection pool at startup."""
    global _connection_pool
    try:
        _ensure_database()
        logger.info(f"Initializing connection pool with {settings.connection_pool_size} connections...")
        for i in range(settings.connection_pool_size):
            conn = duckdb.connect(settings.db_path, read_only=True)
            _connection_pool.append(conn)
        logger.info(f"Connection pool initialized with {len(_connection_pool)} connections")
    except Exception as e:
        logger.error(f"Failed to initialize connection pool: {e}")
        raise


def close_connection_pool() -> None:
    """Close all connections in the pool at shutdown."""
    global _connection_pool
    logger.info("Closing connection pool...")
    for conn in _connection_pool:
        try:
            conn.close()
        except Exception as e:
            logger.warning(f"Error closing connection: {e}")
    _connection_pool.clear()
    logger.info("Connection pool closed")


def get_connection() -> duckdb.DuckDBPyConnection:
    """
    Get a connection from the pool.

    Returns:
        DuckDB connection from the pool

    Raises:
        HTTPException: If connection pool is not initialized
    """
    if not _connection_pool:
        logger.error("Connection pool not initialized")
        raise HTTPException(status_code=500, detail="Database connection pool not available")

    # Simple approach: return first connection (all are read-only and thread-safe for reads)
    return _connection_pool[0]


def get_cache() -> TTLCache:
    """Get the application cache."""
    return _cache


def cache_key(*args, **kwargs) -> str:
    """Generate a cache key from function arguments."""
    key_parts = [str(arg) for arg in args]
    key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return ":".join(key_parts)
