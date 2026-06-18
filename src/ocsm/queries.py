from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _normalize_directory(path: str) -> str:
    """Normalize a directory path for cross-platform comparison.

    OpenCode stores project paths with forward slashes even on Windows
    (e.g. ``D:/Documents/proj``), but ``Path.resolve()`` on Windows returns
    backslashes (``D:\\Documents\\proj``). Comparing these verbatim fails.
    We normalize both sides to forward slashes, lower-cased drive letters, so
    a session's ``directory`` column matches regardless of which form the
    caller passed in. Case sensitivity outside the drive letter is preserved
    (project folder names can differ only by case).
    """
    p = Path(path).expanduser()
    # Resolve without requiring the path to exist on disk (opencode may point at a
    # folder that was moved/deleted). Use absolute() so a relative input is still
    # anchored, but avoid resolve() which forces OS-native separators.
    if not p.is_absolute():
        p = (Path.cwd() / p)
    s = str(p)
    norm = s.replace("\\", "/")
    # Normalize drive letter casing (Windows): "D:/" vs "d:/"
    if len(norm) >= 2 and norm[1] == ":":
        norm = norm[0].upper() + norm[1:]
    return norm


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            s.directory,
            s.project_id,
            p.name as project_name,
            COUNT(*) as session_count,
            MAX(s.time_updated) as latest_updated
        FROM session s
        LEFT JOIN project p ON p.worktree = s.directory
        GROUP BY s.directory, s.project_id
        ORDER BY latest_updated DESC
        """
    ).fetchall()


def list_sessions(
    conn: sqlite3.Connection, project: str | None = None, *, include_children: bool = False
) -> list[sqlite3.Row]:
    where = ""
    params: list = []
    if not include_children:
        where = "WHERE parent_id IS NULL"
    if project:
        # Normalize both the stored value and the candidate to forward slashes so a
        # session stored as 'D:/Documents/proj' matches a candidate resolved to
        # 'D:\\Documents\\proj'. Also fall back to the raw candidate string.
        resolved = str(__import__("pathlib").Path(project).expanduser().resolve())
        normalized = _normalize_directory(project)
        if where:
            where += " AND"
        else:
            where = "WHERE"
        where += " (REPLACE(directory, '\\', '/') = ? OR directory = ? OR directory = ?)"
        params = [normalized, resolved, project]
    return conn.execute(
        f"""
        SELECT * FROM session
        {where}
        ORDER BY time_updated DESC
        """,
        params,
    ).fetchall()


def get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM session WHERE id = ?",
        (session_id,),
    ).fetchone()


def get_session_timestamps(conn: sqlite3.Connection, directory: str) -> dict[str, int]:
    """Return {session_id: time_updated} for all sessions in a project directory.

    Matching mirrors list_sessions(): forward-slash normalization handles the
    OpenCode-on-Windows storage style (forward slashes) vs Path.resolve() output
    (backslashes). Used to build the DB-side index for sync diffing without
    loading full message/part rows.
    """
    resolved = str(Path(directory).expanduser().resolve())
    normalized = _normalize_directory(directory)
    rows = conn.execute(
        """
        SELECT id, time_updated FROM session
        WHERE REPLACE(directory, '\\', '/') = ? OR directory = ? OR directory = ?
        """,
        (normalized, resolved, directory),
    ).fetchall()
    return {row["id"]: row["time_updated"] for row in rows}


def session_info(row: sqlite3.Row) -> dict:
    out: dict = {
        "id": row["id"],
        "slug": row["slug"],
        "projectID": row["project_id"],
        "workspaceID": row["workspace_id"],
        "directory": row["directory"],
        "parentID": row["parent_id"],
        "title": row["title"],
        "version": row["version"],
        "time": {
            "created": row["time_created"],
            "updated": row["time_updated"],
            "compacting": row["time_compacting"],
            "archived": row["time_archived"],
        },
    }
    if row["share_url"]:
        out["share"] = {"url": row["share_url"]}
    if row["summary_additions"] is not None or row["summary_deletions"] is not None or row["summary_files"] is not None:
        out["summary"] = {
            "additions": row["summary_additions"] or 0,
            "deletions": row["summary_deletions"] or 0,
            "files": row["summary_files"] or 0,
            "diffs": json.loads(row["summary_diffs"]) if row["summary_diffs"] else None,
        }
    if row["revert"]:
        out["revert"] = json.loads(row["revert"])
    if row["permission"]:
        out["permission"] = json.loads(row["permission"])
    for key in ("workspaceID", "parentID"):
        if out[key] is None:
            out.pop(key)
    for key in ("compacting", "archived"):
        if out["time"][key] is None:
            out["time"].pop(key)
    return out


def load_messages(conn: sqlite3.Connection, session_id: str) -> list:
    msgs = conn.execute(
        """
        SELECT id, session_id, time_created, time_updated, data
        FROM message
        WHERE session_id = ?
        ORDER BY time_created ASC
        """,
        (session_id,),
    ).fetchall()

    parts = conn.execute(
        """
        SELECT id, session_id, message_id, time_created, time_updated, data
        FROM part
        WHERE session_id = ?
        ORDER BY time_created ASC
        """,
        (session_id,),
    ).fetchall()

    part_map: dict[str, list] = {}
    for row in parts:
        data = json.loads(row["data"])
        data["id"] = row["id"]
        data["sessionID"] = row["session_id"]
        data["messageID"] = row["message_id"]
        part_map.setdefault(row["message_id"], []).append(data)

    out = []
    for row in msgs:
        data = json.loads(row["data"])
        data["id"] = row["id"]
        data["sessionID"] = row["session_id"]
        if "time" not in data or not isinstance(data["time"], dict):
            data["time"] = {}
        if "created" not in data["time"]:
            data["time"]["created"] = row["time_created"]
        if "updated" not in data["time"]:
            data["time"]["updated"] = row["time_updated"]
        out.append({"info": data, "parts": part_map.get(row["id"], [])})
    return out


def load_raw_messages(conn: sqlite3.Connection, session_id: str) -> dict:
    """Load messages and parts as raw row dicts, preserving original data strings."""
    msgs = conn.execute(
        """
        SELECT id, session_id, time_created, time_updated, data
        FROM message
        WHERE session_id = ?
        ORDER BY time_created ASC
        """,
        (session_id,),
    ).fetchall()

    parts = conn.execute(
        """
        SELECT id, session_id, message_id, time_created, time_updated, data
        FROM part
        WHERE session_id = ?
        ORDER BY time_created ASC
        """,
        (session_id,),
    ).fetchall()

    return {
        "messages": [dict(row) for row in msgs],
        "parts": [dict(row) for row in parts],
    }


def get_session_tree(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Return the root session and all its descendants, breadth-first."""
    all_ids: list[str] = [session_id]
    rows = [get_session(conn, session_id)]
    i = 0
    while i < len(all_ids):
        children = conn.execute(
            "SELECT * FROM session WHERE parent_id = ?",
            (all_ids[i],),
        ).fetchall()
        for child in children:
            all_ids.append(child["id"])
            rows.append(child)
        i += 1
    return [r for r in rows if r is not None]


