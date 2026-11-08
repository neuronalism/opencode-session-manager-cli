"""``ocsm import`` subcommands: rebuild v2 session trees from raw JSON exports.

An import payload is what ``ocsm export ... --format raw`` writes:
``{"session": <full session_v2 row>, "messages": [...], "blobs": [...]}``.
Writes go exclusively through :func:`ocsm.queries.insert_session_tree`,
which follows OpenCode 2.x ``session/transfer.ts`` import semantics: session
ids are preserved (an existing id is a hard conflict), ``project_id`` /
``directory`` / ``path`` are recomputed for the destination, message ids and
seqs survive verbatim, and an ``event_sequence`` watermark row is created so
the next OpenCode append cannot collide with the imported messages.

v1 exports (``{"session", "messages", "parts"[, "todos"]}`` around the legacy
29-column session row) are rejected up front: they target the v1 tables ocsm
no longer writes; restore them with ocsm from the v1 branch.

Safety pipeline per v1 habits: WAL checkpoint + timestamped backup before the
first write, single-transaction inserts (rolled back on failure), then
:func:`ocsm.queries.verify_database` and a report with the backup path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import typer

from ocsm import queries
from ocsm.common import console, open_db
from ocsm.projectid import ResolvedProject, resolve_project

app = typer.Typer(name="import", help="Import sessions from raw JSON into the database", no_args_is_help=True)

_V1_REJECT = "ocsm v1 export format is no longer supported; restore it with ocsm from the v1 branch instead"

#: Columns that only exist on ``session_v2``. A v2 export always carries the
#: full 37-column row, while a v1 export's session object has none of them.
_V2_ONLY_SESSION_COLUMNS = (
    "fork_session_id", "fork_boundary", "time_suspended", "idle_outcome",
    "resume_attempts", "time_idle", "time_viewed", "time_busy",
)


# ---------------------------------------------------------------------------
# payload loading / validation


def _load_payload(path: Path) -> dict:
    """Read and parse one raw JSON export; ``ValueError`` on any failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"failed to read raw JSON file '{path}': {e}")
    if not isinstance(data, dict):
        raise ValueError(f"invalid raw JSON file '{path}': top-level value must be an object")
    return data


def _check_payload(payload: dict, source: str) -> None:
    """Shape pre-check before anything is written.

    ``ValueError`` messages are printed verbatim by the callers, so they are
    user-facing: distinguish "broken file", "v1 export" and "wrong shape".
    """
    session = payload.get("session")
    if not isinstance(session, dict) or not session.get("id"):
        raise ValueError(f"{source}: invalid raw JSON payload: 'session' must be an object with an 'id'")
    if "parts" in payload or "todos" in payload or not any(c in session for c in _V2_ONLY_SESSION_COLUMNS):
        raise ValueError(f"{source}: {_V1_REJECT}")
    if not isinstance(payload.get("messages"), list):
        raise ValueError(f"{source}: invalid raw JSON payload: 'messages' must be a list")


# ---------------------------------------------------------------------------
# planning


def _plan_entry(
    conn: sqlite3.Connection,
    payload: dict,
    resolved: ResolvedProject,
    target_directory: Path,
    *,
    batch_ids: frozenset[str] = frozenset(),
) -> dict:
    """Read-only preview of one import: identity, placement and risk flags.

    ``batch_ids`` are the other session ids being imported in the same run; a
    parent inside the batch counts as resolvable (the project command defers
    children to a second round until their parent lands).
    """
    session = payload["session"]
    place = queries.placement(resolved, target_directory)
    parent_id = session.get("parent_id")
    return {
        "id": session["id"],
        "title": session.get("title"),
        "messages": len(payload.get("messages") or []),
        "blobs": len(payload.get("blobs") or []),
        "parent_id": parent_id or None,
        "project_id": place["project_id"],
        "directory": place["directory"],
        "path": place["path"],
        "conflict": queries.session_row(conn, session["id"]) is not None,
        "parent_missing": bool(parent_id)
        and parent_id not in batch_ids
        and queries.session_row(conn, parent_id) is None,
    }


def _status(entry: dict) -> tuple[str, str]:
    if entry["conflict"]:
        return "conflict: session id already in database", "yellow"
    if entry["parent_missing"]:
        return f"blocked: parent {entry['parent_id']} not in database or batch", "yellow"
    return "new", "green"


def _print_plan(entries: list[dict], target_directory: Path, *, dry: bool) -> None:
    verb = "Would import" if dry else "Importing"
    hint = "would be set to" if dry else "set to"
    for entry in entries:
        status, color = _status(entry)
        title = f" — {entry['title']}" if entry["title"] else ""
        console.print(f"[{color}]{verb} {entry['id']}{title}[/{color}]")
        console.print(f"[dim]    messages: {entry['messages']}, blobs: {entry['blobs']}[/dim]")
        console.print(f"[dim]    project_id: {entry['project_id']}[/dim]")
        console.print(f"[dim]    directory: {entry['directory']}[/dim]")
        console.print(f"[dim]    path: {entry['path']}[/dim]")
        console.print(f"[dim]    status: {status}[/dim]")
    if entries:
        console.print(
            f"[dim]project_id {hint} the id resolved from the target directory[/dim]"
        )
    console.print(f"[dim]Target directory: {target_directory}[/dim]")


