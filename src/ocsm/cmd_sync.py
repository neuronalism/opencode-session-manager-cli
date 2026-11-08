"""``ocsm sync`` — two-way sync (incl. deletions) between the v2 database and a
project folder's ``.opencode/raw_conversations/``.

Ported from the v1 implementation (``.zcode/ref-v1/cli.py``, sync section) onto
the v2 storage layer (:mod:`ocsm.queries`):

- the DB side is scoped by project id — ``--project`` accepts a directory
  (resolved purely via :func:`ocsm.projectid.resolve_project`) or a project id
  (located through the ``project`` table);
- pushes (DB -> folder) reuse the shared exporter
  :func:`ocsm.common.export_sessions` in raw format, one call per session tree
  so children nest under their root id like v1's tree layout;
- pulls (folder -> DB) go through :func:`ocsm.queries.insert_session_tree`
  with the project registered via :func:`ocsm.queries.resolve_and_register`;
  an id already in the DB is overwritten delete-first
  (``delete_session_tree_full(recursive=False)`` — v1 ``replace_session``
  semantics: children of the replaced session survive);
- deletions propagate both ways using the ``.ocsm-sync.json`` manifest memory
  (same format/version as v1); DB-side deletions use
  ``delete_session_tree_full`` (recursive, whole subtree);
- before any DB write the database is checkpointed and backed up
  (:func:`ocsm.queries.checkpoint_and_backup`); after a DB-changing sync the
  database is verified (:func:`ocsm.queries.verify_database`);
- v1's ``substitute_paths`` logic is gone: v2 imports recompute the placement
  columns (``project_id``/``directory``/``path``) from the project registry,
  mirroring OpenCode's own transfer import.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import typer

from ocsm import queries
from ocsm.common import console, export_sessions, is_interactive, open_db
from ocsm.db import get_connection
from ocsm.format import format_timestamp
from ocsm.projectid import resolve_project

app = typer.Typer(
    name="sync",
    help="Two-way sync (incl. deletions) between DB and a project folder",
    no_args_is_help=True,
)

# Manifest constants for deletion propagation (format kept from v1).
MANIFEST_VERSION = 1
MANIFEST_FILENAME = ".ocsm-sync.json"
RAW_SUBDIR = "raw_conversations"


# ---------------------------------------------------------------------------
# manifest + folder scanning


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
        console.print(
            f"[yellow]Warning: manifest at {path} is unreadable; treating this as"
            " a first sync (no deletions propagated).[/yellow]"
        )
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


def _load_raw_file(path: Path) -> dict:
    """Load and parse a raw JSON payload file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Failed to read raw JSON file '{path}': {e}")


def _validate_raw_payload(data) -> tuple[dict, list[dict]]:
    """Validate a v2 raw payload shape ``{"session", "messages", "blobs"?}``."""
    if not isinstance(data, dict):
        raise ValueError("Invalid raw JSON: top level must be an object")
    session = data.get("session")
    if not isinstance(session, dict) or "id" not in session:
        raise ValueError("Invalid raw JSON: 'session' must be a dict with 'id'")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Invalid raw JSON: 'messages' must be a list")
    return session, messages


def _load_folder_sessions(raw_dir: Path) -> dict[str, dict]:
    """Scan a raw_conversations dir (flat or tree layout) and return {id: data}.

    Each value is the raw payload dict ``{"session", "messages", "blobs"?}`` with
    an extra internal ``_source_path`` key (a Path) pointing at the file it was
    read from — used by deletion propagation so a session can be removed even
    when its filename does not equal its id. Invalid files are warned and
    skipped; on duplicate ids (flat + tree copy) the first sorted file wins.
    """
    if not raw_dir.is_dir():
        return {}
    json_files = sorted(raw_dir.glob("*.json")) + sorted(raw_dir.glob("*/*.json"))
    out: dict[str, dict] = {}
    for jf in json_files:
        try:
            data = _load_raw_file(jf)
            _validate_raw_payload(data)
        except ValueError as e:
            console.print(f"[yellow]Warning: skipping {jf.name}: {e}[/yellow]")
            continue
        sid = data["session"]["id"]
        if sid in out:
            continue
        # stash the source path for deletion propagation; never serialized back out
        data["_source_path"] = jf
        out[sid] = data
    return out


