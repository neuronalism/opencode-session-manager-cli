from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from ocsm.db import get_connection, resolve_db_path
from ocsm.format import format_projects_list, format_sessions_list, format_sessions_tree, session_to_markdown, session_to_raw_json
from ocsm.queries import (
    get_session,
    get_session_tree,
    insert_messages,
    insert_parts,
    insert_session,
    list_projects,
    list_sessions,
    load_messages,
    load_raw_messages,
    reset_project_id_to_global,
    resolve_project,
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
    project_id: str = typer.Option(None, "--project-id", help="Filter by project ID"),
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
    if project or project_id:
        conn = get_connection(db_path)
        try:
            resolved = resolve_project(conn, project, project_id)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        finally:
            conn.close()

    conn = get_connection(db_path)
    try:
        rows = list_sessions(conn, resolved, include_children=include_children)
    finally:
        conn.close()
    if not rows:
        msg = "No sessions found."
        label = project or project_id or resolved
        if label:
            msg = f"No sessions found for project '{label}'."
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

        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{sid}.{ext}"
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
        exported = _export_sessions(conn, rows, to=to, to_project=to_project, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls)
    finally:
        conn.close()
    for p in exported:
        console.print(f"Exported to {p}")


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
            error = result.stderr.strip()
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


@export_app.command("project")
def export_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(None, "--from", help="Project directory path"),
    from_id: str = typer.Option(None, "--from-id", help="Project ID"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to project's .opencode dir"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export with subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export with subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
):
    """Export all sessions of a project."""
    if not project and not from_id:
        console.print("[red]Error:[/red] --from or --from-id must be provided.")
        raise typer.Exit(1)
    if flat and tree:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)
    db_path = ctx.obj["db_path"]
    conn = get_connection(db_path)
    try:
        resolved = resolve_project(conn, project, from_id)
        rows = list_sessions(conn, resolved, include_children=flat or tree)
        if not rows:
            console.print(f"[red]Error:[/red] No sessions found for project '{project or from_id}'.")
            raise typer.Exit(1)
        exported = _export_sessions(conn, rows, to=to, to_project=to_project, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls)
    finally:
        conn.close()
    for p in exported:
        console.print(f"Exported to {p}")


@import_app.command("session")
def import_session_cmd(
    ctx: typer.Context,
    json_file: Path = typer.Option(..., "--from", help="Path to raw JSON file"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Local project directory to map to"),
    to_project_id: str | None = typer.Option(None, "--to-project-id", help="Project ID to map to"),
    substitute_paths_flag: bool = typer.Option(True, "--substitute-paths/--no-substitute-paths", help="Replace old paths in data fields"),
):
    """Import a session (and its subagent tree) from a raw JSON file."""
    if not to_project and not to_project_id:
        console.print("[red]Error:[/red] --to-project or --to-project-id must be provided.")
        raise typer.Exit(1)
    db_path = ctx.obj["db_path"]

    # Resolve target project
    if to_project_id:
        conn = get_connection(db_path)
        try:
            resolved_dir = resolve_project(conn, project_id=to_project_id)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        finally:
            conn.close()
        to_project = Path(resolved_dir)

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
    to_project: Path | None = typer.Option(None, "--to-project", help="Local project directory to map to"),
    to_project_id: str | None = typer.Option(None, "--to-project-id", help="Project ID to map to"),
    substitute_paths_flag: bool = typer.Option(True, "--substitute-paths/--no-substitute-paths", help="Replace old paths in data fields"),
):
    """Import all sessions from a project's raw export directory."""
    if not to_project and not to_project_id:
        console.print("[red]Error:[/red] --to-project or --to-project-id must be provided.")
        raise typer.Exit(1)
    db_path = ctx.obj["db_path"]

    # Resolve target project
    if to_project_id:
        conn = get_connection(db_path)
        try:
            resolved_dir = resolve_project(conn, project_id=to_project_id)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        finally:
            conn.close()
        to_project = Path(resolved_dir)

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
    from_dir: Path | None = typer.Option(None, "--from", help="Original project directory path"),
    from_id: str | None = typer.Option(None, "--from-id", help="Original project ID"),
    to_project: Path | None = typer.Option(None, "--to-project", help="New project directory path"),
    to_id: str | None = typer.Option(None, "--to-id", help="New project ID"),
):
    """Move all sessions from one project directory to another."""
    if not from_dir and not from_id:
        console.print("[red]Error:[/red] --from or --from-id must be provided.")
        raise typer.Exit(1)
    if not to_project and not to_id:
        console.print("[red]Error:[/red] --to-project or --to-id must be provided.")
        raise typer.Exit(1)
    db_path = ctx.obj["db_path"]

    # Resolve source and target
    conn = get_connection(db_path)
    try:
        old_path = resolve_project(conn, str(from_dir) if from_dir else None, from_id)
        new_path = resolve_project(conn, str(to_project) if to_project else None, to_id)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    finally:
        conn.close()

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
