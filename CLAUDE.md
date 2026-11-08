# OpenCode Sessions Manager (ocsm)

CLI tool for managing OpenCode **2.x** sessions stored in SQLite. Source of
truth: OpenCode **2.2.5** sources at `D:\Code\opencode-src\opencode-2.2.5`
(`packages/core/src`: `session/sql.ts`, `session/projector.ts`,
`session/transfer.ts`, `project.ts`, `bus.ts`, `database/path.ts`).

**Scope rule (v2)**: ocsm reads and writes ONLY the v2 tables. The legacy
`session` / `message` / `part` / `todo` / `session_share` tables and the
deprecated `project_directory` table are never touched — OpenCode's runtime
ignores them once its one-shot `v1-migration` completes, and the v1→v2
migration itself is OpenCode's job, not ocsm's. v1-format export files
(pre-0.6.0) are rejected with a pointer to the `v1` branch.

## Commands

- `uv run ocsm list project` — list projects (id, vcs, worktree, counts)
- `uv run ocsm list session [--project <path-or-id>] [--flat|--tree]` — root sessions by default; subagents need `--flat`/`--tree`
- `uv run ocsm verify` — SQL-level health check (integrity, FKs, orphans, `event_sequence` watermarks); replaces the removed `opencode db`
- `uv run ocsm export session --from <id> [--format markdown|json|raw] [--tree|--flat] [--to <dir>|--to-project <path>] [--dry-run]`
- `uv run ocsm export project --from <path-or-id> [...]` — all sessions of a project
- `uv run ocsm import session --file <json> --directory <dir> [--dry-run]` — import one raw payload; `directory` (default cwd) is the destination project
- `uv run ocsm import project --from-dir <dir> --directory <dir> [--dry-run]` — batch import (parent chains resolved by deferring children)
- `uv run ocsm move project --from <old-path-or-id> --to <new-dir> [--dry-run]` — re-place a project's sessions
- `uv run ocsm sync project --project <path-or-id> [--on-conflict ask|newer|skip] [--no-delete] [-y] [--dry-run]`
- `uv run ocsm export-then-delete session --from <id> --to <dir>|--to-project <path> [--dry-run]`
- `uv run ocsm export-then-delete project --from <path-or-id> --to <dir> [--vacuum] [--dry-run]`
- `uv run ocsm --db <path>` / `OCSM_DB_PATH` — custom database path
- Python via `uv run python` / `uv run ocsm` only (PATH `python` may be the Store placeholder)

## Project Structure

```
src/ocsm/
  cli.py         # assembly: global --db, verify command, mounts the six subapps
  db.py          # path resolution (XDG_DATA_HOME aware) + FK/busy_timeout connection
  projectid.py   # Project.resolve replication (project identity algorithm)
  queries.py     # v2 storage layer: ALL SQL lives here
  format.py      # rich output + markdown/json/raw serialization
  common.py      # console, confirmation gates, shared export walker
  cmd_list.py / cmd_export.py / cmd_import.py / cmd_move.py / cmd_sync.py / cmd_etd.py
```

## Database (v2 map)

`{XDG_DATA_HOME}/opencode/opencode.db`:

- `session_v2` — 37-column session rows (real column order from `PRAGMA table_info`; `fork_session_id`/`time_suspended`/`idle_outcome`/`resume_attempts`/… are v2-only). Order key: `time_updated DESC, id DESC`.
- `session_message` — `id` (global PK), `session_id` (FK→session_v2 CASCADE), `type` (`user`/`assistant`/`system`/…), `seq` (**unique per session**), `data` = message object minus `id`/`type`; assistant parts inline in `data.content`.
- `session_part` — forward-compat part rows, only written by newer OpenCode releases. Reads prefer them over inline content (`history.ts` `withParts`): `queries.session_messages` folds part rows back into `data.content` on export, so payloads carry what OpenCode would read.
- `event_sequence` — durable-event watermark per session (`aggregate_id` PK). **Invariant: every session_v2 row needs one, with `seq` ≥ max message seq** — otherwise OpenCode's next append collides on the `(session_id, seq)` unique index. Every ocsm insert maintains it (`insert_session_tree`).
- `project` / `worktree` — registry. `project.worktree` stores the canonical main checkout; `worktree` lists every checkout directory of a project. `project_directory` is deprecated — do not use.
- `blob` / `blob_ref` — deduplicated media (`media:<sha256>` refs in message data; `experimental.media_blob`, off by default). Exported as payload `blobs` (base64) when referenced; restored on import; released (+ orphan blobs dropped) on delete.
- `event` / `job` — deleted by `aggregate_id`/`session_id` when removing sessions.

