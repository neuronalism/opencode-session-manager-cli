"""End-to-end test for `ocsm sync project`.

Builds a temporary opencode.db (real schema) + project folder, then exercises:
  1. first sync (no manifest) -> DB -> folder
  2. folder -> DB (new session on folder side)
  3. no-op (in sync)
  4. conflict resolution (--on-conflict newer)
  5. deletion propagation

Run: uv run python tests/test_sync_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Ensure we import the in-repo package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ocsm.cli import (  # noqa: E402
    _apply_sync,
    _compute_sync_diff,
    _load_folder_sessions,
    _load_manifest,
    _refresh_manifest,
    _raw_dir,
    get_session_timestamps,
    _folder_tu,
)

SCHEMA_SESSION = """
CREATE TABLE `session` (
    `id` text PRIMARY KEY,
    `project_id` text NOT NULL,
    `parent_id` text,
    `slug` text NOT NULL,
    `directory` text NOT NULL,
    `title` text NOT NULL,
    `version` text NOT NULL,
    `share_url` text,
    `summary_additions` integer,
    `summary_deletions` integer,
    `summary_files` integer,
    `summary_diffs` text,
    `revert` text,
    `permission` text,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `time_compacting` integer,
    `time_archived` integer,
    `workspace_id` text
)
"""
SCHEMA_MESSAGE = """
CREATE TABLE `message` (
    `id` text PRIMARY KEY,
    `session_id` text NOT NULL,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `data` text NOT NULL
)
"""
SCHEMA_PART = """
CREATE TABLE `part` (
    `id` text PRIMARY KEY,
    `message_id` text NOT NULL,
    `session_id` text NOT NULL,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `data` text NOT NULL
)
"""
SCHEMA_PROJECT = """
CREATE TABLE `project` (
    `id` text PRIMARY KEY,
    `worktree` text NOT NULL,
    `name` text,
    `icon_url` text,
    `icon_color` text,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL
)
"""


def make_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SESSION)
    conn.executescript(SCHEMA_MESSAGE)
    conn.executescript(SCHEMA_PART)
    conn.executescript(SCHEMA_PROJECT)
    conn.commit()
    return conn


def insert_session_row(conn, sid, directory, time_updated, title="t", parent_id=None, summary_diffs=None):
    conn.execute(
        """INSERT INTO session
           (id, project_id, parent_id, slug, directory, title, version,
            time_created, time_updated, summary_diffs)
           VALUES (?, 'global', ?, ?, ?, ?, '1', ?, ?, ?)""",
        (sid, parent_id, sid, directory, title, time_updated, time_updated, summary_diffs),
    )
    conn.commit()


def insert_msg(conn, mid, sid, tu):
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (mid, sid, tu, tu, json.dumps({"role": "user", "text": f"msg-{mid}"})),
    )
    conn.commit()


def write_folder_session(raw_dir: Path, sid, tu, title="t", parent_id=None):
    payload = {
        "session": {
            "id": sid,
            "project_id": "global",
            "parent_id": parent_id,
            "slug": sid,
            "directory": str(raw_dir.parent.parent),  # placeholder, replaced on import
            "title": title,
            "version": "1",
            "time_created": tu,
            "time_updated": tu,
            "summary_diffs": None,
        },
        "messages": [
            {"id": f"{sid}-m1", "session_id": sid, "time_created": tu, "time_updated": tu,
             "data": json.dumps({"role": "assistant", "text": "hi"})}
        ],
        "parts": [],
    }
    target = raw_dir / (parent_id or sid) if parent_id else raw_dir
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{sid}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def gather_db(db_path, directory):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return get_session_timestamps(conn, directory)
    finally:
        conn.close()


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def test_first_sync_db_to_folder(tmp: Path):
    print("\n=== Test 1: first sync, DB has session, folder empty -> push to folder ===")
    tmp.mkdir(parents=True, exist_ok=True)
    db_path = tmp / "a.db"
    proj = tmp / "proj1"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    conn = make_db(db_path)
    insert_session_row(conn, "s1", str(proj), 1000, title="hello")
    insert_msg(conn, "s1-m1", "s1", 1000)
    conn.close()

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_side = {}  # empty
    manifest = _load_manifest(proj)  # None
    check("manifest absent on first sync", manifest is None)

    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("to_folder == [s1]", diff["to_folder"] == ["s1"], diff["to_folder"])
    check("to_db empty", diff["to_db"] == [])
    check("no deletions on first sync", diff["delete_from_folder"] == [] and diff["delete_from_db"] == [])

    result = _apply_sync(db_path, proj, diff, _load_folder_sessions(raw_dir), db_side,
                         substitute=True, do_delete=True)
    check("folder_written has s1", result["folder_written"] == ["s1"], result["folder_written"])
    check("file created", (raw_dir / "s1.json").is_file())

    # refresh manifest
    post_db = gather_db(db_path, directory)
    post_folder_sessions = _load_folder_sessions(raw_dir)
    post_folder = {sid: d["session"]["time_updated"] for sid, d in post_folder_sessions.items()}
    _refresh_manifest(proj, post_db, post_folder, post_folder_sessions)
    m = _load_manifest(proj)
    check("manifest now written", m is not None)
    check("manifest tracks s1", "s1" in (m or {}).get("sessions", {}))


def test_folder_to_db(tmp: Path):
    print("\n=== Test 2: folder has new session, DB lacks it -> pull to DB ===")
    db_path = tmp / "b.db"
    proj = tmp / "proj2"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)
    raw_dir.mkdir(parents=True)

    write_folder_session(raw_dir, "s2", 2000, title="from-folder")

    conn = make_db(db_path)  # empty DB
    conn.close()

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}
    manifest = None
    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("to_db == [s2]", diff["to_db"] == ["s2"], diff["to_db"])
    check("to_folder empty", diff["to_folder"] == [])

    result = _apply_sync(db_path, proj, diff, folder_sessions, db_side,
                         substitute=True, do_delete=True)
    check("db_imported has s2", result["db_imported"] == ["s2"], result["db_imported"])

    # verify DB row
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, directory, project_id, time_updated FROM session WHERE id='s2'").fetchone()
    conn.close()
    check("DB row exists", row is not None)
    if row:
        check("directory rewritten to project path", row["directory"] == str(proj.resolve()), row["directory"])
        check("project_id reset to global", row["project_id"] == "global")


def test_noop_in_sync(tmp: Path):
    print("\n=== Test 3: both sides identical -> no-op ===")
    db_path = tmp / "c.db"
    proj = tmp / "proj3"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    conn = make_db(db_path)
    insert_session_row(conn, "s3", str(proj), 3000, title="same")
    insert_msg(conn, "s3-m1", "s3", 3000)
    conn.close()
    write_folder_session(raw_dir, "s3", 3000, title="same")

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}
    manifest = {"version": 1, "sessions": {"s3": {"db_time_updated": 3000, "folder_time_updated": 3000}}}
    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("same == [s3]", diff["same"] == ["s3"], diff["same"])
    check("no to_folder", diff["to_folder"] == [])
    check("no to_db", diff["to_db"] == [])
    check("no conflicts", diff["conflicts"] == [])
    check("no deletions", diff["delete_from_folder"] == [] and diff["delete_from_db"] == [])


def test_conflict_newer(tmp: Path):
    print("\n=== Test 4: conflict (same id, different time_updated) with newer-wins ===")
    db_path = tmp / "d.db"
    proj = tmp / "proj4"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    # DB version newer (5000) than folder (4000) -> DB wins -> push to folder
    conn = make_db(db_path)
    insert_session_row(conn, "s4", str(proj), 5000, title="db-newer")
    insert_msg(conn, "s4-m1", "s4", 5000)
    conn.close()
    write_folder_session(raw_dir, "s4", 4000, title="folder-older")

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}

    diff = _compute_sync_diff(db_side, folder_side, manifest=None)
    check("conflicts == [s4]", diff["conflicts"] == ["s4"], diff["conflicts"])

    # resolve via newer
    from ocsm.cli import _resolve_conflict
    chosen = _resolve_conflict("s4", db_side.get("s4"), folder_side.get("s4"), "newer")
    check("newer picks db", chosen == "db", chosen)
    if chosen == "db":
        diff["to_folder"].append("s4")
    diff["conflicts"] = []

    result = _apply_sync(db_path, proj, diff, folder_sessions, db_side,
                         substitute=True, do_delete=True)
    check("folder overwritten with DB version", result["folder_written"] == ["s4"])
    # verify folder file now has time_updated 5000
    data = json.loads((raw_dir / "s4.json").read_text(encoding="utf-8"))
    check("folder file time_updated == 5000", data["session"]["time_updated"] == 5000, data["session"]["time_updated"])
    check("folder file title == db-newer", data["session"]["title"] == "db-newer")


def test_deletion_propagation(tmp: Path):
    print("\n=== Test 5: deletion propagation (DB deleted -> remove from folder) ===")
    db_path = tmp / "e.db"
    proj = tmp / "proj5"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    # Both sides have s5; manifest tracks it.
    conn = make_db(db_path)
    insert_session_row(conn, "s5", str(proj), 6000, title="to-be-deleted-on-db")
    insert_msg(conn, "s5-m1", "s5", 6000)
    conn.close()
    write_folder_session(raw_dir, "s5", 6000, title="to-be-deleted-on-db")
    manifest = {"version": 1, "sessions": {"s5": {"db_time_updated": 6000, "folder_time_updated": 6000}}}

    # Now delete from DB.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM message WHERE session_id='s5'")
    conn.execute("DELETE FROM session WHERE id='s5'")
    conn.commit()
    conn.close()

    directory = str(proj)
    db_side = gather_db(db_path, directory)  # empty now
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}

    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("delete_from_folder == [s5]", diff["delete_from_folder"] == ["s5"], diff["delete_from_folder"])
    check("delete_from_db empty", diff["delete_from_db"] == [])

    result = _apply_sync(db_path, proj, diff, folder_sessions, db_side,
                         substitute=True, do_delete=True)
    check("folder file removed", not (raw_dir / "s5.json").exists())
    check("folder_deleted == [s5]", result["folder_deleted"] == ["s5"], result["folder_deleted"])


def test_deletion_db_side(tmp: Path):
    print("\n=== Test 6: deletion propagation (folder deleted -> remove from DB) ===")
    db_path = tmp / "f.db"
    proj = tmp / "proj6"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    conn = make_db(db_path)
    insert_session_row(conn, "s6", str(proj), 7000, title="to-be-deleted-on-folder")
    insert_msg(conn, "s6-m1", "s6", 7000)
    conn.close()
    # folder side: do NOT write s6 -> it's "deleted" relative to manifest
    manifest = {"version": 1, "sessions": {"s6": {"db_time_updated": 7000, "folder_time_updated": 7000}}}

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_side = {}  # folder empty

    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("delete_from_db == [s6]", diff["delete_from_db"] == ["s6"], diff["delete_from_db"])
    check("delete_from_folder empty", diff["delete_from_folder"] == [])

    result = _apply_sync(db_path, proj, diff, {}, db_side,
                         substitute=True, do_delete=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 1 FROM session WHERE id='s6'").fetchone()
    msg_row = conn.execute("SELECT 1 FROM message WHERE session_id='s6'").fetchone()
    conn.close()
    check("DB session row removed", row is None)
    check("DB message rows removed (cascade via delete_session_tree)", msg_row is None)


def test_untracked_not_deleted(tmp: Path):
    print("\n=== Test 7: session only in folder, NOT in manifest -> must NOT be deleted, must be imported ===")
    db_path = tmp / "g.db"
    proj = tmp / "proj7"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)
    raw_dir.mkdir(parents=True)
    write_folder_session(raw_dir, "s7", 8000, title="untracked")

    # manifest tracks a DIFFERENT session that's gone from both -> nothing happens to s7
    manifest = {"version": 1, "sessions": {"sX": {"db_time_updated": 1, "folder_time_updated": 1}}}

    directory = str(proj)
    conn = make_db(db_path)  # empty DB (s7 only on folder side)
    conn.close()
    db_side = gather_db(db_path, directory)
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}

    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("s7 not in any delete list", "s7" not in diff["delete_from_db"] and "s7" not in diff["delete_from_folder"])
    check("s7 in to_db", diff["to_db"] == ["s7"], diff["to_db"])


def test_db_deletion_not_reimported(tmp: Path):
    print("\n=== Test 8: DB-side deletion must NOT be re-imported from folder ===")
    db_path = tmp / "h.db"
    proj = tmp / "proj8"
    proj.mkdir(parents=True, exist_ok=True)
    raw_dir = _raw_dir(proj)

    # Both sides had s8 (tracked by manifest). DB deleted it. Folder still has it.
    conn = make_db(db_path)  # DB empty -> s8 was deleted
    conn.close()
    write_folder_session(raw_dir, "s8", 9000, title="deleted-on-db")
    manifest = {"version": 1, "sessions": {"s8": {"db_time_updated": 9000, "folder_time_updated": 9000}}}

    directory = str(proj)
    db_side = gather_db(db_path, directory)
    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side = {sid: d["session"]["time_updated"] for sid, d in folder_sessions.items()}

    diff = _compute_sync_diff(db_side, folder_side, manifest)
    check("delete_from_folder == [s8]", diff["delete_from_folder"] == ["s8"], diff["delete_from_folder"])
    check("s8 NOT in to_db (must not be re-imported)", "s8" not in diff["to_db"], diff["to_db"])
    check("delete_from_db empty", diff["delete_from_db"] == [])

    result = _apply_sync(db_path, proj, diff, folder_sessions, db_side,
                         substitute=True, do_delete=True)
    check("folder file removed", not (raw_dir / "s8.json").exists())
    check("db_imported empty (not re-imported)", result["db_imported"] == [], result["db_imported"])
    check("folder_deleted == [s8]", result["folder_deleted"] == ["s8"], result["folder_deleted"])


def main():
    root = Path(tempfile.mkdtemp(prefix="ocsm-sync-test-"))
    try:
        test_first_sync_db_to_folder(root / "t1")
        test_folder_to_db(root / "t2")
        test_noop_in_sync(root / "t3")
        test_conflict_newer(root / "t4")
        test_deletion_propagation(root / "t5")
        test_deletion_db_side(root / "t6")
        test_untracked_not_deleted(root / "t7")
        test_db_deletion_not_reimported(root / "t8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{'='*50}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