def _print_report(entries: list[dict], *, backup: Path | None, problems: list[str]) -> None:
    console.print(f"[green]Imported {len(entries)} session(s):[/green]")
    for entry in entries:
        title = f" — {entry['title']}" if entry["title"] else ""
        console.print(f"  {entry['id']}{title}")
    total_messages = sum(e["messages"] for e in entries)
    total_blobs = sum(e["blobs"] for e in entries)
    console.print(
        f"[dim]  inserted {len(entries)} session row(s), {total_messages} message row(s),"
        f" {total_blobs} blob row(s)[/dim]"
    )
    console.print(f"[dim]  project_id set to '{entries[0]['project_id']}' — resolved from the target directory[/dim]")
    if problems:
        console.print("[red]Verification problems:[/red]")
        for problem in problems:
            console.print(f"  [red]{problem}[/red]")
    else:
        console.print("[dim]  verification: no problems found[/dim]")
    if backup is not None:
        console.print(f"\n[dim]Backup: {backup}[/dim]")
        console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")


# ---------------------------------------------------------------------------
# commands


@app.command("session")
def import_session_cmd(
    ctx: typer.Context,
    file: Path = typer.Option(..., "--file", help="Raw JSON file exported by 'ocsm export --format raw'"),
    directory: Path = typer.Option(
        None,
        "--directory",
        "-d",
        help="Target project directory; the session is re-placed under the project resolved from it (default: current directory)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the import plan without writing or backing up"),
):
    """Import one session (its messages and blobs) from a raw JSON export."""
    db_path = ctx.obj["db_path"]
    target = (directory or Path.cwd()).expanduser().resolve()
    try:
        payload = _load_payload(file.expanduser())
        _check_payload(payload, str(file))
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    conn = open_db(ctx)
    try:
        session_id = payload["session"]["id"]
        resolved = resolve_project(target)  # pure: no DB writes, dry-run safe
        entry = _plan_entry(conn, payload, resolved, target)

        if dry_run:
            _print_plan([entry], target, dry=True)
            console.print("\n[dim]--dry-run: no changes made, no backup created.[/dim]")
            return
        # Fail before the backup when this import is doomed anyway.
        if entry["conflict"]:
            console.print(f"[red]Error:[/red] session {session_id} already exists in the database; nothing was imported.")
            raise typer.Exit(1)
        if entry["parent_missing"]:
            console.print(
                f"[red]Error:[/red] parent session {entry['parent_id']} not found in the database;"
                " import the parent session first."
            )
            raise typer.Exit(1)

        backup = queries.checkpoint_and_backup(db_path)
        _print_plan([entry], target, dry=False)
        try:
            resolved = queries.resolve_and_register(conn, target)
            queries.insert_session_tree(conn, payload, resolved=resolved, target_directory=target)
        except queries.SessionExistsError as e:
            console.print(f"[red]Error:[/red] session {e.session_id} already exists in the database; nothing was imported.")
            raise typer.Exit(1)
        except queries.ParentNotFoundError as e:
            console.print(
                f"[red]Error:[/red] parent session {e.parent_id} not found in the database;"
                " import the parent session first."
            )
            raise typer.Exit(1)
        except queries.OcsmError as e:
            console.print(f"[red]Error during import:[/red] {e}")
            console.print("[dim]The database was rolled back. Your data is unchanged.[/dim]")
            raise typer.Exit(1)
        except Exception as e:  # v1 habit: report instead of a raw traceback
            console.print(f"[red]Error during import:[/red] {e}")
            console.print("[dim]The database was rolled back. Your data is unchanged.[/dim]")
            raise typer.Exit(1)
        problems = queries.verify_database(conn, [session_id])
        _print_report([entry], backup=backup, problems=problems)
        if problems:
            raise typer.Exit(1)
    finally:
        conn.close()


