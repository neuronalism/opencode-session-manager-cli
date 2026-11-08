from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_db_path() -> Path:
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg / "opencode" / "opencode.db"


def resolve_db_path(path: Path | None = None) -> Path:
    if path is None:
        path = default_db_path()
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Database not found: {resolved}")
    return resolved


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # The v2 schema relies on ON DELETE CASCADE from session_v2 to its child
    # tables; SQLite only enforces foreign keys when this pragma is on.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