# ---------------------------------------------------------------------------
# diff computation (v1 algorithm, unchanged)


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
    manifest_sessions = manifest.get("sessions", {}) if manifest else {}

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


# ---------------------------------------------------------------------------
# tree ordering


def _forest(parent_of: dict[str, str | None], id_set: set[str]) -> list[list[str]]:
    """Group ids into root-first trees: ``[[root, child, ...], ...]`` (BFS).

    An id whose parent is not in ``id_set`` is treated as its own root — it will
    still be synced, and its real parent (if present on the other side) links up
    there. Deterministic (sorted); a parent cycle falls back to singleton order.
    """
    children_map: dict[str, list[str]] = {}
    for sid in id_set:
        pid = parent_of.get(sid)
        if pid is not None and pid in id_set:
            children_map.setdefault(pid, []).append(sid)

    ordered: list[list[str]] = []
    seen: set[str] = set()
    for root in sorted(sid for sid in id_set if parent_of.get(sid) not in id_set):
        if root in seen:
            continue
        tree = [root]
        seen.add(root)
        queue = [root]
        while queue:
            current = queue.pop(0)
            for child in sorted(children_map.get(current, [])):
                if child not in seen:
                    seen.add(child)
                    tree.append(child)
                    queue.append(child)
        ordered.append(tree)
    ordered.extend([sid] for sid in sorted(id_set - seen))
    return ordered


# ---------------------------------------------------------------------------
# apply


def _push_to_folder(
    conn,
    db_rows_by_id: dict[str, dict],
    to_folder_ids: list[str],
    raw_dir: Path,
) -> list[str]:
    """Push DB -> folder for the flagged ids, tree layout per root.

    One :func:`ocsm.common.export_sessions` call per session tree: children land
    in ``<raw_dir>/<root_id>/<child_id>.json`` (v1's layout for direct
    subagents); a child whose parent is not part of this push becomes a flat
    ``<raw_dir>/<sid>.json``.
    """
    parent_of = {sid: row.get("parent_id") for sid, row in db_rows_by_id.items()}
    written: list[str] = []
    for tree in _forest(parent_of, set(to_folder_ids)):
        rows = [db_rows_by_id[sid] for sid in tree if sid in db_rows_by_id]
        if not rows:
            continue
        export_sessions(
            conn, rows, to=raw_dir, to_project=None, fmt="raw", tree=True,
            thinking=True, tool_calls="info",
        )
        written.extend(row["id"] for row in rows)
    return written


def _delete_from_folder(raw_dir: Path, folder_sessions: dict[str, dict], ids: list[str]) -> list[str]:
    """Propagate DB-side deletions to the folder.

    Candidate file paths per sid: the recorded source path (authoritative,
    handles non-standard filenames) plus the conventional ``{sid}.json``
    locations (top level and every immediate subdir). Empty subtree dirs are
    cleaned up afterwards.
    """
    deleted: list[str] = []
    subdirs_to_cleanup: set[Path] = set()
    for sid in sorted(ids):
        candidates: list[Path] = []
        src = folder_sessions.get(sid, {}).get("_source_path")
        if isinstance(src, Path):
            candidates.append(src)
        candidates.append(raw_dir / f"{sid}.json")
        if raw_dir.is_dir():
            for sub in raw_dir.iterdir():
                if sub.is_dir():
                    candidates.append(sub / f"{sid}.json")
        removed = False
        for candidate in candidates:
            try:
                if candidate.is_file():
                    candidate.unlink()
                    removed = True
                    if candidate.parent != raw_dir:
                        subdirs_to_cleanup.add(candidate.parent)
            except OSError:
                pass
        if removed:
            deleted.append(sid)
    for d in subdirs_to_cleanup:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return deleted


