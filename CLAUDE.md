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
- `uv run ocsm import session --from <json> --to-project <path>` — import a session tree from raw JSON (path substitution on by default)
- `uv run ocsm import project --from <dir> --to-project <path>` — import all sessions from a project's raw export
- `uv run ocsm move project --from <old> --to-project <new>` — move sessions
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
