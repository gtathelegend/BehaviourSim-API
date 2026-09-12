"""Database engine, session factory, and FastAPI request-scoped session dependency."""

from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return or initialize SQLAlchemy engine lazily without connecting immediately."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        engine_kwargs = {"pool_pre_ping": True}

        if settings.DATABASE_URL.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        else:
            engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
            engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
            engine_kwargs["pool_recycle"] = settings.DB_POOL_RECYCLE

        _engine = create_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            **engine_kwargs,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return or initialize SQLAlchemy sessionmaker."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped database session."""
    factory = get_session_factory()
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()
