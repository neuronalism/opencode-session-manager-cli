# OpenCode Sessions Manager (ocsm)

CLI tool for managing OpenCode sessions stored in SQLite.

## Commands

- `uv run ocsm list project` — list all projects (grouped by directory)
- `uv run ocsm list session` — list root sessions
- `uv run ocsm list session --project <path>` — filter by project directory
- `uv run ocsm list session --flat` — include subagent sessions (flat)
- `uv run ocsm list session --tree` — include subagent sessions (tree)
- `uv run ocsm export session --from <id>` — export as markdown
- `uv run ocsm export session --from <id> --format raw` — export as raw JSON (import-safe)
- `uv run ocsm export session --from <id> --tree` / `--flat` — export with subagents
- `uv run ocsm export project --from <path>` — export all sessions of a project
- `uv run ocsm export session --from <id> --dry-run` / `export project --from <path> --dry-run` — preview target paths, write nothing
- `uv run ocsm import session --from <json> --to-project <path>` — import a session tree from raw JSON (path substitution on by default)
- `uv run ocsm import project --from <dir> --to-project <path>` — import all sessions from a project's raw export
- `uv run ocsm import session --from <json> --to-project <path> --dry-run` / `import project ... --dry-run` — preview what would be imported (no DB writes, no backup)
- `uv run ocsm move project --from <old> --to-project <new>` — move sessions
- `uv run ocsm sync project --from <path>` — two-way sync (DB ↔ `<project>/.opencode/raw_conversations/`), incl. deletions
- `uv run ocsm export-then-delete session --from <id> --to <dir> | --to-project <path>` — export a session (raw JSON, import-safe) then permanently delete it from the DB
- `uv run ocsm export-then-delete project --from <path> --to <dir> | --to-project <path>` — export all sessions of a project then permanently delete them (and the project row) from the DB
- `uv run ocsm --db <path>` / `OCSM_DB_PATH` — custom database path

## Project Structure

```
src/ocsm/
  cli.py       # typer commands, all CLI entry points
  db.py        # database path resolution (XDG_DATA_HOME aware)
  queries.py   # SQL queries, data loading (load_messages for markdown, load_raw_messages for raw)
  format.py    # rich text output (list/tree) and export serialization (markdown/raw JSON)
```

## Database

SQLite at `{XDG_DATA_HOME}/opencode/opencode.db`. Tables: `session`, `message`, `part`, `project`.

Key relationships:
- `session.parent_id` → parent session (null = root, non-null = subagent)
- `message.session_id` → session
- `part.message_id` → message, `part.session_id` → session

## Conventions

- `load_messages()` — parses JSON `data` fields, injects `id`/`sessionID`/`time`. Used for markdown export.
- `load_raw_messages()` — preserves original `data` JSON strings. Used for raw export (import-safe).
- `session_info()` — transforms row to camelCase dict (readable). Only used for legacy raw format reference.
- Raw export uses `dict(row)` directly — no transformation, no null dropping.
- Markdown default dir: `<project>/.opencode/conversations/`
- Raw default dir: `<project>/.opencode/raw_conversations/`
- Project dir missing → fallback to `cwd`
- Project references use `--project <path>` (directory path) — always unique
- `project_id` is derived from git root commit hash (not directory basename); OpenCode fixes `'global'` automatically on startup
- Windows: Console initialized with `legacy_windows=False` + UTF-8 wrapper to handle CJK/emoji

## Import Safety Pipeline

1. SQLite WAL checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`)
2. Full database backup (`opencode.db.bak.<timestamp>`)
3. Import sessions (skip existing IDs, replace `session.directory`, insert session → message → part)
4. Tree integrity validation (all parent-child references verified)
5. OpenCode runtime verification (`opencode db PRAGMA integrity_check`)
6. Report results + backup location (user deletes backup manually)

## Sync

`sync project` reconciles DB ↔ `<project>/.opencode/raw_conversations/` both ways.

- **Identity & freshness**: keyed by `session.id`; freshness compared via `session.time_updated` (ms epoch) read from the raw JSON's `session` object (NOT file mtime).
- **Diff sides**: DB (`get_session_timestamps`), folder (`_load_folder_sessions` parses `*.json` + `*/*.json`), and the manifest (`<project>/.opencode/.ocsm-sync.json`).
- **Classification** (`_compute_sync_diff`):
  - only-in-DB → push to folder; only-in-folder → pull to DB
  - both, same `time_updated` → no-op; both, different `time_updated` → conflict
  - **Deletion detection takes precedence over add detection**: a manifest-tracked id missing from one side is classified as a deletion (not a fresh add on the other side), preventing DB-deleted sessions from being re-imported from the folder.
- **Conflicts**: `--on-conflict ask` (interactive, requires TTY; else abort) | `newer` | `skip`.
- **Deletion propagation**: only when manifest exists; **first sync never deletes**. Whole-tree (root removal cascades to subagents). Confirmed interactively (default N) unless `-y`; skipped in non-interactive mode without `-y`.
- **Manifest**: atomic write (tempfile + `os.replace`); records ids present on BOTH sides after sync so a later one-sided removal is detectable. Deleting it resets to first-sync.
- **Writes**: DB writes go through the same safety pipeline as import (checkpoint + backup + single transaction + rollback). "Update in place" = DELETE + INSERT via `replace_session` (message/part rows may have changed).
- **Subagents**: a session's direction follows its root, preserving tree integrity.
- **Deletion cleanup**: deletion propagation to the DB uses `delete_session_tree_full` (NOT `delete_session_tree`), so `part`, `message`, `todo`, `session_message`, `session_input`, `session_share`, `session_context_epoch` are all removed — no orphaned rows. `replace_session` still uses `delete_session_tree` (which preserves those rows) because the same id is re-inserted.

## Export-then-delete

`export-then-delete session|project` exports first (always raw JSON, import-safe) and only then permanently deletes from the DB. The two phases are coupled on purpose: there is no standalone delete command.

- **Mandatory explicit destination**: exactly one of `--to <dir>` / `--to-project <path>` is required (mutually exclusive). The default per-session `.opencode/raw_conversations/` location is unavailable because it may vanish when the project is deleted.
- **Format**: `--format raw` (default) writes raw JSON only; `--format markdown` writes **both** raw JSON and markdown. Raw JSON is always written because it is the only re-importable format.
- **Confirmation gate**: deletion requires re-typing the exact `--from` value (session ID or project path) at an interactive prompt — there is **no `-y` bypass**. Non-TTY invocations abort with an error before any DB change. EOF / Ctrl-C / any mismatch aborts (exported files are kept so the user can retry).
- **Tree integrity**: `session` exports+deletes the whole tree (root + subagents via `get_session_tree`); `project` BFS-expands every matching root so subagents are included on both sides.
- **Deletion**: `delete_session_tree_full` (full cascade) +, for `project`, a table-exists-guarded `DELETE FROM project WHERE worktree = ?` (the `project` row is metadata only; OpenCode recreates it). Single transaction with rollback; backup via `_checkpoint_and_backup` first.
- **Recovery**: the report prints a concrete `ocsm import session --from <exported.json> --to-project <path>` hint plus the backup path.
