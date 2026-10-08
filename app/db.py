"""Database engine/session helpers."""
from sqlmodel import Session, create_engine

from app.config import BASE_DIR, DB_PATH

# Several worker threads write concurrently; wait for locks instead of failing.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30})


def init_db() -> None:
    """Brings the schema up to date via the Alembic migrations in alembic/versions/
    (replaces the old hand-rolled ALTER TABLE list). `alembic upgrade head` is
    idempotent and handles both cases: a brand-new DB gets created from the
    baseline migration; one already at head (tracked via its alembic_version
    table) is a no-op."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    command.upgrade(cfg, "head")


def get_session() -> Session:
    """For non-request contexts with no FastAPI request lifecycle to hook a
    dependency into - the worker thread pool, the batch poller, one-off
    scripts. Request handlers should prefer get_db() instead."""
    return Session(engine)


def get_db():
    """FastAPI dependency: `session: Session = Depends(get_db)`. Opens one
    session per request and guarantees it's closed afterward, replacing the
    manual get_session()/try/finally boilerplate every route used to repeat."""
    session = get_session()
    try:
        yield session
    finally:
        session.close()
