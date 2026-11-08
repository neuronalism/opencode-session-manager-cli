# OpenCode Session Manager CLI

**tl;dr:**

A CLI tool for managing [OpenCode](https://github.com/opencode-ai/opencode) **2.x** projects and sessions stored in SQLite. It can:
- handle renamed/removed project folders,
- batch export all sessions (with sub-sessions) of a project as markdown, JSON or raw JSON,
- import raw JSON sessions (with sub-sessions) into the database,
- export-then-delete sessions/projects safely, and
- sync project-stored JSON sessions with the local database, allowing cross-device project synchronization.

Version 0.6.0 is a rewrite against the OpenCode **v2 storage schema**
(`session_v2` / `session_message` / `worktree` / `event_sequence` / …),
verified end-to-end against OpenCode 2.2.5/2.2.6 on Windows: exported →
deleted → re-imported sessions reappear in `opencode session list` under the
correct project, including sessions moved to brand-new directories.

> [!NOTE]
>
> This version works with OpenCode **2.x (v2 storage)** databases only.
> If you need the old OpenCode 1.x support or want to restore v1-format
> exports, switch to the `v1` branch (`git checkout v1`).

> [!CAUTION]
>
> This tool may manipulate your local OpenCode database. Use with caution, and make sure to backup your database before using this tool.

## Why to use this tool / Features

- Suppose you are working on a long conversation and want to *continue it on another machine*, but OpenCode does not sync conversations across machines.
   - Export the raw conversation into the project folder, sync the folder (git, cloud drive, …), and re-import on the other machine — conversation trees included.
- Suppose you moved a project folder (or renamed it), but OpenCode still lists it under the old path.
   - `move project` re-places every session of the project under the new directory, registering the project exactly the way OpenCode itself would (same project-id algorithm, git remote included).
   - Old paths don't even have to exist on disk; you move the files yourself.
- Suppose you want to prune old conversations without losing them.
   - `export-then-delete` writes an import-safe raw-JSON export first and only deletes after you re-type the exact session/project identifier.

## Comparison to OpenCode's built-in export/import

OpenCode 2.x ships `opencode session export/import` (single session, `info` + `messages` shape). This tool fills the gaps they leave:

- **Full conversation trees.** The built-in export emits only the named session; subagent sessions are not included. ocsm exports and imports the entire tree (root + descendants).
- **Batch operations.** `export project` / `import project` work on every session of a project at once.
- **Two-way sync.** `sync project` reconciles the database and the project folder in both directions (new / updated / deleted) with conflict resolution and deletion propagation.
- **Correct placement on import.** `directory` / `path` / `project_id` are recomputed for the destination directory using OpenCode's own project-resolution algorithm, so imported sessions land in the right project instead of being invisible in the UI.
- **No runtime dependency.** ocsm talks to the SQLite file directly (`--db <path>`), works even when the `opencode` CLI can't run, and never requires a running server.
- **Safe writes.** Every database mutation is preceded by a WAL checkpoint plus a timestamped `.bak` backup, and followed by integrity/foreign-key/orphan verification (`ocsm verify`).

## Acknowledgments

- Motivated by Issues [#11231](https://github.com/anomalyco/opencode/issues/11231), [#14292](https://github.com/anomalyco/opencode/issues/14292), [#19017](https://github.com/anomalyco/opencode/issues/19017) of OpenCode, and inspired by [BrianLan's export-opencode-sessions Skills](https://github.com/brianlan/improved-ai-agent/tree/master/skills/export-opencode-sessions).

## Using this tool

### Installation

using `uv python`:

```bash
uv tool install .
```

### Commands

#### Verifying

```bash
ocsm verify                       # integrity, foreign keys, orphans, event_sequence watermarks
ocsm --db <path> verify           # any database file
```

#### Listing

```bash
ocsm list project                        # list all projects
ocsm list session                        # list root sessions
ocsm list session --project <path-or-id> # filter by project directory or project id
ocsm list session --flat                 # include subagent sessions (flat)
ocsm list session --tree                 # include subagent sessions (tree)
```

#### Exporting

**Export** one session:

```bash
ocsm export session --from <ses_id>                              # markdown, default options
ocsm export session --from <ses_id> --format json                # readable JSON dump
ocsm export session --from <ses_id> --format raw                 # raw JSON (import-safe)
ocsm export session --from <ses_id> --tree                       # with subagents (tree layout)
ocsm export session --from <ses_id> --flat                       # with subagents (flat layout)
ocsm export session --from <ses_id> --to /path/to/dir            # output to specific directory
ocsm export session --from <ses_id> --to-project /path/to/proj   # output to <proj>/.opencode/conversations/
ocsm export session --from <ses_id> --dry-run                    # show target paths, write nothing
```

**Export** all sessions in a project:

```bash
ocsm export project --from /path/to/proj                     # also accepts a project id
ocsm export project --from /path/to/proj --format raw --flat
ocsm export project --from /path/to/proj --dry-run
```

Options: `--format markdown|json|raw`, `--thinking/--no-thinking` (markdown), `--tool-call none|info|details` (markdown), `--tree`/`--flat`, `--dry-run`.

Default export path: `<project>/.opencode/conversations/` for markdown, `<project>/.opencode/raw_conversations/` for raw JSON; falls back to `cwd` if the project directory is gone.

#### Importing

```bash
ocsm import session --file /path/to/session.json --directory /path/to/proj   # import one session tree
ocsm import project --from-dir /path/to/raw_conversations --directory /path/to/proj   # batch import
ocsm import session --file ... --directory ... --dry-run                     # preview only
```

- `--directory` (default: current directory) selects the destination project; `project_id`, `directory` and `path` are recomputed for it, message ids/seqs are preserved.
- Parent→child chains inside a batch are handled automatically (children deferred until their parent lands).
- Conflicting session ids are rejected (single) / skipped with a report (batch).

> [!NOTE]
>
> Import uses a safe pipeline which creates a backup **every time**:
>
> 1. conflict/parent checks
> 2. SQLite WAL checkpoint
> 3. timestamped full-database backup (`opencode.db.bak.<ts>`)
> 4. insert (session_v2 + session_message + event_sequence watermark + optional blobs)
> 5. integrity/foreign-key/orphan verification
> 6. report + backup path
>
> The user must manually delete backups after checking the results.
> Pre-0.6.0 (v1-format) export files are rejected — restore them with the `v1` branch instead.

#### Syncing

`sync project` reconciles a project's conversations **both ways** between the OpenCode database and `<project>/.opencode/raw_conversations/`.

```
# on machine A
ocsm sync project --project /path/to/proj     # writes DB sessions into the folder
# git push / cloud-sync the project folder
# on machine B (after git pull)
ocsm sync project --project /path/to/proj     # pulls new folder sessions into the local DB
```

```bash
ocsm sync project --project /path/to/proj                  # default: prompt on conflicts, deletions on
ocsm sync project --project /path/to/proj --dry-run        # preview the plan, write nothing
ocsm sync project --project /path/to/proj --on-conflict newer   # auto-resolve: newer time_updated wins
ocsm sync project --project /path/to/proj --on-conflict skip    # skip conflicting sessions
ocsm sync project --project /path/to/proj --no-delete      # turn off deletion propagation
ocsm sync project --project /path/to/proj -y               # non-interactive: skip confirmations
```

How it works (unchanged from earlier versions):

- Three states are compared: the DB (sessions of the project), the folder (`raw_conversations/*.json` + `*/*.json`), and the manifest (`.opencode/.ocsm-sync.json`).
- Identity by `id`, freshness by `time_updated` (from the JSON, not file mtime).
- only-one-side → copy; same stamp → no-op; different stamps → conflict (`ask`/`newer`/`skip`).
- Manifest-tracked ids missing from one side are **deletions** (detected before add-detection); first sync never deletes; deletions are whole-tree and confirmed unless `-y`.
- No content merge — a session is always taken wholesale from one side; subagents follow their root.

#### Moving/Renaming a project folder

```bash
ocsm move project --from /old/path --to /new/path    # --from also accepts a project id
ocsm move project --from /old/path --to /new/path --dry-run
```

- `--to` must exist on disk; the project is registered there exactly like OpenCode would (project row + worktree rows).
- Only the session placement columns change — message history keeps its recorded paths, matching OpenCode's own move semantics.

#### Deleting sessions/projects from the database

**export-then-delete** permanently removes sessions (or a whole project) — but only after writing an import-safe raw-JSON export and verifying every file landed.

```bash
ocsm export-then-delete session --from <ses_id> --to /backup/dir           # export raw JSON, then delete the session tree
ocsm export-then-delete project --from /path/to/proj --to /backup/dir      # all sessions of the project
ocsm export-then-delete project --from /path/to/proj --to /backup --vacuum # also VACUUM the DB file
ocsm export-then-delete session --from <ses_id> --to /backup --format markdown  # raw JSON *and* markdown
```

Deletion requires re-typing the exact `--from` value in an interactive prompt (no `-y` bypass). Deletions remove the full dependency closure — messages, parts, inbox rows, blob refs, events, jobs, `event_sequence` watermarks — leaving no orphaned rows, while project/worktree registrations stay (other sessions may still use them).

### Use custom database path

- `ocsm --db <path> list session`
- `OCSM_DB_PATH=<path> ocsm list session`
- Default: `$XDG_DATA_HOME/opencode/opencode.db` (`~/.local/share/opencode/opencode.db`)

## License

MIT
