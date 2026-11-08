"""Shared CLI plumbing for the ocsm v2 subcommand modules.

Every ``cmd_*.py`` module mounts a typer app that relies on the global
``--db`` option resolved in :mod:`ocsm.cli` (``ctx.obj["db_path"]``), this
console (UTF-8 safe on Windows), the confirmation gates, and the shared
export walker.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import typer
from rich.console import Console

from ocsm import queries
from ocsm.format import session_to_json, session_to_markdown, session_to_raw_json


def _make_console() -> Console:
    if hasattr(sys.stdout, "buffer"):
        out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        err = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        return Console(file=out, stderr=err, legacy_windows=False)
    return Console(legacy_windows=False)


console = _make_console()
# Keep the original console referenced even if tests rebind the public name,
# otherwise GC of its TextIOWrapper would close the real stdout/stderr buffers.
_console_reference = console


def open_db(ctx: typer.Context, *, check_schema: bool = True) -> sqlite3.Connection:
    """Open the database selected by the global ``--db`` option and verify it
    carries the v2 tables."""
    from ocsm.db import get_connection

    conn = get_connection(ctx.obj["db_path"])
    if check_schema:
        try:
            queries.check_v2_schema(conn)
        except queries.OcsmError as e:
            conn.close()
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
    return conn


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_delete_confirmation(label: str, items: list[tuple[str, str | None]]) -> bool:
    """List what is about to be deleted and ask for a plain y/n."""
    console.print(f"\n[red]The following {label} will be permanently deleted:[/red]")
    for session_id, title in items:
        shown = f" — {title}" if title else ""
        console.print(f"  {session_id}{shown}")
    if not is_interactive():
        console.print("[red]Refusing to delete without confirmation (no TTY).[/red]")
        return False
    try:
        answer = input("\nType 'y' to confirm deletion: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() == "y"


def confirm_delete_by_retype(label: str, expected: str) -> bool:
    """Hard confirmation gate: re-type the exact identifier to proceed."""
    console.print(f"[red]This will permanently delete {label}.[/red]")
    if not is_interactive():
        console.print("[red]Refusing to delete without confirmation (no TTY).[/red]")
        return False
    try:
        answer = input(f"Type '{expected}' to confirm: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == expected


def export_sessions(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    to: Path | None,
    to_project: Path | None,
    fmt: str,
    tree: bool,
    thinking: bool,
    tool_calls: str,
    dry_run: bool = False,
) -> list[Path]:
    """Write every session row to ``<id>.<ext>`` under the chosen directory.

    Mirrors the v1 export walker: raw payloads land in ``raw_conversations/``,
    markdown in ``conversations/`` (either directly under ``--to`` or in the
    project's ``.opencode`` dir); ``--tree`` nests children under the root id.
    """
    if fmt not in ("markdown", "json", "raw"):
        raise ValueError(f"unknown export format: {fmt}")
    is_raw = fmt == "raw"
    ext = "json" if is_raw or fmt == "json" else "md"
    sub_dir = "raw_conversations" if is_raw else "conversations"

    exported: list[Path] = []
    root_id = rows[0]["id"] if rows else None
    for row in rows:
        sid = row["id"]
        messages = queries.session_messages(conn, sid)

        if is_raw:
            blobs = queries.export_blobs(conn, sorted(queries.message_media_refs(messages)))
            content = session_to_raw_json(dict(row), messages, blobs)
        elif fmt == "json":
            content = session_to_json(dict(row), messages)
        else:
            content = session_to_markdown(
                dict(row), messages, thinking=thinking, tool_calls=tool_calls,
                todos=queries.latest_todos(messages),
            )

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
