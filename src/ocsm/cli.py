"""ocsm CLI assembly: the global ``--db`` option plus the six subapps.

All behavior lives in the ``cmd_*`` modules (each mounting its own typer app)
on top of the v2 storage layer in :mod:`ocsm.queries`. OpenCode 2.x reads and
writes only the v2 tables (``session_v2``/``session_message``/...), so every
ocsm command targets those tables exclusively; the legacy v1 tables and the
deprecated ``project_directory`` table are never touched.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ocsm.cmd_etd import app as etd_app
from ocsm.cmd_export import app as export_app
from ocsm.cmd_import import app as import_app
from ocsm.cmd_list import app as list_app
from ocsm.cmd_move import app as move_app
from ocsm.cmd_sync import app as sync_app
from ocsm.common import console, open_db
from ocsm.db import resolve_db_path
from ocsm import queries

app = typer.Typer(name="ocsm", help="OpenCode Sessions Manager", no_args_is_help=True)

app.add_typer(list_app)
app.add_typer(export_app)
app.add_typer(import_app)
app.add_typer(move_app)
app.add_typer(sync_app)
app.add_typer(etd_app)


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


@app.command("verify")
def verify_cmd(ctx: typer.Context):
    """Sanity-check the v2 tables: integrity, foreign keys, orphans, and the
    ``event_sequence`` watermark rows every session needs (replaces the v1
    ``opencode db`` based verification, which the 2.x CLI no longer offers)."""
    conn = open_db(ctx)
    try:
        problems = queries.verify_database(conn, [])
        missing = queries.missing_event_sequences(conn)
        for sid in missing:
            problems.append(f"session {sid} has no event_sequence row")
    finally:
        conn.close()
    if problems:
        console.print(f"[red]{len(problems)} problem(s) found:[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(1)
    console.print("[green]OK:[/green] integrity, foreign keys, orphans, event_sequence rows all clean.")
