"""OpenCode v2 storage layer.

Reads and writes ONLY the v2 tables introduced by the OpenCode 2.x storage
rewrite (source of truth: ``opencode-2.2.5/packages/core/src`` —
``session/sql.ts``, ``database/schema.gen.ts``, ``session/projector.ts``,
``session/transfer.ts``, ``bus.ts``). The legacy ``session``/``message``/
``part``/``todo`` tables and the deprecated ``project_directory`` table are
never touched: OpenCode's own runtime ignores them after the one-shot
``v1-migration`` completes, and ocsm does not perform migrations.

Table map used here:

- ``session_v2``       — one row per session (37 columns, v2-only columns
                         ``fork_session_id``/``time_suspended``/``idle_outcome``/...).
- ``session_message``  — messages; ``data`` is the full message object minus
                         ``id``/``type`` (``SessionMessageData``), assistant
                         parts inline in ``data.content``.
- ``session_part``     — forward-compatibility part rows; populated only by
                         newer OpenCode releases. Reads prefer them over the
                         inline content (``session/history.ts`` ``withParts``),
                         so exports merge them back into ``data.content``.
- ``event_sequence``   — per-aggregate durable-event watermark. A session row
                         without its ``event_sequence`` row breaks the next
                         message append (seq collision), so every insert
                         maintains it.
- ``project``/``worktree`` — project registry; ids must match
                         ``Project.resolve`` exactly (see :mod:`ocsm.projectid`).
- ``blob``/``blob_ref`` — deduplicated media store (``experimental.media_blob``,
                         off by default). Exported when messages carry
                         ``media:<sha256>`` references.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

from ocsm.projectid import ResolvedProject, relative_subpath, resolve_project, to_storage_path

SESSION_V2_COLUMNS = [
    "id", "project_id", "workspace_id", "parent_id", "fork_session_id", "fork_boundary",
    "slug", "directory", "path", "title", "version", "share_url",
    "summary_additions", "summary_deletions", "summary_files", "summary_diffs",
    "metadata", "cost", "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "revert", "permission", "agent", "model",
    "time_created", "time_updated", "time_compacting", "time_archived", "time_suspended",
    "resume_attempts", "time_idle", "time_viewed", "idle_outcome", "time_busy",
]
SESSION_MESSAGE_COLUMNS = ["id", "session_id", "type", "seq", "time_created", "time_updated", "data"]
SESSION_PART_COLUMNS = ["message_id", "session_id", "ordinal", "type", "call_id", "time_created", "time_updated", "data"]

#: Tables whose rows are removed by the ``session_v2`` ON DELETE CASCADE
#: (schema.gen.ts FK edges). Used for verification and post-delete checks.
CASCADE_TABLES = [
    "session_message", "session_part", "session_inbox", "blob_ref",
    "instruction_entry", "instruction_state", "schedule", "instruction_blob",
]

_MEDIA_REF = re.compile(r"media:([0-9a-f]{64})")


class OcsmError(Exception):
    """Base error for ocsm storage operations."""


class SessionExistsError(OcsmError):
    def __init__(self, session_id: str):
        super().__init__(f"session already exists: {session_id}")
        self.session_id = session_id


class SessionNotFoundError(OcsmError):
    def __init__(self, session_id: str):
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class ParentNotFoundError(OcsmError):
    def __init__(self, parent_id: str):
        super().__init__(f"parent session not found: {parent_id}")
        self.parent_id = parent_id


def now_ms() -> int:
    return int(time.time() * 1000)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _columns_equal(conn: sqlite3.Connection, table: str, expected: list[str]) -> None:
    """Refuse to operate on a database whose v2 schema is not what we expect."""
    actual = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if not actual:
        raise OcsmError(f"table is missing: {table} (database not migrated to the OpenCode v2 schema?)")
    missing = set(expected) - set(actual)
    if missing:
        raise OcsmError(f"table {table} is missing columns: {sorted(missing)}")


def check_v2_schema(conn: sqlite3.Connection) -> None:
    _columns_equal(conn, "session_v2", SESSION_V2_COLUMNS)
    _columns_equal(conn, "session_message", SESSION_MESSAGE_COLUMNS)


# ---------------------------------------------------------------------------
# reads


def session_row(conn: sqlite3.Connection, session_id: str) -> dict | None:
    cols = ", ".join(SESSION_V2_COLUMNS)
    row = conn.execute(f"SELECT {cols} FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
    return dict(zip(SESSION_V2_COLUMNS, row)) if row else None


def require_session(conn: sqlite3.Connection, session_id: str) -> dict:
    row = session_row(conn, session_id)
    if row is None:
        raise SessionNotFoundError(session_id)
    return row


def list_sessions(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    directory: str | None = None,
    include_archived: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Session rows in OpenCode's list order: ``time_updated DESC, id DESC``
    (``session/store.ts`` list ordering)."""
    cols = ", ".join(SESSION_V2_COLUMNS)
    conditions, params = [], []
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    if directory is not None:
        conditions.append("directory = ?")
        params.append(directory)
    if not include_archived:
        conditions.append("time_archived IS NULL")
    sql = f"SELECT {cols} FROM session_v2"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY time_updated DESC, id DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(zip(SESSION_V2_COLUMNS, row)) for row in conn.execute(sql, params).fetchall()]


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """Project rows with live session counts, most recently active first."""
    sql = """
        SELECT p.id, p.worktree, p.vcs, p.name, p.time_active, p.time_archived,
               (SELECT COUNT(*) FROM session_v2 s WHERE s.project_id = p.id) AS session_count,
               (SELECT MAX(s.time_updated) FROM session_v2 s WHERE s.project_id = p.id) AS latest_updated
        FROM project p
        ORDER BY latest_updated DESC, p.id
    """
    return [dict(row) for row in conn.execute(sql).fetchall()]


