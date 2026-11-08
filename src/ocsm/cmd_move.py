"""``ocsm move`` — re-place sessions under a new project directory.

Semantics follow OpenCode 2.2.5 ``SessionEvent.Moved``
(``session/projector.ts:569-585``): a move rewrites only the ``session_v2``
placement columns (``project_id``/``directory``/``path``) and registers the
target's ``project``/``worktree`` rows. Message payloads keep the paths they
were recorded with, exactly like OpenCode's own move — v1 ocsm additionally
substituted old paths inside message data, which v2 intentionally drops.

UX follows the v1 ``move project`` command: ``--from`` (old project directory
or its project id) and ``--to`` (new directory, which must exist on disk so
``Project.resolve`` sees the same identity OpenCode will), a plan preview,
then a pre-write backup, the move itself, and a post-move report. Like v1
there is no interactive confirmation gate — the backup is the safety net;
``--dry-run`` shows the plan without writing anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer
from rich.table import Table

from ocsm import queries
from ocsm.common import console, open_db
from ocsm.projectid import resolve_project, to_storage_path

app = typer.Typer(name="move", help="Re-place sessions under a new project directory", no_args_is_help=True)


def _old_project_id(conn: sqlite3.Connection, from_ref: str) -> str:
    """``--from`` accepts a project id (exact match against ``project.id``)
    or a directory, resolved purely like ``Project.resolve`` — the old
    directory may no longer exist on disk, the directory hash still is."""
    for project in queries.list_projects(conn):
        if project["id"] == from_ref:
            return from_ref
    return resolve_project(from_ref).id


def _print_project_overview(conn: sqlite3.Connection) -> None:
    """n=0 troubleshooting aid: the projects the database actually knows."""
    projects = queries.list_projects(conn)
    if not projects:
        console.print("[dim]The project table is empty.[/dim]")
        return
    table = Table(title="Projects currently in the database")
    table.add_column("project id", overflow="fold")
    table.add_column("worktree", overflow="fold")
    table.add_column("sessions", justify="right")
    for project in projects:
        table.add_row(project["id"], project["worktree"] or "", str(project["session_count"]))
    console.print(table)


@app.command("project")
def move_project_cmd(
    ctx: typer.Context,
    from_ref: str = typer.Option(
        ..., "--from", help="Old project directory path (or its project id)"
    ),
    to_dir: Path = typer.Option(
        ..., "--to", help="New project directory path (must exist on disk)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the move plan and exit without writing anything"
    ),
) -> None:
    """Move all sessions of one project under a new project directory."""
    if not to_dir.is_dir():
        console.print(f"[red]Error:[/red] target directory does not exist: {to_dir}")
        raise typer.Exit(1)

    conn = open_db(ctx)
    try:
        old_project_id = _old_project_id(conn, from_ref)
        rows = queries.list_sessions(conn, project_id=old_project_id)
        if not rows:
            console.print(
                f"[yellow]No sessions are attached to project '{old_project_id}'.[/yellow]"
            )
            _print_project_overview(conn)
            raise typer.Exit(1)

        to_resolved = resolve_project(to_dir)
        # Every session lands at the project root, so the placement is uniform.
        place = queries.placement(to_resolved, to_dir)
        if all(
            row["project_id"] == place["project_id"]
            and row["directory"] == place["directory"]
            and row["path"] == place["path"]
            for row in rows
        ):
            console.print(
                "[yellow]Sessions are already at the target placement. Nothing to do.[/yellow]"
            )
            raise typer.Exit(0)

        console.print(
            f"Moving {len(rows)} session(s) from project '{old_project_id}'"
            f" to '{place['directory']}'"
        )
        table = Table(title="Sessions to move")
        table.add_column("session", overflow="fold")
        table.add_column("title", overflow="fold")
        table.add_column("old directory", overflow="fold")
        table.add_column("new directory", overflow="fold")
        table.add_column("new path", overflow="fold")
        for row in rows:
            table.add_row(
                row["id"],
                row["title"] or "",
                row["directory"],
                place["directory"],
                place["path"],
            )
        console.print(table)
        console.print(f"Target project id: [bold]{to_resolved.id}[/bold]")

        if dry_run:
            console.print("\n[dim]--dry-run: no changes made, no backup created.[/dim]")
            return

        backup_path = queries.checkpoint_and_backup(ctx.obj["db_path"])
        to_resolved = queries.resolve_and_register(conn, to_dir)

        moved = 0
        try:
            for row in rows:
                queries.move_session(
                    conn, row["id"], resolved=to_resolved, target_directory=to_dir
                )
                moved += 1
        except (queries.OcsmError, sqlite3.Error) as e:
            console.print(
                f"[red]Error:[/red] move failed after {moved}/{len(rows)} session(s): {e}"
            )
            console.print(f"[dim]Backup of the pre-move database: {backup_path}[/dim]")
            raise typer.Exit(1)

        problems = queries.verify_database(conn, [row["id"] for row in rows])
        if problems:
            for problem in problems:
                console.print(f"[red]Verification problem:[/red] {problem}")
            raise typer.Exit(1)

        console.print(f"[green]Moved {moved} session(s) to '{place['directory']}'[/green]")
        console.print(f"  new project_id: {to_resolved.id}")
        console.print(f"[dim]Backup: {backup_path}[/dim]")
        console.print(
            "[dim]Delete the backup manually once you confirm everything works.[/dim]"
        )
    finally:
        conn.close()
