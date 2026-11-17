"""Database engine and sessions (SQLAlchemy 2.0, PostgreSQL)."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init_engine(url: str, serverless: bool = False):
    """Create the engine and all tables (idempotent)."""
    global engine
    kwargs = {"pool_pre_ping": True, "future": True}
    if serverless:
        # small pool, recycle idle connections, and no server-side prepared statements
        # (works with Neon's pooled / PgBouncer connection string)
        kwargs.update(pool_size=2, max_overflow=2, pool_recycle=300,
                      connect_args={"prepare_threshold": None})
    engine = create_engine(url, **kwargs)
    SessionLocal.configure(bind=engine)
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    return engine


def drop_all():
    from . import models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