def session_messages(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Messages ordered by ``seq`` with ``data`` parsed.

    Assistant messages that already have ``session_part`` rows get their
    inline ``data.content`` replaced by those rows (ordinal order) — the
    ``SessionHistory.withParts`` read path — so exports carry whatever
    OpenCode itself would read back.
    """
    messages = [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "type": row["type"],
            "seq": row["seq"],
            "time_created": row["time_created"],
            "time_updated": row["time_updated"],
            "data": json.loads(row["data"]),
        }
        for row in conn.execute(
            f"SELECT {', '.join(SESSION_MESSAGE_COLUMNS)} FROM session_message"
            " WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    ]
    assistant_ids = [m["id"] for m in messages if m["type"] == "assistant"]
    if not assistant_ids:
        return messages
    placeholders = ",".join("?" * len(assistant_ids))
    parts: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        f"SELECT {', '.join(SESSION_PART_COLUMNS)} FROM session_part"
        f" WHERE message_id IN ({placeholders}) ORDER BY message_id, ordinal",
        assistant_ids,
    ).fetchall():
        parts.setdefault(row["message_id"], []).append(row)
    for message in messages:
        rows = parts.get(message["id"])
        if rows:
            message["data"]["content"] = [json.loads(r["data"]) for r in rows]
    return messages


def latest_todos(messages: list[dict]) -> list[dict]:
    """Latest todo list derived from the most recent ``todowrite`` tool part
    (``tool/plugin/todo.ts`` stores the snapshot in ``state.metadata.todos``;
    v2 has no todo table)."""
    for message in reversed(messages):
        if message["type"] != "assistant":
            continue
        for part in reversed(message["data"].get("content") or []):
            if part.get("type") != "tool" or part.get("name") not in ("todowrite", "todo_write"):
                continue
            todos = ((part.get("state") or {}).get("metadata") or {}).get("todos")
            if isinstance(todos, list):
                return todos
    return []


def message_media_refs(messages: list[dict]) -> set[str]:
    """Blob hashes referenced by the serialized message payloads."""
    refs: set[str] = set()
    for message in messages:
        refs.update(_MEDIA_REF.findall(json.dumps(message["data"], ensure_ascii=False)))
    return refs


def export_blobs(conn: sqlite3.Connection, hashes: list[str]) -> list[dict]:
    """Blob rows (data base64-encoded) for export payloads."""
    if not hashes:
        return []
    placeholders = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"SELECT hash, mime, bytes, width, height, data, time_created FROM blob"
        f" WHERE hash IN ({placeholders}) ORDER BY hash",
        hashes,
    ).fetchall()
    missing = set(hashes) - {r["hash"] for r in rows}
    if missing:
        raise OcsmError(f"referenced blobs missing from the blob table: {sorted(missing)}")
    return [
        {
            "hash": r["hash"], "mime": r["mime"], "bytes": r["bytes"],
            "width": r["width"], "height": r["height"],
            "data": base64.b64encode(r["data"]).decode("ascii"),
            "time_created": r["time_created"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# project registry writes (Project.persist, project.ts:129-236)


def upsert_project(conn: sqlite3.Connection, resolved: ResolvedProject, *, now: int | None = None) -> None:
    """``upsertProject`` (project/sql.ts:49-63): insert the identity row, on
    conflict only reconcile ``vcs``."""
    now = now if now is not None else now_ms()
    worktree = to_storage_path(resolved.canonical)
    conn.execute(
        "INSERT INTO project (id, worktree, vcs, time_created, time_updated, time_active, sandboxes)"
        " VALUES (?, ?, ?, ?, ?, ?, '[]')"
        " ON CONFLICT(id) DO UPDATE SET"
        "  vcs = CASE WHEN ? IS NULL THEN NULL ELSE excluded.vcs END"
        " WHERE (? IS NULL AND project.vcs IS NOT NULL)"
        "    OR (? IS NOT NULL AND (project.vcs IS NULL OR project.vcs != excluded.vcs))",
        (resolved.id, worktree, resolved.vcs_type, now, now, now, resolved.vcs_type, resolved.vcs_type, resolved.vcs_type),
    )


def register_worktree_rows(conn: sqlite3.Connection, resolved: ResolvedProject, *, now: int | None = None) -> None:
    """``Worktree.Event.Resolved`` commit hook (project.ts:224-230): register
    the canonical checkout, plus the linked worktree with strategy ``git``
    when resolving from a non-main checkout."""
    now = now if now is not None else now_ms()
    conn.execute(
        "INSERT OR IGNORE INTO worktree (project_id, directory, strategy, time_created) VALUES (?, ?, NULL, ?)",
        (resolved.id, to_storage_path(resolved.canonical), now),
    )
    if not _same_storage(resolved.canonical, resolved.directory):
        conn.execute(
            "INSERT OR IGNORE INTO worktree (project_id, directory, strategy, time_created) VALUES (?, ?, 'git', ?)",
            (resolved.id, to_storage_path(resolved.directory), now),
        )


def _same_storage(a: str, b: str) -> bool:
    return to_storage_path(a).lower() == to_storage_path(b).lower()


def _ensure_fk_pragma(conn: sqlite3.Connection) -> None:
    """Writes rely on ON DELETE CASCADE and FK enforcement, which SQLite only
    applies per-connection; make that explicit instead of trusting the caller."""
    conn.execute("PRAGMA foreign_keys = ON")


def resolve_and_register(conn: sqlite3.Connection, directory: str | Path) -> ResolvedProject:
    """``Project.resolve`` + ``persist``: derive the identity for ``directory``
    and make sure the ``project``/``worktree`` rows exist."""
    resolved = resolve_project(directory)
    _ensure_fk_pragma(conn)
    with conn:
        upsert_project(conn, resolved)
        register_worktree_rows(conn, resolved)
    return resolved


def placement(resolved: ResolvedProject, directory: str | Path) -> dict:
    """``directory``/``path``/``project_id`` columns for a session placed at
    ``directory`` (session.ts:297-317)."""
    real = os.path.realpath(os.path.abspath(str(directory)))
    return {
        "project_id": resolved.id,
        "directory": to_storage_path(real),
        "path": relative_subpath(resolved.directory, real),
    }


# ---------------------------------------------------------------------------
# writes: import / move / delete


def insert_session_tree(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    resolved: ResolvedProject,
    target_directory: str | Path,
) -> dict:
    """Insert one exported session tree (``session`` + ``messages`` + optional
    ``blobs``) at ``target_directory``, following ``session/transfer.ts``
    import semantics:

    - the session id is preserved as-is; an existing id is a hard conflict;
    - ``project_id``/``directory``/``path`` are recomputed for the destination;
    - message ids and seqs are preserved;
    - an ``event_sequence`` watermark row is created (``max`` message seq, or
      0 for an empty session) so the next append cannot collide — the
      v1 ocsm gap that made imported sessions un-writable, not just invisible.
    """
    check_v2_schema(conn)
    session = dict(payload.get("session") or {})
    unknown = set(session) - set(SESSION_V2_COLUMNS)
    if unknown:
        raise OcsmError(f"payload session has unknown columns: {sorted(unknown)}")
    messages = list(payload.get("messages") or [])
    blobs = list(payload.get("blobs") or [])
    session_id = session.get("id")
    if not session_id:
        raise OcsmError("payload session is missing id")
    if session_row(conn, session_id) is not None:
        raise SessionExistsError(session_id)
    parent_id = session.get("parent_id")
    if parent_id and session_row(conn, parent_id) is None:
        raise ParentNotFoundError(parent_id)

    place = placement(resolved, target_directory)
    session.update(place)

    seen_seqs: set[int] = set()
    prepared_messages = []
    for message in messages:
        for column in ("id", "session_id", "type", "seq", "data"):
            if column not in message:
                raise OcsmError(f"payload message is missing {column}")
        seq = message["seq"]
        if seq in seen_seqs:
            raise OcsmError(f"duplicate message seq {seq} in payload")
        seen_seqs.add(seq)
        prepared_messages.append({
            "id": message["id"],
            "session_id": session_id,
            "type": message["type"],
            "seq": seq,
            "time_created": message.get("time_created") or session["time_created"],
            "time_updated": message.get("time_updated") or session["time_updated"],
            "data": json.dumps(message["data"], ensure_ascii=False, separators=(",", ":")),
        })

    blob_rows = {b["hash"]: b for b in blobs}
    missing = message_media_refs([{"data": json.loads(m["data"])} for m in prepared_messages]) - set(blob_rows)
    if missing:
        # References with no exported blob would dangle in the destination.
        raise OcsmError(f"payload references blobs that were not exported: {sorted(missing)}")

    _ensure_fk_pragma(conn)
    with conn:
        upsert_project(conn, resolved)
        register_worktree_rows(conn, resolved)
        conn.execute(
            f"INSERT INTO session_v2 ({', '.join(session)}) VALUES ({', '.join('?' * len(session))})",
            [session[c] for c in session],
        )
        conn.executemany(
            f"INSERT INTO session_message ({', '.join(SESSION_MESSAGE_COLUMNS)})"
            f" VALUES ({', '.join('?' * len(SESSION_MESSAGE_COLUMNS))})",
            [tuple(m[c] for c in SESSION_MESSAGE_COLUMNS) for m in prepared_messages],
        )
        for blob in blobs:
            conn.execute(
                "INSERT OR IGNORE INTO blob (hash, mime, bytes, width, height, data, time_created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    blob["hash"], blob["mime"], blob["bytes"], blob.get("width"), blob.get("height"),
                    base64.b64decode(blob["data"]), blob.get("time_created") or now_ms(),
                ),
            )
        for message in prepared_messages:
            for ref in _MEDIA_REF.findall(message["data"]):
                conn.execute(
                    "INSERT OR IGNORE INTO blob_ref (session_id, message_id, hash, seq) VALUES (?, ?, ?, ?)",
                    (session_id, message["id"], ref, message["seq"]),
                )
        watermark = max(seen_seqs) if seen_seqs else 0
        conn.execute(
            "INSERT INTO event_sequence (aggregate_id, seq, owner_id) VALUES (?, ?, NULL)"
            " ON CONFLICT(aggregate_id) DO UPDATE SET seq = max(event_sequence.seq, excluded.seq)",
            (session_id, watermark),
        )
    return session_row(conn, session_id) or session


def move_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    resolved: ResolvedProject,
    target_directory: str | Path,
) -> dict:
    """``SessionEvent.Moved`` projection (projector.ts:569-585): only the
    session placement columns change; message content keeps its recorded
    paths, exactly like OpenCode's own move."""
    require_session(conn, session_id)
    place = placement(resolved, target_directory)
    _ensure_fk_pragma(conn)
    with conn:
        upsert_project(conn, resolved)
        register_worktree_rows(conn, resolved)
        conn.execute(
            "UPDATE session_v2 SET project_id = ?, directory = ?, path = ? WHERE id = ?",
            (place["project_id"], place["directory"], place["path"], session_id),
        )
    return require_session(conn, session_id)


def descendant_ids(conn: sqlite3.Connection, session_id: str) -> list[str]:
    """The session plus its recursive ``parent_id`` children (roots first)."""
    ids: list[str] = []
    frontier = [session_id]
    while frontier:
        placeholders = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT id FROM session_v2 WHERE parent_id IN ({placeholders})", frontier
        ).fetchall()
        frontier = [r[0] for r in rows]
        ids.extend(frontier)
    return [session_id, *ids]


