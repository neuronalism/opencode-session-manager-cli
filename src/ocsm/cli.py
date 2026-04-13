from __future__ import annotations

import io
import sys
from pathlib import Path

import typer
from rich.console import Console

from ocsm.db import get_connection, resolve_db_path
from ocsm.format import format_projects_list, format_sessions_list, format_sessions_tree, session_to_markdown, session_to_raw_json
from ocsm.queries import get_session, get_session_tree, list_projects, list_sessions, load_messages, load_raw_messages

app = typer.Typer(name="ocsm", help="OpenCode Sessions Manager", no_args_is_help=True)

list_app = typer.Typer(name="list", help="List resources", no_args_is_help=True)
app.add_typer(list_app)

export_app = typer.Typer(name="export", help="Export resources", no_args_is_help=True)
app.add_typer(export_app)


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
    conn = get_connection(db_path)
    try:
        rows = list_sessions(conn, project, include_children=include_children)
    finally:
        conn.close()
    if not rows:
        msg = "No sessions found."
        if project:
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
        exported = _export_sessions(conn, rows, to=to, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls)
    finally:
        conn.close()
    for p in exported:
        console.print(f"Exported to {p}")


@export_app.command("project")
def export_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(..., "--from", help="Project directory path"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export with subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export with subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
):
    """Export all sessions of a project."""
    db_path = ctx.obj["db_path"]
    conn = get_connection(db_path)
    try:
        if tree or flat:
            rows = list_sessions(conn, project, include_children=True)
        else:
            rows = list_sessions(conn, project, include_children=False)
        if not rows:
            console.print(f"[red]Error:[/red] No sessions found for project '{project}'.")
            raise typer.Exit(1)
        exported = _export_sessions(conn, rows, to=to, fmt=fmt, tree=tree, thinking=thinking, tool_calls=tool_calls)
    finally:
        conn.close()
    for p in exported:
        console.print(f"Exported to {p}")