@app.command("project")
def import_project_cmd(
    ctx: typer.Context,
    from_dir: Path = typer.Option(
        ...,
        "--from-dir",
        help="Directory holding the raw JSON exports (e.g. <project>/.opencode/raw_conversations)",
    ),
    directory: Path = typer.Option(
        None,
        "--directory",
        "-d",
        help="Target project directory for every imported session (default: current directory)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the import plan without writing or backing up"),
):
    """Import every raw JSON session in a directory, skipping conflicts."""
    db_path = ctx.obj["db_path"]
    target = (directory or Path.cwd()).expanduser().resolve()
    if not from_dir.is_dir():
        console.print(f"[red]Error:[/red] directory not found: '{from_dir}'")
        raise typer.Exit(1)
    # Flat layout (one file per session) plus one nesting level ('--tree'
    # exports group subagents under <root-id>/).
    files = [p for p in sorted(from_dir.glob("*.json")) if p.is_file()]
    files += [p for p in sorted(from_dir.glob("*/*.json")) if p.is_file()]
    if not files:
        console.print(f"[red]Error:[/red] no JSON files found in '{from_dir}'")
        raise typer.Exit(1)

    conn = open_db(ctx)
    try:
        payloads: list[tuple[Path, dict]] = []
        skipped: list[tuple[str, str]] = []  # (label, reason) — pre-batch skips
        failed: list[tuple[str, str]] = []  # (label, reason) — validation failures
        seen: set[str] = set()
        for path in files:
            try:
                payload = _load_payload(path)
                _check_payload(payload, path.name)
            except ValueError as e:
                failed.append((path.name, str(e)))
                continue
            session_id = payload["session"]["id"]
            if session_id in seen:
                skipped.append((path.name, f"duplicate session id {session_id} in batch"))
                continue
            seen.add(session_id)
            payloads.append((path, payload))

        if not payloads:
            console.print(f"[red]Error:[/red] no importable sessions found in '{from_dir}'")
            for label, reason in failed:
                console.print(f"  [red]{label}: {reason}[/red]")
            raise typer.Exit(1)

        # Dependency order: sessions whose parent is NOT another batch member
        # go first; in-batch children are retried in a second round below.
        batch_ids = frozenset(p["session"]["id"] for _, p in payloads)
        payloads.sort(key=lambda item: item[1]["session"].get("parent_id") in batch_ids)

        resolved = resolve_project(target)
        entries: dict[str, dict] = {}
        for path, payload in payloads:
            entries[payload["session"]["id"]] = _plan_entry(
                conn, payload, resolved, target, batch_ids=batch_ids
            )
        plan = [entries[p["session"]["id"]] for _, p in payloads]
        console.print(f"Found {len(plan)} session(s) to import from '{from_dir}'")

        if dry_run:
            _print_plan(plan, target, dry=True)
            would_import = [e for e in plan if not e["conflict"] and not e["parent_missing"]]
            would_skip = [e for e in plan if e["conflict"]] + skipped
            would_fail = [e for e in plan if e["parent_missing"]] + failed
            console.print(
                f"\nSummary: would import {len(would_import)},"
                f" skip {len(would_skip)}, fail {len(would_fail)}."
            )
            console.print("\n[dim]--dry-run: no changes made, no backup created.[/dim]")
            return

        backup = queries.checkpoint_and_backup(db_path)
        resolved = queries.resolve_and_register(conn, target)

        imported: list[dict] = []
        deferred: list[tuple[Path, dict]] = []

        def try_import(path: Path, payload: dict) -> bool:
            """One insert attempt. Returns True only when the session landed;
            ``ParentNotFoundError`` defers, everything else is classified."""
            try:
                queries.insert_session_tree(conn, payload, resolved=resolved, target_directory=target)
            except queries.SessionExistsError as e:
                skipped.append((path.name, f"session {e.session_id} already exists in the database"))
            except queries.ParentNotFoundError as e:
                deferred.append((path, payload))
                console.print(
                    f"[yellow]Deferring {path.name}: parent session {e.parent_id}"
                    " not in the database yet; retrying after the rest of the batch.[/yellow]"
                )
            except Exception as e:  # v1 habit: report and keep the batch going
                failed.append((path.name, str(e)))
                console.print(f"[red]Failed {path.name}:[/red] {e}")
            else:
                imported.append(entries[payload["session"]["id"]])
                return True
            return False

        for path, payload in payloads:
            try_import(path, payload)
        # Second round for in-batch children (usually one extra pass; keep
        # going only while parents keep landing).
        while deferred:
            round_items, deferred[:] = deferred[:], []
            results = [try_import(path, payload) for path, payload in round_items]
            if not any(results):
                for path, payload in deferred:
                    failed.append((path.name, f"parent session {payload['session'].get('parent_id')} not found in the database or batch"))
                    console.print(
                        f"[red]Failed {path.name}:[/red] parent session"
                        f" {payload['session'].get('parent_id')} not found"
                    )
                break

        problems = queries.verify_database(conn, [e["id"] for e in imported]) if imported else []
        console.print("")
        if imported:
            console.print(f"[green]Imported {len(imported)} session(s):[/green]")
            for entry in imported:
                title = f" — {entry['title']}" if entry["title"] else ""
                console.print(f"  [green]{entry['id']}{title}[/green]")
        if skipped:
            console.print(f"[yellow]Skipped {len(skipped)} session(s):[/yellow]")
            for label, reason in skipped:
                console.print(f"  [yellow]{label}: {reason}[/yellow]")
        if failed:
            console.print(f"[red]Failed {len(failed)} session(s):[/red]")
            for label, reason in failed:
                console.print(f"  [red]{label}: {reason}[/red]")
        if problems:
            console.print("[red]Verification problems:[/red]")
            for problem in problems:
                console.print(f"  [red]{problem}[/red]")
        total_messages = sum(e["messages"] for e in imported)
        console.print(
            f"Summary: imported {len(imported)}, skipped {len(skipped)},"
            f" failed {len(failed)} ({total_messages} message row(s) inserted)."
        )
        console.print(f"\n[dim]Backup: {backup}[/dim]")
        console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")
        if not imported or problems:
            raise typer.Exit(1)
    finally:
        conn.close()
