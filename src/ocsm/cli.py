from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from ocsm.db import get_connection, resolve_db_path
from ocsm.format import format_projects_list, format_sessions_list, format_sessions_tree, format_timestamp, session_to_markdown, session_to_raw_json
from ocsm.queries import (
    delete_session_tree,
    get_session,
    get_session_timestamps,
    get_session_tree,
    insert_messages,
    insert_parts,
    insert_session,
    list_projects,
    list_sessions,
    load_messages,
    load_raw_messages,
    replace_session,
    reset_project_id_to_global,
    session_exists,
    substitute_paths,
    update_project_worktree,
    update_session_directory,
    validate_session_tree,
)

app = typer.Typer(name="ocsm", help="OpenCode Sessions Manager", no_args_is_help=True)

list_app = typer.Typer(name="list", help="Show projects and sessions stored in the database", no_args_is_help=True)
app.add_typer(list_app)

export_app = typer.Typer(name="export", help="Export sessions as markdown or raw JSON", no_args_is_help=True)
app.add_typer(export_app)

import_app = typer.Typer(name="import", help="Import sessions from raw JSON into the database", no_args_is_help=True)
app.add_typer(import_app)

move_app = typer.Typer(name="move", help="Update project paths after renaming or moving a folder", no_args_is_help=True)
app.add_typer(move_app)

sync_app = typer.Typer(name="sync", help="Two-way sync (incl. deletions) between DB and a project folder", no_args_is_help=True)
app.add_typer(sync_app)

# Manifest constants for deletion propagation
MANIFEST_VERSION = 1
MANIFEST_FILENAME = ".ocsm-sync.json"
RAW_SUBDIR = "raw_conversations"


def _make_console() -> Console:
    if hasattr(sys.stdout, "buffer"):
        out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        err = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        return Console(file=out, stderr=err, legacy_windows=False)
    return Console(legacy_windows=False)


console = _make_console()


@app.callback()
def main(
    ctx: typer.Context,
    db_path: Path = typer.Option(
        None,
        "--db",
        "-d",
        envvar="OCSM_DB_PATH",
        help="Path to opencode.db (default: $XDG_DATA_HOME/opencode/opencode.db)",
    ),
):
    ctx.ensure_object(dict)
    try:
        ctx.obj["db_path"] = resolve_db_path(db_path)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@list_app.command("project")
def list_projects_cmd(ctx: typer.Context):
    """List all unique projects."""
    db_path = ctx.obj["db_path"]
    conn = get_connection(db_path)
    try:
        rows = list_projects(conn)
    finally:
        conn.close()
    if not rows:
        console.print("No projects found.")
        return
    console.print(format_projects_list(rows))


@list_app.command("session")
def list_sessions_cmd(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help="Filter by project directory"),
    flat: bool = typer.Option(False, "--flat", help="Include subagent sessions"),
    tree: bool = typer.Option(False, "--tree", help="Show sessions in tree structure"),
):
    """List sessions, optionally filtered by project."""
    if flat and tree:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)
    db_path = ctx.obj["db_path"]
    include_children = flat or tree

    resolved = None
    if project:
        resolved = str(Path(project).expanduser().resolve())

    conn = get_connection(db_path)
    try:
        rows = list_sessions(conn, resolved, include_children=include_children)
    finally:
        conn.close()
    if not rows:
        msg = "No sessions found."
        if resolved:
            msg = f"No sessions found for project '{project}'."
        console.print(msg)
        return
    if tree:
        console.print(format_sessions_tree(rows))
    else:
        console.print(format_sessions_list(rows))


def _export_sessions(
    conn,
    rows: list,
    *,
    to: Path | None,
    to_project: Path | None,
    fmt: str,
    tree: bool,
    thinking: bool,
    tool_calls: str,
    dry_run: bool = False,
) -> list[Path]:
    is_raw = fmt == "raw"
    ext = "json" if is_raw else "md"
    sub_dir = "raw_conversations" if is_raw else "conversations"

    exported: list[Path] = []
    root_id = rows[0]["id"] if rows else None
    for row in rows:
        sid = row["id"]

        if is_raw:
            raw = load_raw_messages(conn, sid)
            content = session_to_raw_json(dict(row), raw)
        else:
            messages = load_messages(conn, sid)
            content = session_to_markdown(dict(row), messages, thinking=thinking, tool_calls=tool_calls)

        if to:
            out_dir = to
        elif to_project:
            out_dir = to_project / ".opencode" / sub_dir
        else:
            project_dir = Path(row["directory"])
            if project_dir.is_dir():
                out_dir = project_dir / ".opencode" / sub_dir
            else:
                out_dir = Path.cwd()

        if tree and root_id and sid != root_id:
            out_dir = out_dir / root_id

        output = out_dir / f"{sid}.{ext}"
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
        exported.append(output)
    return exported