# --- Import functions ---

SESSION_COLUMNS = [
    "id", "project_id", "parent_id", "slug", "directory", "title", "version",
    "share_url", "summary_additions", "summary_deletions", "summary_files",
    "summary_diffs", "revert", "permission",
    "time_created", "time_updated", "time_compacting", "time_archived",
    "workspace_id",
]

MESSAGE_COLUMNS = ["id", "session_id", "time_created", "time_updated", "data"]

PART_COLUMNS = ["id", "message_id", "session_id", "time_created", "time_updated", "data"]


def session_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM session WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def insert_session(conn: sqlite3.Connection, session_dict: dict) -> None:
    cols = [c for c in SESSION_COLUMNS if c in session_dict]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    values = [session_dict[c] for c in cols]
    conn.execute(f"INSERT INTO session ({col_names}) VALUES ({placeholders})", values)


def insert_messages(conn: sqlite3.Connection, messages: list[dict]) -> int:
    if not messages:
        return 0
    cols = [c for c in MESSAGE_COLUMNS if c in messages[0]]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    rows = []
    for m in messages:
        rows.append(tuple(m[c] for c in cols))
    conn.executemany(f"INSERT INTO message ({col_names}) VALUES ({placeholders})", rows)
    return len(rows)


def insert_parts(conn: sqlite3.Connection, parts: list[dict], messages: list[dict] | None = None) -> int:
    """Insert parts. If parts lack time_created/time_updated (old exports), derive from messages."""
    if not parts:
        return 0
    # Build message time lookup for fallback
    msg_times: dict[str, tuple[int, int]] = {}
    if messages:
        for m in messages:
            tc = m.get("time_created", 0) or 0
            tu = m.get("time_updated", 0) or 0
            msg_times[m["id"]] = (tc, tu)
    cols = [c for c in PART_COLUMNS if c in parts[0] or c in ("time_created", "time_updated")]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    rows = []
    for p in parts:
        row = []
        for c in cols:
            if c in p:
                row.append(p[c])
            elif c in ("time_created", "time_updated"):
                fallback = msg_times.get(p.get("message_id", ""), (0, 0))
                row.append(fallback[0] if c == "time_created" else fallback[1])
            else:
                row.append(None)
        rows.append(tuple(row))
    conn.executemany(f"INSERT INTO part ({col_names}) VALUES ({placeholders})", rows)
    return len(rows)