def _pull_to_db(
    conn,
    project_dir: Path,
    diff: dict,
    folder_sessions: dict[str, dict],
    resolved,
) -> tuple[list[str], list[str]]:
    """Pull folder -> DB for the flagged ids (overwrite in place on conflict).

    Parents are imported before children (deterministic root-first order). A
    subagent whose parent is neither in this batch nor already in the DB is
    skipped with a warning (v1 behavior). An id already in the DB is replaced
    delete-first via ``delete_session_tree_full(recursive=False)`` — only the
    session's own rows go, its DB-side children survive, exactly like v1's
    ``replace_session``.
    """
    to_db_ids = set(diff["to_db"])
    imported: list[str] = []
    skipped: list[str] = []
    if not to_db_ids:
        return imported, skipped

    parent_of = {
        sid: data["session"].get("parent_id")
        for sid, data in folder_sessions.items()
        if sid in to_db_ids
    }
    order = [sid for tree in _forest(parent_of, to_db_ids) for sid in tree]
    imported_now: set[str] = set()
    for sid in order:
        data = folder_sessions.get(sid)
        if data is None:
            continue
        session = data["session"]
        pid = session.get("parent_id")
        # Skip orphan subagents whose parent is neither in this batch nor already in DB.
        if pid is not None and pid not in imported_now and queries.session_row(conn, pid) is None:
            console.print(
                f"[yellow]Warning: skipping {sid}: parent session {pid} is not in"
                " this batch nor in the DB.[/yellow]"
            )
            skipped.append(sid)
            continue
        payload: dict = {"session": session, "messages": data["messages"]}
        if data.get("blobs") is not None:
            payload["blobs"] = data["blobs"]
        if queries.session_row(conn, sid) is not None:
            queries.delete_session_tree_full(conn, sid, recursive=False)
        queries.insert_session_tree(conn, payload, resolved=resolved, target_directory=project_dir)
        imported_now.add(sid)
        imported.append(sid)
    return imported, skipped


def _delete_from_db(conn, delete_from_db_ids: list[str]) -> list[str]:
    """Propagate folder-side deletions to the DB (whole subtrees).

    Descendants of every deleted id are removed too (recursive
    ``delete_session_tree_full``); a top-level id already covered by an earlier
    subtree deletion is skipped.
    """
    deleted: list[str] = []
    remaining = set(delete_from_db_ids)
    for sid in sorted(delete_from_db_ids):
        if sid not in remaining:
            continue  # already removed as a descendant of an earlier delete
        ids = queries.descendant_ids(conn, sid)
        queries.delete_session_tree_full(conn, sid)  # recursive: removes the subtree
        deleted.extend(ids)
        remaining.difference_update(ids)
    return deleted


def _apply_sync(
    db_path: Path,
    project_dir: Path,
    diff: dict,
    folder_sessions: dict[str, dict],
    db_rows_by_id: dict[str, dict],
    *,
    do_delete: bool,
) -> dict:
    """Execute the sync plan against DB and filesystem.

    - Folder writes happen before the DB writes (the DB is the side that can be
      regenerated); the DB is checkpointed + backed up before its first write.
    - Each queries-layer write commits on its own (per-operation transactions).
    - Returns a summary dict for reporting.
    """
    raw_dir = _raw_dir(project_dir)

    folder_written: list[str] = []
    folder_deleted: list[str] = []
    db_imported: list[str] = []
    db_skipped: list[str] = []
    db_deleted: list[str] = []
    backup_path: Path | None = None

    conn = get_connection(db_path)
    try:
        # --- Folder-side mutations (writes + deletes) ---
        if diff["to_folder"]:
            folder_written = _push_to_folder(conn, db_rows_by_id, diff["to_folder"], raw_dir)
        if do_delete and diff["delete_from_folder"]:
            folder_deleted = _delete_from_folder(raw_dir, folder_sessions, diff["delete_from_folder"])

        # --- DB-side mutations ---
        if diff["to_db"] or (do_delete and diff["delete_from_db"]):
            backup_path = queries.checkpoint_and_backup(db_path)
            resolved = queries.resolve_and_register(conn, str(project_dir))
            db_imported, db_skipped = _pull_to_db(conn, project_dir, diff, folder_sessions, resolved)
            if do_delete and diff["delete_from_db"]:
                db_deleted = _delete_from_db(conn, diff["delete_from_db"])
    finally:
        conn.close()

    return {
        "folder_written": folder_written,
        "folder_deleted": folder_deleted,
        "db_imported": db_imported,
        "db_skipped": db_skipped,
        "db_deleted": db_deleted,
        "backup_path": backup_path,
    }


# ---------------------------------------------------------------------------
# reporting


def _folder_title(folder_sessions: dict[str, dict], sid: str) -> str | None:
    s = folder_sessions.get(sid, {}).get("session")
    return s.get("title") if s else None


