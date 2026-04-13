from __future__ import annotations

import json
import sqlite3


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            directory,
            project_id,
            COUNT(*) as session_count,
            MAX(time_updated) as latest_updated
        FROM session
        GROUP BY directory, project_id
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
        resolved = str(__import__("pathlib").Path(project).expanduser().resolve())
        if where:
            where += " AND"
        else:
            where = "WHERE"
        where += " (directory = ? OR directory = ?)"
        params = [resolved, project]
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
        SELECT id, session_id, message_id, data
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
        SELECT id, session_id, message_id, data
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