def validate_session_tree(conn: sqlite3.Connection, root_id: str) -> list[str]:
    """Verify root session and all its children exist in DB. Returns list of session IDs."""
    all_ids: list[str] = [root_id]
    i = 0
    while i < len(all_ids):
        children = conn.execute(
            "SELECT id FROM session WHERE parent_id = ?", (all_ids[i],)
        ).fetchall()
        for child in children:
            all_ids.append(child["id"])
        i += 1
    # Verify all exist
    missing = []
    for sid in all_ids:
        if not session_exists(conn, sid):
            missing.append(sid)
    if missing:
        raise ValueError(f"Broken tree: sessions missing in DB: {missing}")
    return all_ids


def substitute_paths(conn: sqlite3.Connection, session_ids: list[str], old_path: str, new_path: str) -> dict[str, int]:
    """Replace old_path with new_path in data fields of imported sessions.

    Handles both raw paths (backslashes) and JSON-escaped paths (double backslashes)
    since data columns store JSON strings where backslashes are escaped.

    Returns dict mapping session_id -> number of rows affected.
    """
    if not session_ids or old_path == new_path:
        return {}
    placeholders = ", ".join(["?"] * len(session_ids))
    old_json = old_path.replace("\\", "\\\\")
    new_json = new_path.replace("\\", "\\\\")
    # Count affected rows per session_id before replacement
    counts: dict[str, int] = {sid: 0 for sid in session_ids}
    id_col_map = {"message": "session_id", "part": "session_id", "session": "id"}
    search_patterns = [f"%{old_path}%"]
    if old_json != old_path:
        search_patterns.append(f"%{old_json}%")
    for table, col in [("message", "data"), ("part", "data"), ("session", "summary_diffs")]:
        id_col = id_col_map[table]
        for pattern in search_patterns:
            for row in conn.execute(
                f"SELECT {id_col}, COUNT(*) FROM {table} WHERE {col} LIKE ? AND {id_col} IN ({placeholders}) GROUP BY {id_col}",
                [pattern] + session_ids,
            ):
                counts[row[0]] += row[1]
    for table, col, id_col in [("message", "data", "session_id"), ("part", "data", "session_id"), ("session", "summary_diffs", "id")]:
        conn.execute(
            f"UPDATE {table} SET {col} = REPLACE({col}, ?, ?) WHERE {id_col} IN ({placeholders})",
            [old_path, new_path] + session_ids,
        )
        if old_json != old_path:
            conn.execute(
                f"UPDATE {table} SET {col} = REPLACE({col}, ?, ?) WHERE {id_col} IN ({placeholders})",
                [old_json, new_json] + session_ids,
            )
    return {sid: c for sid, c in counts.items() if c > 0}