@export_app.command("session")
def export_session_cmd(
    ctx: typer.Context,
    session_id: str = typer.Option(..., "--from", help="Session ID to export"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to project's .opencode dir"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export session and all subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export session and all subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported and exit without writing"),
):
    """Export a session as markdown or raw JSON."""
    db_path = ctx.obj["db_path"]
    conn = get_connection(db_path)
    try:
        if tree or flat:
            rows = get_session_tree(conn, session_id)
        else:
            row = get_session(conn, session_id)
            if row is None:
                console.print(f"[red]Error:[/red] Session '{session_id}' not found.")
                raise typer.Exit(1)
            rows = [row]
        exported = _export_sessions(conn, rows, to=to, to_project=to_project, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls, dry_run=dry_run)
    finally:
        conn.close()
    verb = "Would export to" if dry_run else "Exported to"
    for p in exported:
        console.print(f"{verb} {p}")
    if dry_run:
        console.print("\n[dim]--dry-run: no files written.[/dim]")


# --- Import helpers ---

def _validate_raw_json(data: dict) -> tuple[dict, list[dict], list[dict]]:
    """Validate raw JSON structure and return (session, messages, parts)."""
    for key in ("session", "messages", "parts"):
        if key not in data:
            raise ValueError(f"Invalid raw JSON: missing '{key}' key")
    session = data["session"]
    if not isinstance(session, dict) or "id" not in session:
        raise ValueError("Invalid raw JSON: 'session' must be a dict with 'id'")
    return session, data["messages"], data["parts"]


def _load_raw_file(path: Path) -> dict:
    """Load and parse a raw JSON file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Failed to read raw JSON file '{path}': {e}")


def _find_tree_children(json_file: Path, root_id: str) -> list[dict]:
    """Auto-detect subagent JSON files from tree or flat layout."""
    json_dir = json_file.parent

    # Try tree layout first: <dir>/<root_id>/*.json
    tree_dir = json_dir / root_id
    if tree_dir.is_dir():
        candidates = list(tree_dir.glob("*.json"))
    else:
        # Fall back to flat layout: <dir>/*.json (excluding the root file itself)
        candidates = [f for f in json_dir.glob("*.json") if f != json_file]

    children = []
    known_ids = {root_id}
    # BFS: keep expanding as we discover new sessions
    queue = [root_id]
    while queue:
        parent_id = queue.pop(0)
        for candidate in candidates:
            if candidate in children:
                continue
            try:
                data = _load_raw_file(candidate)
                session = data["session"]
            except (ValueError, KeyError):
                continue
            if session.get("parent_id") == parent_id and session["id"] not in known_ids:
                known_ids.add(session["id"])
                children.append(data)
                queue.append(session["id"])
    return children


def _checkpoint_and_backup(db_path: Path) -> Path:
    """Checkpoint WAL and create a timestamped backup. Returns backup path."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = db_path.parent / f"{db_path.name}.bak.{timestamp}"
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


def _import_session_tree(
    db_path: Path,
    tree_data: list[dict],
    to_project: Path,
    substitute: bool = False,
) -> dict:
    """Import a session tree into the database."""
    if not tree_data:
        return {"imported": 0, "skipped": 0, "session_ids": [], "backup_path": None, "old_directory": None}

    backup_path = _checkpoint_and_backup(db_path)
    new_directory = str(to_project.expanduser().resolve())

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN")
        imported = []
        skipped = []
        imported_ids = []
        old_directory = None

        for data in tree_data:
            session, messages, parts = _validate_raw_json(data)

            if session.get("parent_id") is not None and session["parent_id"] not in imported_ids:
                skipped.append(session["id"])
                continue

            if session_exists(conn, session["id"]):
                skipped.append(session["id"])
                imported_ids.append(session["id"])
                continue

            if old_directory is None and session.get("parent_id") is None:
                old_directory = session.get("directory")

            session["directory"] = new_directory
            session["project_id"] = "global"

            insert_session(conn, session)
            insert_messages(conn, messages)
            insert_parts(conn, parts, messages=messages)
            imported.append(session["id"])
            imported_ids.append(session["id"])

        path_sub_counts = {}
        if substitute and old_directory and imported_ids:
            path_sub_counts = substitute_paths(conn, imported_ids, old_directory, new_directory)

        for data in tree_data:
            session = data["session"]
            if session.get("parent_id") is None and session["id"] in imported:
                validate_session_tree(conn, session["id"])

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "imported": imported,
        "skipped": skipped,
        "session_ids": imported_ids,
        "backup_path": backup_path,
        "old_directory": old_directory,
        "path_sub_counts": path_sub_counts,
    }


def _plan_import(db_path: Path, tree_data: list[dict], to_project: Path) -> dict:
    """Read-only preview of what _import_session_tree would do.

    Mirrors the per-session classification in _import_session_tree (skip
    subagents whose parent isn't in this batch, skip ids already in the DB)
    but performs only SELECTs — no transaction, no checkpoint, no backup.

    Returns {"imported": [...], "skipped": [...], "old_directory": str|None}.
    """
    imported: list[str] = []
    skipped: list[str] = []
    planned_ids: list[str] = []  # ids counted as 'present' (newly imported or pre-existing)
    old_directory = None

    conn = get_connection(db_path)
    try:
        for data in tree_data:
            session, _messages, _parts = _validate_raw_json(data)

            if session.get("parent_id") is not None and session["parent_id"] not in planned_ids:
                skipped.append(session["id"])
                continue

            if session_exists(conn, session["id"]):
                skipped.append(session["id"])
                planned_ids.append(session["id"])
                continue

            if old_directory is None and session.get("parent_id") is None:
                old_directory = session.get("directory")

            imported.append(session["id"])
            planned_ids.append(session["id"])
    finally:
        conn.close()

    return {
        "imported": imported,
        "skipped": skipped,
        "old_directory": old_directory,
    }


def _print_import_plan(plan: dict, substitute: bool, to_project: Path) -> None:
    """Pretty-print the import preview (used by --dry-run)."""
    new_directory = str(to_project.expanduser().resolve())
    imported = plan["imported"]
    skipped = plan["skipped"]

    if imported:
        console.print(f"[green]Would import {len(imported)} session(s):[/green]")
        console.print("[dim]  project_id would be reset to 'global' — OpenCode assigns the correct ID on next startup[/dim]")
        for sid in imported:
            console.print(f"  {sid}")
    if skipped:
        console.print(f"[yellow]Would skip {len(skipped)} session(s) (already exist or parent missing):[/yellow]")
        for sid in skipped:
            console.print(f"  {sid}")
    if not imported and not skipped:
        console.print("[yellow]Nothing to import.[/yellow]")
        return

    old_directory = plan.get("old_directory")
    if substitute and old_directory and old_directory != new_directory and imported:
        console.print(f"\n[cyan]Would substitute paths: '{old_directory}' -> '{new_directory}'[/cyan]")
    elif substitute:
        console.print("\n[dim]Path substitution: no source directory to substitute.[/dim]")
    console.print(f"[dim]Target directory: {new_directory}[/dim]")


