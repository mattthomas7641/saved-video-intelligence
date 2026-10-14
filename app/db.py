"""Database engine/session helpers."""
from sqlalchemy import event
from sqlmodel import Session, create_engine

from app.config import BASE_DIR, DB_PATH

# Several worker threads write concurrently; wait for locks instead of failing.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Wait out short write locks (the dashboard, the research lane and background
    rescans share this file; each commits promptly). Deliberately NOT WAL: WAL's
    shared-memory index doesn't work across Docker Desktop's file sharing, so opening
    the database from the host while the container runs can delete the live WAL file
    and silently lose the container's writes (this happened, 2026-10-11)."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=DELETE")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


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