def update_session_directory(
    conn: sqlite3.Connection, session_ids: list[str], old_path: str, new_path: str
) -> int:
    """Update session.directory from old_path to new_path for the given session IDs."""
    if not session_ids or old_path == new_path:
        return 0
    placeholders = ", ".join(["?"] * len(session_ids))
    cursor = conn.execute(
        f"UPDATE session SET directory = ? WHERE directory = ? AND id IN ({placeholders})",
        [new_path, old_path] + session_ids,
    )
    return cursor.rowcount


def update_project_worktree(conn: sqlite3.Connection, old_path: str, new_path: str) -> int:
    """Update project.worktree from old_path to new_path.

    The project table stores icon_url (base64 data URL) and icon_color, keyed by
    worktree (the project directory).  Moving sessions without updating this table
    causes project icons to disappear in the OpenCode web UI.
    """
    if old_path == new_path:
        return 0
    # Check if the project table exists (older DBs may not have it)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "project" not in tables:
        return 0
    cursor = conn.execute(
        "UPDATE project SET worktree = ? WHERE worktree = ?",
        [new_path, old_path],
    )
    return cursor.rowcount


def reset_project_id_to_global(
    conn: sqlite3.Connection, session_ids: list[str]
) -> int:
    """Reset project_id to 'global' for the given sessions.

    Used during move/import so OpenCode can re-assign the correct ID on startup.
    """
    if not session_ids:
        return 0
    placeholders = ", ".join(["?"] * len(session_ids))
    cursor = conn.execute(
        f"UPDATE session SET project_id = 'global' WHERE id IN ({placeholders})",
        session_ids,
    )
    return cursor.rowcount


def delete_session_tree(conn: sqlite3.Connection, session_ids: list[str]) -> int:
    """Delete the part / message / session rows for the given session IDs.

    Used by sync for (a) replacing an existing session in place (DELETE + INSERT)
    and (b) propagating deletions to the DB side.  Caller passes the full id set
    (root + descendants) so whole trees can be removed without leaving orphans.
    Returns the number of session rows deleted.
    """
    if not session_ids:
        return 0
    placeholders = ", ".join(["?"] * len(session_ids))
    conn.execute(f"DELETE FROM part WHERE session_id IN ({placeholders})", session_ids)
    conn.execute(f"DELETE FROM message WHERE session_id IN ({placeholders})", session_ids)
    cursor = conn.execute(f"DELETE FROM session WHERE id IN ({placeholders})", session_ids)
    return cursor.rowcount


def replace_session(
    conn: sqlite3.Connection,
    session_dict: dict,
    messages: list[dict],
    parts: list[dict],
) -> None:
    """Replace a session (and its messages/parts) in place: DELETE then INSERT.

    Distinct from insert_session(): the latter is used by `import` which skips
    existing IDs.  Sync needs to overwrite when the folder copy is newer, so we
    delete the existing rows first (if any) and re-insert.  Safe to call when no
    prior row exists — delete_session_tree() is a no-op on an empty set.
    """
    delete_session_tree(conn, [session_dict["id"]])
    insert_session(conn, session_dict)
    insert_messages(conn, messages)
    insert_parts(conn, parts, messages=messages)