def _folder_tu(folder_sessions: dict[str, dict], sid: str) -> int | None:
    s = folder_sessions.get(sid, {}).get("session")
    if not s:
        return None
    v = s.get("time_updated")
    return v if isinstance(v, int) else None


def _print_sync_plan(diff: dict, folder_sessions: dict[str, dict], db_sessions: dict[str, int]) -> None:
    """Pretty-print the computed sync plan (used for --dry-run and pre-apply summary)."""
    def _fmt_ts(ms: int | None) -> str:
        return format_timestamp(ms) if ms else "(unknown)"

    if diff["to_folder"]:
        console.print(f"[green]DB -> folder ({len(diff['to_folder'])}):[/green]")
        for sid in diff["to_folder"]:
            console.print(f"  + {sid}  [dim]db updated {_fmt_ts(db_sessions.get(sid))}[/dim]")
    if diff["to_db"]:
        console.print(f"[cyan]folder -> DB ({len(diff['to_db'])}):[/cyan]")
        for sid in diff["to_db"]:
            console.print(f"  + {sid}  [dim]{_folder_title(folder_sessions, sid) or '(untitled)'}[/dim]")
    if diff["conflicts"]:
        console.print(f"[yellow]Conflicts ({len(diff['conflicts'])}):[/yellow]")
        for sid in diff["conflicts"]:
            console.print(
                f"  ! {sid}  [dim]db {_fmt_ts(db_sessions.get(sid))} vs folder"
                f" {_fmt_ts(_folder_tu(folder_sessions, sid))}[/dim]"
            )
    if diff["same"]:
        console.print(f"[dim]In sync ({len(diff['same'])}): no change[/dim]")
    if diff["delete_from_folder"]:
        console.print(f"[red]Delete from folder ({len(diff['delete_from_folder'])}):[/red]")
        for sid in diff["delete_from_folder"]:
            console.print(f"  x {sid}  [dim]{_folder_title(folder_sessions, sid) or '(untitled)'}[/dim]")
    if diff["delete_from_db"]:
        console.print(f"[red]Delete from DB ({len(diff['delete_from_db'])}):[/red]")
        for sid in diff["delete_from_db"]:
            console.print(f"  x {sid}  [dim]db updated {_fmt_ts(db_sessions.get(sid))}[/dim]")


def _refresh_manifest(project_dir: Path, db_side: dict[str, int], folder_side: dict[str, int]) -> None:
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
        console.print(f"[dim]  project_id set to '{result.get('project_id', '?')}'[/dim]")
        for sid in di:
            console.print(f"  + {sid}")
    if result.get("db_skipped"):
        console.print(f"[yellow]Skipped {len(result['db_skipped'])} folder session(s) (parent missing)[/yellow]")
        for sid in result["db_skipped"]:
            console.print(f"  - {sid}")
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
    if result["backup_path"]:
        console.print(f"\n[dim]Backup: {result['backup_path']}[/dim]")
        console.print("[dim]Delete the backup manually once you confirm everything works.[/dim]")


# ---------------------------------------------------------------------------
# command


def _locate_project(ctx: typer.Context, project: str) -> tuple[Path, str]:
    """Resolve ``--project`` (a directory, or a project id) to (dir, project_id).

    A directory is resolved purely via :func:`ocsm.projectid.resolve_project`
    (no registry writes). A project id is located through the ``project`` table
    and its registered worktree is used as the project directory.
    """
    as_path = Path(project).expanduser()
    if as_path.is_dir():
        project_dir = as_path.resolve()
        return project_dir, resolve_project(project_dir).id
    conn = open_db(ctx)
    try:
        for row in queries.list_projects(conn):
            if row["id"] == project:
                worktree = Path(row["worktree"])
                if not worktree.is_dir():
                    break
                return worktree.resolve(), row["id"]
    finally:
        conn.close()
    console.print(f"[red]Error:[/red] Project not found (as a directory or a project id): {project}")
    raise typer.Exit(1)


