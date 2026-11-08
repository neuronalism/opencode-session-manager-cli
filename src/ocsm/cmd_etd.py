"""``ocsm export-then-delete`` — export sessions as import-safe raw JSON,
then permanently delete them from the DB (v1 ``etd`` UX on the v2 storage).

Flow shared by both commands:

1. resolve the targets and BFS-expand them along ``parent_id`` — subagent
   descendants always ride along (v1 ``get_session_tree`` /
   ``_etd_expand_trees`` semantics), so what gets deleted is exactly what was
   exported and no child is ever orphaned;
2. export via the shared :func:`ocsm.common.export_sessions` walker: raw JSON
   always (the import-safe format), markdown additionally with
   ``--format markdown``; the DB is untouched in this phase;
3. verify every export landed on disk non-empty — a missing/empty file aborts
   before any DB mutation;
4. checkpoint+backup the DB file (:func:`ocsm.queries.checkpoint_and_backup`),
   then remove each top-level tree with
   :func:`ocsm.queries.delete_session_tree_full` — ``recursive=True`` on the
   rows whose parent is outside the set covers the whole expanded set exactly
   once — and run :func:`ocsm.queries.verify_database`;
5. report exported files, per-table delete counts, the backup path and the
   verification result.

The ``project``/``worktree`` registry rows are never deleted: OpenCode keeps
the project registration when its sessions go away, so ocsm does too.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ocsm import queries
from ocsm.common import confirm_delete_by_retype, console, export_sessions, is_interactive, open_db
from ocsm.db import get_connection
from ocsm.projectid import resolve_project

app = typer.Typer(
    name="export-then-delete",
    help="Export sessions (raw JSON, import-safe) and then permanently delete them from the DB",
    no_args_is_help=True,
)

#: keys of the per-table counts returned by ``delete_session_tree_full``.
_COUNT_KEYS = ("sessions", "messages", "parts", "inbox", "events", "jobs")


# ---------------------------------------------------------------------------
# helpers (v1 ``_etd_*`` ports)


def _etd_validate_options(
    to: Path | None, to_project: Path | None, fmt: str, flat: bool, tree: bool
) -> None:
    """Shared validation for the two export-then-delete commands."""
    if (to is None) == (to_project is None):
        console.print("[red]Error:[/red] exactly one of --to / --to-project is required.")
        raise typer.Exit(1)
    if to is not None and to_project is not None:
        console.print("[red]Error:[/red] --to and --to-project are mutually exclusive.")
        raise typer.Exit(1)
    if fmt not in ("raw", "markdown"):
        console.print("[red]Error:[/red] --format must be one of: raw, markdown")
        raise typer.Exit(2)
    if flat and tree:
        console.print("[red]Error:[/red] --flat and --tree are mutually exclusive.")
        raise typer.Exit(1)


def _expand_trees(conn: sqlite3.Connection, root_ids: list[str]) -> list[str]:
    """BFS-expand root session ids to their full descendant closure.

    Mirrors v1 ``_etd_expand_trees`` / ``get_session_tree``: recursion follows
    ``parent_id`` downward, duplicates collapse, and the order is
    parents-first so the shared export walker sees each root at ``rows[0]``
    (``--tree`` nests everything else under it) and deletion can take every
    top-level tree with a single recursive call.
    """
    seen: dict[str, None] = {}
    queue = list(root_ids)
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen[current] = None
        for row in conn.execute(
            "SELECT id FROM session_v2 WHERE parent_id = ? ORDER BY id", (current,)
        ).fetchall():
            queue.append(row[0])
    return list(seen)


def _rows_for_ids(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    rows: list[dict] = []
    for sid in ids:
        row = queries.session_row(conn, sid)
        if row is not None:  # defensive: dropped between expand and read
            rows.append(row)
    return rows


def _top_level_rows(rows: list[dict]) -> list[dict]:
    """Rows whose parent is outside the expanded set.

    Deleting exactly these with ``recursive=True`` covers the whole set: every
    other row is a descendant of one of them (expansion is closed under
    descendants), and each session is counted exactly once.
    """
    ids = {row["id"] for row in rows}
    return [row for row in rows if not row["parent_id"] or row["parent_id"] not in ids]


def _locate_project(conn: sqlite3.Connection, value: str) -> str:
    """``--from`` as a project id (exact ``project`` table match) or as a
    directory, resolved with OpenCode's identity algorithm (pure resolution —
    nothing is registered in the DB)."""
    if value in {row["id"] for row in queries.list_projects(conn)}:
        return value
    return resolve_project(Path(value).expanduser()).id


def _etd_export(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    to: Path | None,
    to_project: Path | None,
    fmt_markdown: bool,
    tree: bool,
    thinking: bool,
    tool_calls: str,
    dry_run: bool,
) -> tuple[list[Path], list[Path]]:
    """Export phase shared by ``export-then-delete session|project``.

    Always writes raw JSON (import-safe); additionally writes markdown when
    ``fmt_markdown`` is True. Returns (raw_paths, markdown_paths). On a write
    error the exception propagates so the caller aborts before any DB mutation.
    """
    raw_paths = export_sessions(
        conn, rows, to=to, to_project=to_project, fmt="raw", tree=tree,
        thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
    )
    md_paths: list[Path] = []
    if fmt_markdown:
        md_paths = export_sessions(
            conn, rows, to=to, to_project=to_project, fmt="markdown", tree=tree,
            thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
        )
    return raw_paths, md_paths


def _verify_exported_files(paths: list[Path]) -> bool:
    """Every export must exist on disk and be non-empty before we delete."""
    bad = [p for p in paths if not p.is_file() or p.stat().st_size == 0]
    if not bad:
        return True
    console.print("[red]Error:[/red] export verification failed (missing or empty file):")
    for path in bad:
        console.print(f"  [red]{path}[/red]")
    console.print("[dim]DB is unchanged; nothing was deleted.[/dim]")
    return False


def _etd_dry_run_report(raw_paths: list[Path], md_paths: list[Path], full_id_set: list[str]) -> None:
    """Print paths + the deletion plan, then exit without touching the DB."""
    for path in raw_paths:
        console.print(f"Would export to {path}")
    for path in md_paths:
        console.print(f"Would export to {path}")
    console.print(f"\n[red]Would delete {len(full_id_set)} session(s) from DB:[/red]")
    for sid in full_id_set:
        console.print(f"  [red]x[/red] {sid}")
    console.print("\n[dim]--dry-run: no files written, nothing deleted.[/dim]")


def _confirm_delete(rows: list[dict], label: str, expected: str) -> None:
    """List what is about to be deleted, then the re-type confirmation gate.

    v1 semantics: always interactive, no ``--yes`` bypass — without a TTY the
    deletion is refused (exports stay on disk so the retry is cheap).
    """
    console.print(f"\n[red]The following {len(rows)} session(s) will be permanently deleted:[/red]")
    for row in rows:
        shown = f" — {row['title']}" if row["title"] else ""
        console.print(f"  {row['id']}{shown}")
    if not is_interactive():
        console.print("[red]Error:[/red] this command requires an interactive TTY to confirm deletion.")
        raise typer.Exit(1)
    if not confirm_delete_by_retype(label, expected):
        console.print(
            "[yellow]Delete aborted (re-type did not match). "
            "Exported files remain on disk so you can retry.[/yellow]"
        )
        raise typer.Exit(1)


def _etd_delete(ctx: typer.Context, rows: list[dict]) -> tuple[Path, dict, list[str]]:
    """Backup the DB file, then delete every top-level tree recursively.

    Returns (backup_path, per-table counts, verify_database problems). Each
    ``delete_session_tree_full`` call is its own transaction; on failure the
    DB may be partially updated, so the backup path is surfaced in the error.
    """
    backup_path = queries.checkpoint_and_backup(ctx.obj["db_path"])
    conn = open_db(ctx)
    try:
        totals = dict.fromkeys(_COUNT_KEYS, 0)
        for row in _top_level_rows(rows):
            counts = queries.delete_session_tree_full(conn, row["id"], recursive=True)
            for key in _COUNT_KEYS:
                totals[key] += counts.get(key, 0)
        problems = queries.verify_database(conn)
    except (queries.OcsmError, sqlite3.Error) as e:
        console.print(f"[red]Error during delete: {e}[/red]")
        console.print(
            f"[dim]The DB may be partially updated; "
            f"restore from the backup at {backup_path} if needed.[/dim]"
        )
        raise typer.Exit(1)
    finally:
        conn.close()
    return backup_path, totals, problems


def _etd_report(
    *,
    raw_paths: list[Path],
    md_paths: list[Path],
    deleted_ids: list[str],
    counts: dict,
    backup_path: Path,
    problems: list[str],
    raw_hint: Path,
    to_project: Path | None,
) -> None:
    """Print the export-then-delete result block."""
    for path in raw_paths:
        console.print(f"Exported to {path}")
    for path in md_paths:
        console.print(f"Exported to {path}")
    console.print(f"\n[red]Deleted {len(deleted_ids)} session(s) from DB[/red]")
    for sid in deleted_ids:
        console.print(f"  [red]x[/red] {sid}")
    detail = ", ".join(f"{key}={counts.get(key, 0)}" for key in _COUNT_KEYS)
    console.print(f"[dim]Deleted rows: {detail}[/dim]")
    console.print(f"\n[dim]Backup: {backup_path}[/dim]")
    console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")
    target = to_project if to_project else raw_paths[0].parent
    console.print(f"[dim]Restore with: ocsm import session --from {raw_hint} --to-project {target}[/dim]")
    if problems:
        for problem in problems:
            console.print(f"[red]verify_database: {problem}[/red]")
    else:
        console.print("[green]verify_database: OK (no problems found)[/green]")


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024


def _vacuum_db(db_path: Path) -> None:
    """VACUUM the DB file to return freed pages to the OS.

    A committed DELETE only moves pages onto SQLite's freelist — the file keeps
    its pre-delete size (and every future ocsm backup copies that bloat), so
    mass deletions should be followed by a VACUUM rebuild. Runs strictly after
    the deletion transaction; a failure here is non-fatal by design: the
    deletion is already committed and the DB is consistent, only the space
    reclamation is skipped.
    """
    before = db_path.stat().st_size
    console.print("\n[cyan]Vacuuming database (may take a while)...[/cyan]")
    try:
        conn = get_connection(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
    except sqlite3.Error as e:
        console.print(f"[yellow]Warning: VACUUM failed: {e}[/yellow]")
        console.print(
            "[dim]The deletion is already committed; only disk-space reclamation was skipped. "
            'Common causes: OpenCode is running (locked DB) or insufficient free disk. '
            'Retry later with: sqlite3 <db> "VACUUM"[/dim]'
        )
        return
    after = db_path.stat().st_size
    console.print(
        f"[green]Vacuumed DB:[/green] {_format_bytes(before)} -> {_format_bytes(after)} "
        f"(reclaimed {_format_bytes(before - after)})"
    )


# ---------------------------------------------------------------------------
# commands


@app.command("session")
def export_then_delete_session_cmd(
    ctx: typer.Context,
    session_id: str = typer.Option(..., "--from", help="Session ID to export, then delete"),
    to: Path | None = typer.Option(None, "--to", help="Output directory (mutually exclusive with --to-project)"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to this project's .opencode dir (mutually exclusive with --to)"),
    fmt: str = typer.Option("raw", "--format", "-f", help="Export format: raw (default, raw JSON only) or markdown (raw JSON + markdown)"),
    tree: bool = typer.Option(False, "--tree", help="Include subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Include subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking/--no-thinking", help="Markdown only: include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Markdown only: tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported/deleted and exit without writing or deleting"),
):
    """Export a session (always raw JSON) and then permanently delete it from the DB.

    Export runs first and is mandatory; deletion only proceeds after you re-type
    the exact session ID at the prompt. Subagent sessions in the same tree are
    exported and deleted together. Requires a TTY for the confirmation prompt.
    """
    _etd_validate_options(to, to_project, fmt, flat, tree)
    fmt_markdown = fmt == "markdown"

    conn = open_db(ctx)
    try:
        if queries.session_row(conn, session_id) is None:
            console.print(f"[red]Error:[/red] Session '{session_id}' not found.")
            raise typer.Exit(1)
        rows = _rows_for_ids(conn, _expand_trees(conn, [session_id]))
        if not rows:
            console.print(f"[red]Error:[/red] Session '{session_id}' not found.")
            raise typer.Exit(1)
        # Export phase (raw JSON, plus markdown if requested). DB is untouched here.
        try:
            raw_paths, md_paths = _etd_export(
                conn, rows, to=to, to_project=to_project, fmt_markdown=fmt_markdown,
                tree=tree, thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
            )
        except Exception as e:
            console.print(f"[red]Error during export: {e}[/red]")
            console.print("[dim]DB is unchanged; nothing was deleted.[/dim]")
            raise typer.Exit(1)
    finally:
        conn.close()

    full_id_set = [row["id"] for row in rows]
    console.print(f"\n[dim]Target: {len(full_id_set)} session(s) (1 root + {len(full_id_set) - 1} subagents).[/dim]")

    if dry_run:
        _etd_dry_run_report(raw_paths, md_paths, full_id_set)
        return

    if not _verify_exported_files(raw_paths + md_paths):
        raise typer.Exit(1)

    # Re-type confirmation gate (always interactive; no -y bypass).
    _confirm_delete(rows, "session ID", session_id)

    backup_path, counts, problems = _etd_delete(ctx, rows)
    _etd_report(
        raw_paths=raw_paths, md_paths=md_paths, deleted_ids=full_id_set,
        counts=counts, backup_path=backup_path, problems=problems,
        raw_hint=raw_paths[0], to_project=to_project,
    )
    if problems:
        raise typer.Exit(1)


@app.command("project")
def export_then_delete_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(..., "--from", help="Project directory path or project ID to export, then delete"),
    to: Path | None = typer.Option(None, "--to", help="Output directory (mutually exclusive with --to-project)"),
    to_project: Path | None = typer.Option(None, "--to-project", help="Output to this project's .opencode dir (mutually exclusive with --to)"),
    fmt: str = typer.Option("raw", "--format", "-f", help="Export format: raw (default, raw JSON only) or markdown (raw JSON + markdown)"),
    tree: bool = typer.Option(False, "--tree", help="Include subagent sessions (tree layout)"),
    flat: bool = typer.Option(False, "--flat", help="Include subagent sessions (flat layout)"),
    thinking: bool = typer.Option(True, "--thinking/--no-thinking", help="Markdown only: include reasoning parts"),
    tool_calls: str = typer.Option("info", "--tool-call", help="Markdown only: tool call detail level: none, info, details"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be exported/deleted and exit without writing or deleting"),
    vacuum: bool = typer.Option(False, "--vacuum", help="Rebuild the DB file after deletion to reclaim disk space (SQLite otherwise keeps deleted pages on its freelist)"),
):
    """Export all sessions of a project (always raw JSON) and then permanently delete them from the DB.

    ``--from`` accepts a project id or a directory. Export runs first and is
    mandatory; deletion only proceeds after you re-type the exact project path
    (or id) at the prompt. Every listed session is expanded to its full
    subagent tree so nothing is orphaned. Requires a TTY for the confirmation
    prompt. ``--vacuum`` rebuilds the DB file afterwards to reclaim disk space.
    """
    db_path = ctx.obj["db_path"]
    _etd_validate_options(to, to_project, fmt, flat, tree)
    fmt_markdown = fmt == "markdown"

    conn = open_db(ctx)
    try:
        project_id = _locate_project(conn, project)
        root_rows = queries.list_sessions(conn, project_id=project_id)
        if not root_rows:
            console.print(f"[red]Error:[/red] No sessions found for project '{project}'.")
            raise typer.Exit(1)
        # Expand to whole trees so every subagent is exported AND deleted (no orphans).
        rows = _rows_for_ids(conn, _expand_trees(conn, [row["id"] for row in root_rows]))
        # Export phase (raw JSON, plus markdown if requested). DB is untouched here.
        try:
            raw_paths, md_paths = _etd_export(
                conn, rows, to=to, to_project=to_project, fmt_markdown=fmt_markdown,
                tree=tree, thinking=thinking, tool_calls=tool_calls, dry_run=dry_run,
            )
        except Exception as e:
            console.print(f"[red]Error during export: {e}[/red]")
            console.print("[dim]DB is unchanged; nothing was deleted.[/dim]")
            raise typer.Exit(1)
    finally:
        conn.close()

    full_id_set = [row["id"] for row in rows]
    console.print(f"\n[dim]Target: {len(full_id_set)} session(s) across {len(root_rows)} root(s).[/dim]")

    if dry_run:
        _etd_dry_run_report(raw_paths, md_paths, full_id_set)
        if vacuum:
            console.print("[dim]--vacuum: would rebuild the DB file after deletion to reclaim freed pages.[/dim]")
        return

    if not _verify_exported_files(raw_paths + md_paths):
        raise typer.Exit(1)

    # Re-type confirmation gate (always interactive; no -y bypass).
    _confirm_delete(rows, "project path", project)

    backup_path, counts, problems = _etd_delete(ctx, rows)
    _etd_report(
        raw_paths=raw_paths, md_paths=md_paths, deleted_ids=full_id_set,
        counts=counts, backup_path=backup_path, problems=problems,
        raw_hint=raw_paths[0], to_project=to_project,
    )
    if problems:
        raise typer.Exit(1)
    if vacuum:
        _vacuum_db(db_path)
