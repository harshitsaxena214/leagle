from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.engine.url import make_url
from core.config import settings
import logging

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models"""
    pass


def get_async_database_url(url_str: str) -> str:
    """
    Normalize database URLs for SQLAlchemy + psycopg/aiosqlite.
    Handles Render's postgres:// and Neon's sslmode=require.
    """
    if not url_str:
        raise ValueError("DATABASE_URL is not set or empty")
    
    parsed_url = make_url(url_str)
    
    # Map synchronous schemes to async dialects
    if parsed_url.drivername in ("postgres", "postgresql"):
        parsed_url = parsed_url.set(drivername="postgresql+psycopg")
    elif parsed_url.drivername == "sqlite":
        parsed_url = parsed_url.set(drivername="sqlite+aiosqlite")
    elif parsed_url.drivername not in ("postgresql+psycopg", "sqlite+aiosqlite"):
        raise ValueError(f"Unsupported database scheme: {parsed_url.drivername}")
        
    return parsed_url.render_as_string(hide_password=False)


def get_masked_url(url_str: str) -> str:
    """Return a safely masked database URL for logging."""
    if not url_str:
        return ""
    try:
        return make_url(url_str).render_as_string(hide_password=True)
    except Exception:
        return "***"


normalized_database_url = get_async_database_url(settings.database_url)


# Create async engine with proper configuration for Neon/PostgreSQL
# Note: psycopg driver is required for async operations on Neon
logger.info(f"Connecting to database at {get_masked_url(normalized_database_url)}")

if normalized_database_url.startswith("sqlite"):
    connect_args = {"timeout": 10}
else:
    connect_args = {"connect_timeout": 10}

engine = create_async_engine(
    normalized_database_url,
    echo=False,  # Set to True for SQL logging in debug
    pool_size=5,  # Small pool for Neon free tier
    max_overflow=10,
    pool_pre_ping=True,  # Detect stale connections
    pool_recycle=3600,  # Recycle connections every hour
    connect_args=connect_args,  # Prevent infinite hangs connecting
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncSession:
    """
    FastAPI dependency to inject an async database session.
    Usage in route handlers:
        async def my_route(db: AsyncSession = Depends(get_db)):
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


import asyncio

async def create_tables():
    """
    Create all tables in the database.
    Call this during application startup.
    """
    try:
        logger.info("DB init started...")
        async def _do_create():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                
        await asyncio.wait_for(_do_create(), timeout=20.0)
        logger.info("✅ DB init finished: tables created/verified")
    except Exception as e:
        logger.error(f"⚠️ DB init failed: Could not create database tables on startup: {e}")
        logger.warning("⚠️ If database is not available, table creation will be deferred")
        # Don't re-raise - allow app to start even if DB is unavailable


async def init_db():
    """Initialize database on application startup"""
    await create_tables()
