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
    return Session(engine)