#: Backwards-compatible alias for the pre-release private name.
_descendant_ids = descendant_ids


def delete_session_tree_full(conn: sqlite3.Connection, session_id: str, *, recursive: bool = True) -> dict:
    """Full v2 delete, mirroring ``Session.remove`` + ``Bus.remove``:

    1. ``job`` rows for the session (jobs.cancelSession, job.ts:491-500);
    2. ``blob_ref`` rows plus blobs left unreferenced (SessionBlob.releaseSession);
    3. ``event`` + ``event_sequence`` rows (bus.remove);
    4. the ``session_v2`` row itself — child tables go via ON DELETE CASCADE.

    The legacy v1 tables (``session``/``message``/``part``/``todo``) are NOT
    touched; OpenCode's own delete leaves them too.
    """
    require_session(conn, session_id)
    ids = _descendant_ids(conn, session_id) if recursive else [session_id]
    counts = {"sessions": 0, "messages": 0, "parts": 0, "inbox": 0, "events": 0, "jobs": 0}
    _ensure_fk_pragma(conn)
    with conn:
        for sid in ids:
            counts["sessions"] += 1
            counts["messages"] += conn.execute(
                "SELECT COUNT(*) FROM session_message WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            if _table_exists(conn, "session_part"):
                counts["parts"] += conn.execute(
                    "SELECT COUNT(*) FROM session_part WHERE session_id = ?", (sid,)
                ).fetchone()[0]
            if _table_exists(conn, "session_inbox"):
                counts["inbox"] += conn.execute(
                    "SELECT COUNT(*) FROM session_inbox WHERE session_id = ?", (sid,)
                ).fetchone()[0]
            if _table_exists(conn, "blob_ref"):
                conn.execute("DELETE FROM blob_ref WHERE session_id = ?", (sid,))
            if _table_exists(conn, "blob"):
                conn.execute("DELETE FROM blob WHERE hash NOT IN (SELECT hash FROM blob_ref)")
            if _table_exists(conn, "job"):
                cur = conn.execute(
                    "DELETE FROM job WHERE session_id = ? OR child_session_id = ?", (sid, sid)
                )
                counts["jobs"] += cur.rowcount or 0
            counts["events"] += conn.execute(
                "DELETE FROM event WHERE aggregate_id = ?", (sid,)
            ).rowcount or 0
            conn.execute("DELETE FROM event_sequence WHERE aggregate_id = ?", (sid,))
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM session_v2 WHERE id IN ({placeholders})", ids)
    return counts


# ---------------------------------------------------------------------------
# verification & housekeeping


def verify_database(conn: sqlite3.Connection, session_ids: list[str] | None = None) -> list[str]:
    """Post-write sanity checks. Returns a list of problems (empty = healthy)."""
    problems: list[str] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    for row in conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append(f"foreign_key_check: table {row[0]} rowid {row[1]} -> {row[2]}({row[3]})")
    orphans = conn.execute(
        "SELECT COUNT(*) FROM session_message m LEFT JOIN session_v2 s ON s.id = m.session_id"
        " WHERE s.id IS NULL"
    ).fetchone()[0]
    if orphans:
        problems.append(f"{orphans} session_message rows without a session_v2 parent")
    if _table_exists(conn, "session_part"):
        orphans = conn.execute(
            "SELECT COUNT(*) FROM session_part p LEFT JOIN session_message m ON m.id = p.message_id"
            " WHERE m.id IS NULL"
        ).fetchone()[0]
        if orphans:
            problems.append(f"{orphans} session_part rows without a session_message parent")
    for session_id in session_ids or []:
        if session_row(conn, session_id) is None:
            problems.append(f"session {session_id} missing from session_v2")
        elif conn.execute(
            "SELECT 1 FROM event_sequence WHERE aggregate_id = ?", (session_id,)
        ).fetchone() is None:
            problems.append(f"session {session_id} has no event_sequence row")
    return problems


def missing_event_sequences(conn: sqlite3.Connection) -> list[str]:
    """Sessions without their ``event_sequence`` watermark row — the defect
    that silently breaks the next message append in OpenCode."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT s.id FROM session_v2 s"
            " LEFT JOIN event_sequence e ON e.aggregate_id = s.id"
            " WHERE e.aggregate_id IS NULL ORDER BY s.id"
        ).fetchall()
    ]


def checkpoint_and_backup(db_path: Path) -> Path:
    """Fold the WAL into the main file, then take a timestamped file backup.
    Only ever call this on a working copy the user owns — never on the live
    OpenCode database."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak.{stamp}")
    shutil.copy2(db_path, backup)
    return backup
