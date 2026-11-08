"""``ocsm export`` — write sessions out as markdown, readable JSON, or
import-safe raw JSON.

Both commands are read-only walks over the v2 tables that delegate every
rendering and placement decision to the shared :func:`ocsm.common.export_sessions`
walker. ``--tree``/``--flat`` widen the export to the full subagent subtree
(recursed locally over ``parent_id``); by default only the session itself —
resp. the project's root sessions — is written, matching the v1 UX.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ocsm.common import console, export_sessions, open_db
from ocsm.projectid import resolve_project
from ocsm import queries

app = typer.Typer(
    name="export",
    help="Export sessions as markdown, JSON or raw JSON",
    no_args_is_help=True,
)

_FORMATS = ("markdown", "json", "raw")
_TOOL_CALL_LEVELS = ("none", "info", "details")


def _children_map(rows: list[dict]) -> dict[str | None, list[dict]]:
    """Group session rows by their ``parent_id``."""
    children: dict[str | None, list[dict]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    return children


def _subtree(children: dict[str | None, list[dict]], root_id: str) -> list[dict]:
    """All descendants of ``root_id``, breadth-first (the root itself excluded)."""
    out: list[dict] = []
    frontier = [root_id]
    while frontier:
        next_frontier: list[str] = []
        for parent in frontier:
            for row in children.get(parent, []):
                out.append(row)
                next_frontier.append(row["id"])
        frontier = next_frontier
    return out


def _validate_options(fmt: str, tool_calls: str, *, tree: bool, flat: bool) -> None:
    if tree and flat:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)
    if fmt not in _FORMATS:
        console.print(
            f"[red]Error:[/red] Unknown format '{fmt}' (expected: {'/'.join(_FORMATS)})."
        )
        raise typer.Exit(1)
    if tool_calls not in _TOOL_CALL_LEVELS:
        console.print(
            f"[red]Error:[/red] Unknown tool call level '{tool_calls}'"
            f" (expected: {'/'.join(_TOOL_CALL_LEVELS)})."
        )
        raise typer.Exit(1)


def _report(exported: list[Path], *, dry_run: bool) -> None:
    verb = "Would export to" if dry_run else "Exported to"
    for path in exported:
        console.print(f"{verb} {path}")
    if dry_run:
        console.print("\n[dim]--dry-run: no files written.[/dim]")


@app.command("session")
def export_session_cmd(
    ctx: typer.Context,
    session_id: str = typer.Option(..., "--from", help="Session ID to export"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to project's .opencode dir"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown, json or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export session and all subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export session and all subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported and exit without writing"),
):
    """Export a session as markdown, JSON or raw JSON."""
    _validate_options(fmt, tool_calls, tree=tree, flat=flat)
    conn = open_db(ctx)
    try:
        row = queries.session_row(conn, session_id)
        if row is None:
            console.print(f"[red]Error:[/red] Session '{session_id}' not found.")
            raise typer.Exit(1)
        rows = [row]
        if tree or flat:
            children = _children_map(queries.list_sessions(conn))
            rows += _subtree(children, session_id)
        exported = export_sessions(
            conn, rows, to=to, to_project=to_project, fmt=fmt, tree=tree,
            thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
        )
    finally:
        conn.close()
    _report(exported, dry_run=dry_run)


@app.command("project")
def export_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(..., "--from", help="Project directory or project ID"),
    to: Path | None = typer.Option(None, "--to", help="Output directory"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to project's .opencode dir"),
    fmt: str = typer.Option("markdown", "--format", "-f", help="Export format: markdown, json or raw"),
    tree: bool = typer.Option(False, "--tree", help="Export with subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Export with subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking", help="Include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported and exit without writing"),
):
    """Export all sessions of a project.

    ``--from`` accepts a project id (exact match against the ``project`` table)
    or a directory, resolved with OpenCode's identity algorithm. ``--tree``
    gives every root session its own subdirectory holding its subagent
    descendants; by default only the project's root sessions are exported.
    """
    _validate_options(fmt, tool_calls, tree=tree, flat=flat)
    conn = open_db(ctx)
    try:
        if project in {row["id"] for row in queries.list_projects(conn)}:
            project_id = project
        else:
            project_id = resolve_project(Path(project).expanduser()).id
        rows = queries.list_sessions(conn, project_id=project_id)
        own_ids = {row["id"] for row in rows}

        def is_root(row: dict) -> bool:
            # A session whose parent is gone (or lives in another project)
            # roots its own subtree instead of being silently dropped.
            return not row["parent_id"] or row["parent_id"] not in own_ids

        if tree:
            children = _children_map(rows)
            groups = [[root, *_subtree(children, root["id"])] for root in rows if is_root(root)]
        else:
            selected = rows if flat else [row for row in rows if is_root(row)]
            groups = [selected] if selected else []
        if not any(groups):
            console.print(f"[red]Error:[/red] No sessions found for project '{project}'.")
            raise typer.Exit(1)
        exported: list[Path] = []
        for group in groups:
            exported.extend(
                export_sessions(
                    conn, group, to=to, to_project=to_project, fmt=fmt, tree=tree,
                    thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
                )
            )
    finally:
        conn.close()
    _report(exported, dry_run=dry_run)