Deletion (`delete_session_tree_full`): job rows → blob_ref + orphan blobs →
event/event_sequence → the `session_v2` row itself; children
(session_message/part/inbox/blob_ref/instruction_*) go via ON DELETE CASCADE
(write primitives set `PRAGMA foreign_keys=ON` themselves — cascade never
depends on the caller). Legacy v1 tables are left in place, exactly like
OpenCode's own delete.

## Core Conventions

- **Project identity** (`projectid.resolve_project`) replicates `Project.resolve`: realpath → nearest `.git`/`.hg` marker → git: `origin` remote URL (`sha1("git-remote:host/path")`, host lowercased, `.git`/trailing `/` stripped) > cached `<git-common-dir>/opencode` file > first root commit > `"global"`; hg: cached file > first root node; plain dir: `sha1("directory:" + realpath)`. **Windows gotcha (validated against live rows): the `directory:` hash input uses the backslash native realpath** (`C:\Users\WH` → `afaaaff9…`), while DB columns always store forward slashes (`to_storage_path`). Never "fix" either side.
- **Placement**: import/move recompute `project_id`/`directory`/`path` for the destination (`session/transfer.ts` semantics). `path` = location relative to the resolved worktree root, `"."` at the root. Message content is NOT rewritten (matches OpenCode's own move).
- **Project registration**: writes that place sessions also `upsert_project` (insert identity row, on conflict only reconcile `vcs`) + `register_worktree_rows` (canonical + linked worktree with strategy `git`). Reads/filters only ever `resolve_project` (pure, no writes).
- **`--from`/`--project` resolution**: exact `project.id` match first, otherwise treated as a directory and resolved purely.
- **Todos**: v2 has NO todo table. The snapshot lives in the last `todowrite` tool part (`state.metadata.todos`); markdown exports derive it via `queries.latest_todos`.
- **Export payload**: `{"session": <full session_v2 row>, "messages": [<session_message rows, data parsed, parts merged>], "blobs": [...]?}` — round-trips through `import` with ids/seqs preserved (`time_updated` keeps the original value; OpenCode's own import stamps import time — deliberate divergence so sync ordering stays stable).
- **Windows console**: UTF-8 wrapper + `legacy_windows=False`; tests that rebind `ocsm.common.console` must keep the original referenced (`_console_reference` exists for that).

## Import Safety Pipeline

1. Conflict/parent checks (before anything is written)
2. SQLite WAL checkpoint (`wal_checkpoint(TRUNCATE)`) + timestamped `.bak` backup
3. `insert_session_tree` (register project; insert session_v2 + session_message + blobs + event_sequence watermark)
4. `verify_database` (integrity_check, foreign_key_check, orphans, watermarks)
5. Report + backup path (user deletes backups manually)

`opencode db` no longer exists in 2.x — runtime verification is `ocsm verify`
(SQL level). The full end-to-end check (optional, manual): copy the DB into a
fake `XDG_DATA_HOME` tree and run `opencode --standalone session list
--format json` from a project directory.

## Sync

`sync project --project <path-or-id>` reconciles DB ↔
`<project>/.opencode/raw_conversations/` both ways (v1 algorithm preserved:
manifest `.ocsm-sync.json`, deletion detection precedence, `--on-conflict`,
tree-follows-root, confirmed deletions). DB side filters by `project_id`.
Writes go through the import pipeline; note each storage-layer call commits
its own transaction (no cross-operation rollback — the timestamped backup is
the recovery path).

## Export-then-delete

Exports first (always raw JSON; `--format markdown` adds markdown), verifies
every file landed, then — gated on re-typing the exact `--from` value in a
TTY (no `-y` bypass) — deletes via `delete_session_tree_full`. Project/worktree
registration rows are never deleted. `project --vacuum` rebuilds the file
afterwards (non-fatal on failure).

## Testing

- Synthetic DB: `uv run python -I .zcode/make_test_db.py <out.db> [--scratch-dir <dir>]` (untracked helper; real column lists, two live-resolved projects, parent/child sessions, todowrite snapshot).
- Real-scale E2E (temp copies ONLY — the real DB stays read-only, `mode=ro` + `VACUUM INTO`): `.zcode/integration_e2e.py` covers verify/list/export/etd/import/move/sync-dry-run, then `opencode --standalone session list` from project dirs proves visibility and project-id agreement with the real binary.