@app.command("project")
def sync_project_cmd(
    ctx: typer.Context,
    project: str = typer.Option(
        ...,
        "--project",
        help="Project directory (or project id) to sync (DB <-> <project>/.opencode/raw_conversations/)",
    ),
    on_conflict: str = typer.Option("ask", "--on-conflict", help="Conflict resolution: ask | newer | skip"),
    delete: bool = typer.Option(True, "--delete/--no-delete", help="Propagate deletions (requires a prior manifest)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive: skip all confirmations (deletions still need --delete)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and exit without writing anything"),
):
    """Two-way sync a project's sessions between the DB and the project folder.

    Reads the DB state for the project, the <project>/.opencode/raw_conversations/
    folder, and the <project>/.opencode/.ocsm-sync.json manifest, then reconciles
    new/updated/deleted sessions on both sides. Conflicts (same id, different
    time_updated) are resolved via --on-conflict (default: interactive prompt).
    """
    if on_conflict not in ("ask", "newer", "skip"):
        console.print("[red]Error:[/red] --on-conflict must be one of: ask, newer, skip")
        raise typer.Exit(2)

    project_dir, project_id = _locate_project(ctx, project)
    db_path = ctx.obj["db_path"]
    raw_dir = _raw_dir(project_dir)

    # --- Gather state on all three sides ---
    conn = open_db(ctx)
    try:
        db_rows = queries.list_sessions(conn, project_id=project_id)
    finally:
        conn.close()
    db_rows_by_id = {row["id"]: row for row in db_rows}
    db_sessions = {row["id"]: row["time_updated"] for row in db_rows}

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
    console.print(f"[dim]Project ID: {project_id}[/dim]")
    console.print(
        f"[dim]DB: {len(db_sessions)} session(s)  |  folder: {len(folder_side)} session(s)"
        f"  |  manifest: {'yes' if manifest else 'no (first sync)'}[/dim]\n"
    )
    _print_sync_plan(diff, folder_sessions, db_sessions)

    # --- Resolve conflicts ---
    unresolved = list(diff["conflicts"])
    if unresolved:
        if on_conflict == "ask" and not is_interactive():
            console.print(
                f"\n[red]Error:[/red] {len(unresolved)} conflict(s) require --on-conflict"
                " (ask needs a TTY). Re-run with --on-conflict newer|skip."
            )
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
        elif is_interactive():
            items_folder = [(sid, _folder_title(folder_sessions, sid)) for sid in diff["delete_from_folder"]]
            items_db = [
                (sid, (db_rows_by_id.get(sid) or {}).get("title"))
                for sid in diff["delete_from_db"]
            ]
            ok = _ask_delete_confirmation("folder", items_folder) and _ask_delete_confirmation("DB", items_db)
            if not ok:
                console.print("[yellow]Deletions declined by user; continuing without deletion propagation.[/yellow]")
                diff["delete_from_folder"] = []
                diff["delete_from_db"] = []
        else:
            console.print(
                "[yellow]Warning: deletions detected in non-interactive mode without -y;"
                " skipping deletion propagation.[/yellow]"
            )
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
            _refresh_manifest(project_dir, db_sessions, folder_side)
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
            db_rows_by_id,
            do_delete=delete,
        )
    except Exception as e:
        console.print(f"[red]Error during sync: {e}[/red]")
        console.print(
            "[dim]Folder-side writes (if any) are NOT rolled back — re-run sync to"
            " reconcile. If DB writes had started, a .bak backup sits next to the"
            " database; restore it if needed.[/dim]"
        )
        raise typer.Exit(1)
    result["project_id"] = project_id

    # --- Report ---
    _sync_report(result)

    # --- Re-gather post-sync state and refresh manifest ---
    conn = open_db(ctx)
    try:
        post_rows = queries.list_sessions(conn, project_id=project_id)
    finally:
        conn.close()
    post_db = {row["id"]: row["time_updated"] for row in post_rows}
    # Re-scan folder to capture newly written files + reflect deletions
    post_folder_sessions = _load_folder_sessions(raw_dir)
    post_folder = {
        sid: data["session"]["time_updated"]
        for sid, data in post_folder_sessions.items()
        if isinstance(data.get("session", {}).get("time_updated"), int)
    }
    _refresh_manifest(project_dir, post_db, post_folder)

    # --- Verify (only if DB changed) ---
    if result["db_imported"] or result["db_deleted"]:
        conn = open_db(ctx)
        try:
            problems = queries.verify_database(conn, result["db_imported"])
        finally:
            conn.close()
        if problems:
            console.print("\n[red]Database verification found problems:[/red]")
            for problem in problems:
                console.print(f"  [red]{problem}[/red]")
        else:
            console.print("\n[green]Database verification passed.[/green]")
