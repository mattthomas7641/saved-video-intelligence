"""Database engine/session helpers."""
from sqlmodel import Session, SQLModel, create_engine

from app.config import DB_PATH

# Several worker threads write concurrently; wait for locks instead of failing.
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30})

_ADDED_COLUMNS = [
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cost_usd", "REAL"),
    ("batch_id", "VARCHAR"),
    ("user_note", "TEXT"),
]


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    # create_all never alters existing tables, so add newer columns by hand.
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(video)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE video ADD COLUMN {name} {ddl}")
        conn.commit()


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