def _verify_with_opencode(project_dir: Path) -> bool:
    """Verify DB integrity using opencode db CLI.

    Returns True if no errors detected, None if skipped.
    """
    opencode = shutil.which("opencode-cli.exe") or shutil.which("opencode-cli") or shutil.which("opencode")
    if not opencode:
        console.print("[yellow]Warning: opencode-cli not found, skipping runtime verification.[/yellow]")
        return None

    console.print("[cyan]Verifying with OpenCode...[/cyan]")
    try:
        # Use opencode db to run a simple integrity check
        result = subprocess.run(
            [opencode, "db", "PRAGMA integrity_check;"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            error = (result.stderr or "").strip()
            console.print(f"[red]OpenCode verification failed:[/red]")
            for line in error.split("\n")[:10]:
                if line.strip():
                    console.print(f"  [red]{line}[/red]")
            return False

        # Also try querying sessions for this project
        escaped = str(project_dir).replace("'", "''")
        sql = f"SELECT COUNT(*) FROM session WHERE directory LIKE '{escaped}%';"
        result2 = subprocess.run(
            [opencode, "db", sql],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result2.returncode != 0:
            console.print(f"[yellow]Warning: opencode db query failed, but integrity check passed.[/yellow]")
            return True

        console.print("[green]OpenCode verification passed.[/green]")
        return True
    except subprocess.TimeoutExpired:
        console.print("[yellow]Warning: opencode db timed out.[/yellow]")
        return None
    except Exception as e:
        console.print(f"[yellow]Warning: could not verify with OpenCode: {e}[/yellow]")
        return None


def _import_report(result: dict) -> None:
    """Print import results to console."""
    if result["imported"]:
        console.print(f"[green]Imported {len(result['imported'])} session(s):[/green]")
        console.print("[dim]  project_id reset to 'global' — OpenCode will assign the correct ID on next startup[/dim]")
        for sid in result["imported"]:
            console.print(f"  {sid}")
    if result["skipped"]:
        console.print(f"[yellow]Skipped {len(result['skipped'])} session(s) (already exist):[/yellow]")
        for sid in result["skipped"]:
            console.print(f"  {sid}")
    if not result["imported"] and not result["skipped"]:
        console.print("[yellow]Nothing to import.[/yellow]")
        return
    path_counts = result.get("path_sub_counts", {})
    if path_counts:
        total = sum(path_counts.values())
        console.print(f"\n[cyan]Path substitution: {total} row(s) updated in {len(path_counts)} session(s)[/cyan]")
        for sid, count in path_counts.items():
            console.print(f"  {sid}: {count} row(s)")
    if result["backup_path"]:
        console.print(f"\n[dim]Backup: {result['backup_path']}[/dim]")
        console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")


# --- Sync helpers ---


def _is_interactive() -> bool:
    """True when stdin is a TTY (user can answer prompts)."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _manifest_path(project_dir: Path) -> Path:
    return project_dir / ".opencode" / MANIFEST_FILENAME


def _raw_dir(project_dir: Path) -> Path:
    return project_dir / ".opencode" / RAW_SUBDIR


def _load_manifest(project_dir: Path) -> dict | None:
    """Load the sync manifest. Returns None when absent or unreadable (first sync)."""
    path = _manifest_path(project_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(sessions, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        console.print(f"[yellow]Warning: manifest at {path} is unreadable; treating this as a first sync (no deletions propagated).[/yellow]")
        return None


def _write_manifest(project_dir: Path, sessions_map: dict[str, dict]) -> None:
    """Atomically write the sync manifest.

    ``sessions_map`` maps session_id -> {"db_time_updated": int, "folder_time_updated": int}.
    Written via a temp file + os.replace so a crash mid-write never leaves a
    half-written manifest (the next run would treat that as first-sync).
    """
    path = _manifest_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "last_synced": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions": sessions_map,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".ocsm-sync.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_folder_sessions(raw_dir: Path) -> dict[str, dict]:
    """Scan a raw_conversations dir (flat or tree layout) and return {id: data}.

    Each value is the raw payload dict {"session", "messages", "parts"} with an
    extra internal ``_source_path`` key (a Path) pointing at the file it was read
    from — used by deletion propagation so a session can be removed even when its
    filename does not equal its id. Reuses _load_raw_file + _validate_raw_json.
    Invalid files are warned and skipped.
    """
    if not raw_dir.is_dir():
        return {}
    json_files = sorted(raw_dir.glob("*.json")) + sorted(raw_dir.glob("*/*.json"))
    out: dict[str, dict] = {}
    for jf in json_files:
        try:
            data = _load_raw_file(jf)
            session, _, _ = _validate_raw_json(data)
        except ValueError as e:
            console.print(f"[yellow]Warning: skipping {jf.name}: {e}[/yellow]")
            continue
        sid = session["id"]
        if sid in out:
            continue
        # stash the source path for deletion propagation; never serialized back out
        data["_source_path"] = jf
        out[sid] = data
    return out


def _build_folder_trees(folder_sessions: dict[str, dict]) -> dict[str, list[str]]:
    """Group folder session ids by root: {root_id: [root_id, child_id, ...]} (BFS).

    A session whose parent_id is not present in the folder set is treated as its
    own root — it will still be synced, and its real parent (if present in the DB)
    will link up on the DB side.
    """
    by_id = {sid: data["session"] for sid, data in folder_sessions.items()}
    children_map: dict[str, list[str]] = {}
    for sid, sess in by_id.items():
        pid = sess.get("parent_id")
        if pid and pid in by_id:
            children_map.setdefault(pid, []).append(sid)

    roots: dict[str, list[str]] = {}
    seen: set[str] = set()
    for sid, sess in by_id.items():
        pid = sess.get("parent_id")
        is_root = not pid or pid not in by_id
        if not is_root or sid in seen:
            continue
        tree = [sid]
        seen.add(sid)
        queue = [sid]
        while queue:
            parent = queue.pop(0)
            for child in children_map.get(parent, []):
                if child not in seen:
                    seen.add(child)
                    tree.append(child)
                    queue.append(child)
        roots[sid] = tree
    # Orphans already visited as roots above; anything left (shouldn't happen) is ignored.
    return roots


def _compute_sync_diff(
    db_side: dict[str, int],
    folder_side: dict[str, int],
    manifest: dict | None,
) -> dict:
    """Compute the sync plan from DB / folder / manifest timestamp maps.

    Returns dict with keys:
      to_folder:      ids to push DB -> folder (only-in-db + DB-wins conflicts)
      to_db:          ids to pull folder -> DB (only-in-folder + folder-wins conflicts)
      conflicts:      ids present on both sides with differing time_updated (pre-resolution)
      same:           ids on both sides with identical time_updated (no-op)
      delete_from_folder: ids that were tracked and vanished from DB -> delete files
      delete_from_db:     ids that were tracked and vanished from folder -> delete DB rows

    Deletion detection takes precedence over add detection: if a manifest-tracked id
    is now missing from one side, it is classified as a deletion (not a fresh add on
    the other side). This prevents "DB deleted X" from being re-imported from the folder.
    An id absent from the manifest is never treated as a deletion — first sync is safe.
    """
    manifest_sessions = (manifest or {}).get("sessions", {}) if manifest else {}

    delete_from_folder: list[str] = []
    delete_from_db: list[str] = []
    deleted_ids: set[str] = set()

    # 1. Deletion detection (highest precedence). Tracked ids missing from one side.
    for sid in manifest_sessions:
        in_db = sid in db_side
        in_folder = sid in folder_side
        if in_db and in_folder:
            continue
        if not in_db and not in_folder:
            continue  # gone from both: already reconciled
        if not in_db:
            delete_from_folder.append(sid)   # DB deleted it -> propagate to folder
        else:  # in_db and not in_folder
            delete_from_db.append(sid)       # folder deleted it -> propagate to DB
        deleted_ids.add(sid)

    # 2. Add / conflict / same detection for the remaining ids.
    to_folder: list[str] = []
    to_db: list[str] = []
    conflicts: list[str] = []
    same: list[str] = []

    all_ids = set(db_side) | set(folder_side)
    for sid in all_ids:
        if sid in deleted_ids:
            continue  # handled as a deletion above
        in_db = sid in db_side
        in_folder = sid in folder_side
        if in_db and in_folder:
            if db_side[sid] == folder_side[sid]:
                same.append(sid)
            else:
                conflicts.append(sid)
        elif in_db:
            to_folder.append(sid)
        else:  # in_folder only
            to_db.append(sid)

    return {
        "to_folder": sorted(to_folder),
        "to_db": sorted(to_db),
        "conflicts": sorted(conflicts),
        "same": sorted(same),
        "delete_from_folder": sorted(delete_from_folder),
        "delete_from_db": sorted(delete_from_db),
    }


def _resolve_conflict(sid: str, db_tu: int | None, folder_tu: int | None, on_conflict: str) -> str:
    """Resolve a single conflict. Returns 'db' | 'folder' | 'skip'.

    - on_conflict='ask'      : interactive prompt (requires TTY; caller gates this)
    - on_conflict='newer'    : higher time_updated wins; tie -> skip
    - on_conflict='skip'     : always skip
    """
    if on_conflict == "skip":
        return "skip"
    if on_conflict == "newer":
        if db_tu is None or folder_tu is None:
            return "skip"
        if db_tu > folder_tu:
            return "db"
        if folder_tu > db_tu:
            return "folder"
        return "skip"
    # on_conflict == 'ask'
    db_str = format_timestamp(db_tu) if db_tu else "(unknown)"
    folder_str = format_timestamp(folder_tu) if folder_tu else "(unknown)"
    console.print(f"\n[bold yellow]Conflict on session:[/bold yellow] {sid}")
    console.print(f"  [1] keep DB version      updated {db_str}")
    console.print(f"  [2] keep folder version  updated {folder_str}")
    console.print(f"  [3] skip (leave both as-is)")
    while True:
        try:
            choice = input("Choose [1/2/3] (default 3): ").strip()
        except (EOFError, KeyboardInterrupt):
            return "skip"
        if choice in ("", "3"):
            return "skip"
        if choice == "1":
            return "db"
        if choice == "2":
            return "folder"
        console.print("[dim]Please enter 1, 2, or 3.[/dim]")


def _ask_delete_confirmation(label: str, items: list[tuple[str, str | None]]) -> bool:
    """List pending deletions and ask for a single y/N confirmation.

    ``items`` is a list of (id, title) tuples. Returns True only on explicit yes.
    """
    if not items:
        return True
    console.print(f"\n[bold red]Deletions to propagate ({label}):[/bold red]")
    for sid, title in items:
        display = title or "(untitled)"
        console.print(f"  [red]x[/red] {sid}  [dim]{display}[/dim]")
    try:
        ans = input(f"\nProceed with these {len(items)} deletion(s)? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def _apply_sync(
    db_path: Path,
    project_dir: Path,
    diff: dict,
    folder_sessions: dict[str, dict],
    db_sessions: dict[str, int],
    *,
    substitute: bool,
    do_delete: bool,
) -> dict:
    """Execute the sync plan against DB and filesystem.

    - DB writes go through _checkpoint_and_backup + a single transaction.
    - Folder writes happen before the DB transaction (DB is source-of-truth-able to regenerate).
    - Returns a summary dict for reporting.
    """
    raw_dir = _raw_dir(project_dir)
    new_directory = str(project_dir.expanduser().resolve())

    to_folder_ids = set(diff["to_folder"])
    to_db_ids = set(diff["to_db"])
    delete_from_folder_ids = set(diff["delete_from_folder"])
    delete_from_db_ids = set(diff["delete_from_db"])

    # --- Folder-side mutations (writes + deletes) ---
    folder_written: list[str] = []
    folder_deleted: list[str] = []

    # 1. Push DB -> folder for ids flagged to_folder.
    if to_folder_ids:
        conn = get_connection(db_path)
        try:
            for sid in to_folder_ids:
                row = get_session(conn, sid)
                if row is None:
                    continue
                raw = load_raw_messages(conn, sid)
                content = session_to_raw_json(dict(row), raw)
                # tree layout for subagents
                pid = row["parent_id"]
                if pid:
                    out_dir = raw_dir / pid
                else:
                    out_dir = raw_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{sid}.json").write_text(content, encoding="utf-8")
                folder_written.append(sid)
        finally:
            conn.close()

    # 2. Propagate DB-side deletions to the folder.
    if do_delete and delete_from_folder_ids:
        # Gather candidate file paths per sid: the recorded source path (authoritative,
        # handles non-standard filenames) plus the conventional {sid}.json locations.
        subdirs_to_cleanup: set[Path] = set()
        for sid in delete_from_folder_ids:
            candidates: list[Path] = []
            src = folder_sessions.get(sid, {}).get("_source_path")
            if isinstance(src, Path):
                candidates.append(src)
            candidates.append(raw_dir / f"{sid}.json")
            for sub in raw_dir.iterdir():
                if sub.is_dir():
                    candidates.append(sub / f"{sid}.json")
            removed = False
            for c in candidates:
                try:
                    if c.is_file():
                        c.unlink()
                        removed = True
                        if c.parent != raw_dir:
                            subdirs_to_cleanup.add(c.parent)
                except OSError:
                    pass
            if removed:
                folder_deleted.append(sid)
        # clean up now-empty subtree dirs
        for d in subdirs_to_cleanup:
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    # --- DB-side mutations (single transaction) ---
    db_imported: list[str] = []
    db_deleted: list[str] = []
    path_sub_counts: dict[str, int] = {}
    backup_path: Path | None = None

    db_needs_write = bool(to_db_ids or (do_delete and delete_from_db_ids))
    if db_needs_write:
        backup_path = _checkpoint_and_backup(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute("BEGIN")

            # Pull folder -> DB for ids flagged to_db (replace in place).
            imported_ids: list[str] = []
            old_directory: str | None = None
            for sid in to_db_ids:
                data = folder_sessions.get(sid)
                if data is None:
                    continue
                session, messages, parts = _validate_raw_json(data)
                # Skip orphan subagents whose parent is neither in this batch nor already in DB.
                pid = session.get("parent_id")
                if pid is not None and pid not in imported_ids and not session_exists(conn, pid):
                    continue
                if old_directory is None and pid is None:
                    old_directory = session.get("directory")
                session["directory"] = new_directory
                session["project_id"] = "global"
                replace_session(conn, session, messages, parts)
                imported_ids.append(sid)
                db_imported.append(sid)

            if substitute and old_directory and imported_ids:
                path_sub_counts = substitute_paths(conn, imported_ids, old_directory, new_directory)

            # Propagate folder-side deletions to DB.
            if do_delete and delete_from_db_ids:
                # Expand to whole trees: include descendants of any deleted id.
                to_remove: set[str] = set()
                queue = list(delete_from_db_ids)
                while queue:
                    cur = queue.pop(0)
                    if cur in to_remove:
                        continue
                    to_remove.add(cur)
                    children = conn.execute(
                        "SELECT id FROM session WHERE parent_id = ?", (cur,)
                    ).fetchall()
                    for c in children:
                        queue.append(c["id"])
                if to_remove:
                    delete_session_tree(conn, sorted(to_remove))
                    db_deleted = sorted(to_remove)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {
        "folder_written": folder_written,
        "folder_deleted": folder_deleted,
        "db_imported": db_imported,
        "db_deleted": db_deleted,
        "backup_path": backup_path,
        "path_sub_counts": path_sub_counts,
    }


def _print_sync_plan(diff: dict, folder_sessions: dict[str, dict], db_sessions: dict[str, int]) -> None:
    """Pretty-print the computed sync plan (used for --dry-run and pre-apply summary)."""
    def _title_from_folder(sid: str) -> str | None:
        s = folder_sessions.get(sid, {}).get("session")
        return s.get("title") if s else None

    def _fmt_ts(ms: int | None) -> str:
        return format_timestamp(ms) if ms else "(unknown)"

    if diff["to_folder"]:
        console.print(f"[green]DB -> folder ({len(diff['to_folder'])}):[/green]")
        for sid in diff["to_folder"]:
            console.print(f"  + {sid}  [dim]db updated {_fmt_ts(db_sessions.get(sid))}[/dim]")
    if diff["to_db"]:
        console.print(f"[cyan]folder -> DB ({len(diff['to_db'])}):[/cyan]")
        for sid in diff["to_db"]:
            console.print(f"  + {sid}  [dim]{_title_from_folder(sid) or '(untitled)'}[/dim]")
    if diff["conflicts"]:
        console.print(f"[yellow]Conflicts ({len(diff['conflicts'])}):[/yellow]")
        for sid in diff["conflicts"]:
            console.print(f"  ! {sid}  [dim]db {_fmt_ts(db_sessions.get(sid))} vs folder {_fmt_ts(_folder_tu(folder_sessions, sid))}[/dim]")
    if diff["same"]:
        console.print(f"[dim]In sync ({len(diff['same'])}): no change[/dim]")
    if diff["delete_from_folder"]:
        console.print(f"[red]Delete from folder ({len(diff['delete_from_folder'])}):[/red]")
        for sid in diff["delete_from_folder"]:
            console.print(f"  x {sid}  [dim]{_title_from_folder(sid) or '(untitled)'}[/dim]")
    if diff["delete_from_db"]:
        console.print(f"[red]Delete from DB ({len(diff['delete_from_db'])}):[/red]")
        for sid in diff["delete_from_db"]:
            console.print(f"  x {sid}  [dim]db updated {_fmt_ts(db_sessions.get(sid))}[/dim]")


def _folder_tu(folder_sessions: dict[str, dict], sid: str) -> int | None:
    s = folder_sessions.get(sid, {}).get("session")
    if not s:
        return None
    v = s.get("time_updated")
    return v if isinstance(v, int) else None


@sync_app.command("project")
def sync_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(..., "--from", help="Project directory to sync (DB <-> <project>/.opencode/raw_conversations/)"),
    on_conflict: str = typer.Option("ask", "--on-conflict", help="Conflict resolution: ask | newer | skip"),
    delete: bool = typer.Option(True, "--delete/--no-delete", help="Propagate deletions (requires a prior manifest)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive: skip all confirmations (deletions still need --delete)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and exit without writing anything"),
    substitute_paths_flag: bool = typer.Option(True, "--substitute-paths/--no-substitute-paths", help="Replace old paths in imported data fields"),
):
    """Two-way sync a project's sessions between the DB and the project folder.

    Reads the DB state for the project, the <project>/.opencode/raw_conversations/
    folder, and the <project>/.opencode/.ocsm-sync.json manifest, then reconciles
    new/updated/deleted sessions on both sides. Conflicts (same id, different
    time_updated) are resolved via --on-conflict (default: interactive prompt).
    """
    if on_conflict not in ("ask", "newer", "skip"):
        console.print(f"[red]Error:[/red] --on-conflict must be one of: ask, newer, skip")
        raise typer.Exit(2)

    project_dir = Path(project).expanduser().resolve()
    if not project_dir.is_dir():
        console.print(f"[red]Error:[/red] Project directory not found: {project_dir}")
        raise typer.Exit(1)

    db_path = ctx.obj["db_path"]
    raw_dir = _raw_dir(project_dir)

    # --- Gather state on all three sides ---
    new_directory = str(project_dir)
    conn = get_connection(db_path)
    try:
        db_sessions = get_session_timestamps(conn, new_directory)
    finally:
        conn.close()

    folder_sessions = _load_folder_sessions(raw_dir)
    folder_side: dict[str, int] = {
        sid: data["session"]["time_updated"]
        for sid, data in folder_sessions.items()
        if isinstance(data.get("session", {}).get("time_updated"), int)
    }
    manifest = _load_manifest(project_dir)

    # --- Compute diff ---
    diff = _compute_sync_diff(db_sessions, folder_side, manifest)

    console.print(f"[bold]Project:[/bold] {project_dir}")
    console.print(f"[dim]DB: {len(db_sessions)} session(s)  |  folder: {len(folder_side)} session(s)  |  manifest: {'yes' if manifest else 'no (first sync)'}[/dim]\n")
    _print_sync_plan(diff, folder_sessions, db_sessions)

    # --- Resolve conflicts ---
    unresolved = list(diff["conflicts"])
    if unresolved:
        if on_conflict == "ask" and not _is_interactive():
            console.print(f"\n[red]Error:[/red] {len(unresolved)} conflict(s) require --on-conflict (ask needs a TTY). Re-run with --on-conflict newer|skip.")
            raise typer.Exit(1)
        for sid in unresolved:
            chosen = _resolve_conflict(sid, db_sessions.get(sid), folder_side.get(sid), on_conflict)
            if chosen == "db":
                diff["to_folder"].append(sid)
            elif chosen == "folder":
                diff["to_db"].append(sid)
            # skip: leave on both sides untouched
        diff["conflicts"] = []  # all resolved (or skipped)

    # --- Deletion confirmation ---
    if delete and (diff["delete_from_folder"] or diff["delete_from_db"]):
        if dry_run or yes:
            pass  # dry-run just displays; yes skips prompt
        elif _is_interactive():
            items_folder = [(sid, _folder_title(folder_sessions, sid)) for sid in diff["delete_from_folder"]]
            items_db = [(sid, _folder_title(folder_sessions, sid)) for sid in diff["delete_from_db"]]
            ok = _ask_delete_confirmation("folder", items_folder) and _ask_delete_confirmation("DB", items_db)
            if not ok:
                console.print("[yellow]Deletions declined by user; continuing without deletion propagation.[/yellow]")
                diff["delete_from_folder"] = []
                diff["delete_from_db"] = []
        else:
            console.print("[yellow]Warning: deletions detected in non-interactive mode without -y; skipping deletion propagation.[/yellow]")
            diff["delete_from_folder"] = []
            diff["delete_from_db"] = []
    elif not delete and (diff["delete_from_folder"] or diff["delete_from_db"]):
        console.print("[dim]Deletions present but --no-delete set; not propagating.[/dim]")
        diff["delete_from_folder"] = []
        diff["delete_from_db"] = []

    has_work = any(diff[k] for k in ("to_folder", "to_db", "delete_from_folder", "delete_from_db"))
    if not has_work:
        console.print("\n[green]Already in sync. Nothing to do.[/green]")
        # Still refresh the manifest so future runs can detect deletions correctly.
        if not dry_run:
            _refresh_manifest(project_dir, db_sessions, folder_side, folder_sessions)
        return

    if dry_run:
        console.print("\n[dim]--dry-run: no changes made, manifest not updated.[/dim]")
        return

    # --- Apply ---
    try:
        result = _apply_sync(
            db_path,
            project_dir,
            diff,
            folder_sessions,
            db_sessions,
            substitute=substitute_paths_flag,
            do_delete=delete,
        )
    except Exception as e:
        console.print(f"[red]Error during sync: {e}[/red]")
        console.print("[dim]The database was rolled back. Folder-side writes (if any) are NOT rolled back — re-run sync to reconcile.[/dim]")
        raise typer.Exit(1)

    # --- Report ---
    _sync_report(result)

    # --- Re-gather post-sync state and refresh manifest ---
    conn = get_connection(db_path)
    try:
        post_db = get_session_timestamps(conn, new_directory)
    finally:
        conn.close()
    # Re-scan folder to capture newly written files + reflect deletions
    post_folder_sessions = _load_folder_sessions(raw_dir)
    post_folder = {
        sid: data["session"]["time_updated"]
        for sid, data in post_folder_sessions.items()
        if isinstance(data.get("session", {}).get("time_updated"), int)
    }
    _refresh_manifest(project_dir, post_db, post_folder, post_folder_sessions)

    # --- Verify (only if DB changed) ---
    if result["db_imported"] or result["db_deleted"]:
        _verify_with_opencode(project_dir)


def _folder_title(folder_sessions: dict[str, dict], sid: str) -> str | None:
    s = folder_sessions.get(sid, {}).get("session")
    return s.get("title") if s else None


def _refresh_manifest(
    project_dir: Path,
    db_side: dict[str, int],
    folder_side: dict[str, int],
    folder_sessions: dict[str, dict],
) -> None:
    """Write a fresh manifest reflecting current DB + folder state.

    Only ids present on BOTH sides after sync are recorded, so a later one-sided
    deletion is detectable. ids present on only one side are NOT recorded — they
    will be reconciled (pushed/pulled) before being tracked, avoiding spurious
    deletion propagation for never-yet-synced sessions.
    """
    common = set(db_side) & set(folder_side)
    sessions_map = {
        sid: {
            "db_time_updated": db_side[sid],
            "folder_time_updated": folder_side[sid],
        }
        for sid in common
    }
    try:
        _write_manifest(project_dir, sessions_map)
    except OSError as e:
        console.print(f"[yellow]Warning: could not write manifest: {e}[/yellow]")


def _sync_report(result: dict) -> None:
    """Print sync results to console."""
    fw = result["folder_written"]
    di = result["db_imported"]
    fd = result["folder_deleted"]
    dd = result["db_deleted"]
    if fw:
        console.print(f"[green]DB -> folder: wrote {len(fw)} session(s)[/green]")
        for sid in fw:
            console.print(f"  + {sid}")
    if di:
        console.print(f"[cyan]folder -> DB: imported {len(di)} session(s)[/cyan]")
        console.print("[dim]  project_id reset to 'global' — OpenCode will assign the correct ID on next startup[/dim]")
        for sid in di:
            console.print(f"  + {sid}")
    if fd:
        console.print(f"[red]Deleted {len(fd)} session file(s) from folder[/red]")
        for sid in fd:
            console.print(f"  x {sid}")
    if dd:
        console.print(f"[red]Deleted {len(dd)} session(s) from DB[/red]")
        for sid in dd:
            console.print(f"  x {sid}")
    if not (fw or di or fd or dd):
        console.print("[yellow]Nothing changed.[/yellow]")
    counts = result.get("path_sub_counts", {})
    if counts:
        total = sum(counts.values())
        console.print(f"\n[cyan]Path substitution: {total} row(s) updated in {len(counts)} session(s)[/cyan]")
    if result["backup_path"]:
        console.print(f"\n[dim]Backup: {result['backup_path']}[/dim]")
        console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")


@export_app.command("project")
def export_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(..., "--from", help="Project directory path"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to project's .opencode dir"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export with subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export with subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported and exit without writing"),
):
    """Export all sessions of a project."""
    if flat and tree:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)
    resolved = str(Path(project).expanduser().resolve())
    db_path = ctx.obj["db_path"]
    conn = get_connection(db_path)
    try:
        rows = list_sessions(conn, resolved, include_children=flat or tree)
        if not rows:
            console.print(f"[red]Error:[/red] No sessions found for project '{project}'.")
            raise typer.Exit(1)
        exported = _export_sessions(conn, rows, to=to, to_project=to_project, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls, dry_run=dry_run)
    finally:
        conn.close()
    verb = "Would export to" if dry_run else "Exported to"
    for p in exported:
        console.print(f"{verb} {p}")
    if dry_run:
        console.print("\n[dim]--dry-run: no files written.[/dim]")


@import_app.command("session")
def import_session_cmd(
    ctx: typer.Context,
    json_file: Path = typer.Option(..., "--from", help="Path to raw JSON file"),
    to_project: Path = typer.Option(..., "--to-project", help="Local project directory to map to"),
    substitute_paths_flag: bool = typer.Option(True, "--substitute-paths/--no-substitute-paths", help="Replace old paths in data fields"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be imported and exit without writing"),
):
    """Import a session (and its subagent tree) from a raw JSON file."""
    db_path = ctx.obj["db_path"]

    try:
        data = _load_raw_file(json_file)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    session, messages, parts = _validate_raw_json(data)

    if session.get("parent_id") is not None:
        console.print("[red]Error:[/red] This file is a subagent session (has parent_id). Import from the root session instead.")
        raise typer.Exit(1)

    children = _find_tree_children(json_file, session["id"])
    tree_data = [data] + children

    console.print(f"Found {len(tree_data)} session(s) to import (1 root + {len(children)} subagents)")

    if dry_run:
        plan = _plan_import(db_path, tree_data, to_project)
        _print_import_plan(plan, substitute_paths_flag, to_project)
        console.print("\n[dim]--dry-run: no changes made, no backup created.[/dim]")
        return

    try:
        result = _import_session_tree(db_path, tree_data, to_project, substitute=substitute_paths_flag)
    except Exception as e:
        console.print(f"[red]Error during import: {e}[/red]")
        console.print("[dim]The database was rolled back. Your data is unchanged.[/dim]")
        raise typer.Exit(1)

    _import_report(result)
    if result["imported"]:
        _verify_with_opencode(to_project)


@import_app.command("project")
def import_project_cmd(
    ctx: typer.Context,
    from_dir: Path = typer.Option(..., "--from", help="Source project directory"),
    to_project: Path = typer.Option(..., "--to-project", help="Local project directory to map to"),
    substitute_paths_flag: bool = typer.Option(True, "--substitute-paths/--no-substitute-paths", help="Replace old paths in data fields"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be imported and exit without writing"),
):
    """Import all sessions from a project's raw export directory."""
    db_path = ctx.obj["db_path"]

    raw_dir = from_dir / ".opencode" / "raw_conversations"
    if not raw_dir.is_dir():
        console.print(f"[red]Error:[/red] No raw export directory found at '{raw_dir}'")
        raise typer.Exit(1)

    json_files = sorted(raw_dir.glob("*.json")) + sorted(raw_dir.glob("*/*.json"))
    if not json_files:
        console.print(f"[red]Error:[/red] No JSON files found in '{raw_dir}'")
        raise typer.Exit(1)

    all_data: dict[str, dict] = {}
    roots: list[str] = []

    for jf in json_files:
        try:
            data = _load_raw_file(jf)
            session, _, _ = _validate_raw_json(data)
            sid = session["id"]
            if sid in all_data:
                continue
            all_data[sid] = data
            if session.get("parent_id") is None:
                roots.append(sid)
        except ValueError as e:
            console.print(f"[yellow]Warning: skipping {jf.name}: {e}[/yellow]")
            continue

    if not roots:
        console.print("[red]Error:[/red] No root sessions found in the export directory.")
        raise typer.Exit(1)

    console.print(f"Found {len(roots)} root session(s) in '{raw_dir}'")

    all_tree_data = []
    for root_id in roots:
        tree = [all_data[root_id]]
        queue = [root_id]
        while queue:
            parent_id = queue.pop(0)
            for sid, data in all_data.items():
                if sid not in [t["session"]["id"] for t in tree] and data["session"].get("parent_id") == parent_id:
                    tree.append(data)
                    queue.append(sid)
        all_tree_data.extend(tree)

    console.print(f"Total {len(all_tree_data)} session(s) to import")

    if dry_run:
        plan = _plan_import(db_path, all_tree_data, to_project)
        _print_import_plan(plan, substitute_paths_flag, to_project)
        console.print("\n[dim]--dry-run: no changes made, no backup created.[/dim]")
        return

    try:
        result = _import_session_tree(db_path, all_tree_data, to_project, substitute=substitute_paths_flag)
    except Exception as e:
        console.print(f"[red]Error during import: {e}[/red]")
        console.print("[dim]The database was rolled back. Your data is unchanged.[/dim]")
        raise typer.Exit(1)

    _import_report(result)
    if result["imported"]:
        _verify_with_opencode(to_project)


@move_app.command("project")
def move_project_cmd(
    ctx: typer.Context,
    from_dir: Path = typer.Option(..., "--from", help="Original project directory path"),
    to_project: Path = typer.Option(..., "--to-project", help="New project directory path"),
):
    """Move all sessions from one project directory to another."""
    db_path = ctx.obj["db_path"]
    old_path = str(from_dir.expanduser().resolve())
    new_path = str(to_project.expanduser().resolve())

    if old_path == new_path:
        console.print("[yellow]Source and destination paths are the same. Nothing to do.[/yellow]")
        raise typer.Exit(0)

    conn = get_connection(db_path)
    try:
        rows = list_sessions(conn, old_path, include_children=True)
    finally:
        conn.close()

    if not rows:
        console.print(f"[yellow]No sessions found for directory '{old_path}'.[/yellow]")
        raise typer.Exit(1)

    session_ids = [row["id"] for row in rows]
    console.print(f"Found {len(session_ids)} session(s) matching '{old_path}'")

    # Check for existing sessions at target (merge scenario)
    conn2 = get_connection(db_path)
    try:
        target_rows = list_sessions(conn2, new_path, include_children=True)
    finally:
        conn2.close()

    skipped_ids: list[str] = []
    if target_rows:
        target_ids = {row["id"] for row in target_rows}
        skipped_ids = [sid for sid in session_ids if sid in target_ids]
        session_ids = [sid for sid in session_ids if sid not in target_ids]
        if skipped_ids:
            console.print(f"[yellow]Warning: {len(skipped_ids)} session(s) already exist at target, will be skipped[/yellow]")
        if not session_ids:
            console.print("[yellow]All sessions already exist at target. Nothing to move.[/yellow]")
            raise typer.Exit(0)

    console.print(f"Moving {len(session_ids)} session(s) to '{new_path}'...")

    backup_path = _checkpoint_and_backup(db_path)

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN")
        dir_updated = update_session_directory(conn, session_ids, old_path, new_path)
        project_updated = update_project_worktree(conn, old_path, new_path)
        pid_reset = reset_project_id_to_global(conn, session_ids)
        path_sub_counts = substitute_paths(conn, session_ids, old_path, new_path)
        conn.commit()
    except Exception:
        conn.rollback()
        console.print("[red]Error during move operation.[/red]")
        console.print("[dim]The database was rolled back. Your data is unchanged.[/dim]")
        raise typer.Exit(1)
    finally:
        conn.close()

    console.print(f"[green]Moved {len(session_ids)} session(s):[/green]")
    if skipped_ids:
        console.print(f"[yellow]Skipped {len(skipped_ids)} session(s) (already exist at target):[/yellow]")
        for sid in skipped_ids:
            console.print(f"  {sid}")
    console.print(f"  session.directory updated: {dir_updated} row(s)")
    if pid_reset:
        console.print(f"  session.project_id reset to 'global': {pid_reset} row(s) [dim]— OpenCode will assign the correct ID on next startup[/dim]")
    if project_updated:
        console.print(f"  project.worktree updated: {project_updated} row(s)")
    total_data = sum(path_sub_counts.values())
    if total_data:
        console.print(f"  path substitution: {total_data} row(s) updated in {len(path_sub_counts)} session(s)")
        for sid, count in path_sub_counts.items():
            console.print(f"    {sid}: {count} row(s)")
    else:
        console.print("  path substitution: no data fields contained the old path")

    console.print(f"\n[dim]Backup: {backup_path}[/dim]")
    console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")

    _verify_with_opencode(to_project)
