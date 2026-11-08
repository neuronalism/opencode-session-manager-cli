"""``ocsm list`` subapp — read-only views over the v2 project/session tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ocsm import queries
from ocsm.common import console, open_db
from ocsm.format import format_projects_list, format_sessions_list, format_sessions_tree
from ocsm.projectid import resolve_project

app = typer.Typer(name="list", help="Show projects and sessions stored in the database", no_args_is_help=True)


@app.command("project")
def list_projects_cmd(ctx: typer.Context):
    """List all unique projects."""
    conn = open_db(ctx)
    try:
        rows = queries.list_projects(conn)
    finally:
        conn.close()
    if not rows:
        console.print("No projects found.")
        return
    console.print(format_projects_list(rows))


def _project_filter_id(conn: sqlite3.Connection, value: str) -> str:
    """``--project`` value to a project id: a known id is used verbatim (exact
    match on the ``project`` id column); anything else is treated as a project
    directory and resolved purely via :func:`ocsm.projectid.resolve_project`
    (the registry is never written)."""
    if conn.execute("SELECT 1 FROM project WHERE id = ?", (value,)).fetchone() is not None:
        return value
    path = Path(value).expanduser()
    if not path.exists():
        console.print(f"[red]Error:[/red] project path not found: {value}")
        raise typer.Exit(1)
    try:
        return resolve_project(path).id
    except OSError as e:
        console.print(f"[red]Error:[/red] cannot resolve project path '{value}': {e}")
        raise typer.Exit(1)


@app.command("session")
def list_sessions_cmd(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", "-p", help="Filter by project id or directory"),
    flat: bool = typer.Option(False, "--flat", help="Include subagent sessions"),
    tree: bool = typer.Option(False, "--tree", help="Show sessions in tree structure"),
):
    """List sessions, optionally filtered by project."""
    if flat and tree:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)
    conn = open_db(ctx)
    try:
        project_id = _project_filter_id(conn, project) if project else None
        rows = queries.list_sessions(conn, project_id=project_id)
    finally:
        conn.close()
    if not flat and not tree:
        rows = [row for row in rows if not row["parent_id"]]
    if not rows:
        msg = "No sessions found."
        if project:
            msg = f"No sessions found for project '{project}'."
        console.print(msg)
        return
    console.print(format_sessions_tree(rows) if tree else format_sessions_list(rows))
