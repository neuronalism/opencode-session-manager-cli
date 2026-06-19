# OpenCode Session Manager CLI

**tl;dr:** 

A simple CLI tool for managing [OpenCode](https://github.com/opencode-ai/opencode) projects and sessions stored in SQLite. It can:
- handle renamed/removed project folders,
- batch export all sessions (with sub-sessions) of a project as markdown or raw JSON,
- import raw JSON sessions (with sub-sessions) into the database, and 
- sync project-stored JSON sessions with the local database, allowing cross-device project synchronization.

Tested on OpenCode v1.17.8 on Windows and Mac.

> [!CAUTION]
> 
> This tool may manipulate your local OpenCode database. Use with caution, and make sure to backup your database before using this tool.

## Why to use this tool / Features

- Suppose you are working on a long conversation and want to *continue it on another machine*, but currently OpenCode does not support syncing conversations across multiple machines. The other machine will not have these conversations you had elsewhere. 
   - You can use this tool to export the raw conversation to the project folder, sync it, and then re-import it on the other machine. Your conversations (and the whole project!) will be fully preserved and you can continue working smoothly.
   - If you are working on both Windows and Mac machines, re-importing the conversations handles path issues smoothly, as all files are treated relative to the project root.
- Suppose you moved your project folder to another location (or just renamed it for any reason), but OpenCode still has the old path in the database and won't recognize your new location. 
   - You can use this tool to update the path in the OpenCode database and have all your conversations back.
   - You can even *merge* multiple projects into one by "moving" the old project to the new path. Then you will see all your conversations in the merged project.
   - The old project paths don't even have to exist when moving, since this is only manipulating the OpenCode database; you need to manually move any other files in the project.

## Comparison to OpenCode's built-in export/import

OpenCode now ships its own `opencode export [sessionID]` and `opencode import <file>` commands. This tool exists to fill the gaps they leave.

**What OpenCode's built-in commands do:**
- `opencode export <sessionID>` exports a **single session** as JSON (its `info` + `messages`).
- `opencode import <file>` imports that single session back into the database.

**What this tool adds on top:**

- **Full conversation trees.** OpenCode's `export` only emits the one session you name (as of version 1.17.8). Its subagent sessions are **not** exported — their IDs only appear as strings inside a parent's tool-call metadata. Re-importing such an export therefore **loses every subagent session permanently**. This tool exports and imports the **entire tree** (root + all descendants), so a multi-agent conversation round-trips intact.
- **Batch operations.** `export project` / `import project` work on every session of a project at once, instead of one session at a time.
- **Two-way sync.** `sync project` reconciles the database and the project folder in both directions (new / updated / deleted), with conflict resolution and deletion propagation — none of which the built-in commands offer.
- **Cross-machine path handling.** Imported sessions get their absolute paths rewritten to the local project directory (handling Windows ↔ Mac differences automatically); the built-in import keeps the exporting machine's paths.
- **No runtime dependency.** This tool talks to the SQLite database directly (`--db <path>`), so it works even when the `opencode` CLI itself can't run, and can target any database file.
- **Safe writes.** Every database mutation goes through a WAL checkpoint + timestamped backup + single transaction with rollback.

## Acknowledgments

- Motivated by Issues [#11231](https://github.com/anomalyco/opencode/issues/11231), [#14292](https://github.com/anomalyco/opencode/issues/14292), [#19017](https://github.com/anomalyco/opencode/issues/19017) of OpenCode, and inspired by [BrianLan's export-opencode-sessions Skills](https://github.com/brianlan/improved-ai-agent/tree/master/skills/export-opencode-sessions). 


## Using this tool

### Installation 

using `uv python`: 

```bash
uv tool install .
```

### Commands

#### Listing

**List** local projects/sessions

```bash
ocsm list project                        # list all projects
ocsm list session                        # list root sessions
ocsm list session --project <path>       # filter by project directory
ocsm list session --flat                 # include subagent sessions (flat)
ocsm list session --tree                 # include subagent sessions (tree)
```


#### Exporting

**Export** one session:

```bash
ocsm export session --from <ses_id>                              # markdown, default options
ocsm export session --from <ses_id> --format raw                 # raw JSON (import-safe)
ocsm export session --from <ses_id> --tree                       # with subagents (tree layout)
ocsm export session --from <ses_id> --flat                       # with subagents (flat layout)
ocsm export session --from <ses_id> --to /path/to/dir            # output to specific directory
ocsm export session --from <ses_id> --to-project /path/to/proj   # output to <proj>/.opencode/conversations/
```

**Export** all sessions in a project:

```bash
ocsm export project --from /path/to/proj                            # export all top-level sessions of a project
ocsm export project --from /path/to/proj --format raw               # raw JSON
ocsm export project --from /path/to/proj --tree                     # with subagents
ocsm export project --from /path/to/proj --flat                     # with subagents (flat)
```

Preview what would be exported without writing anything:

```bash
ocsm export session --from <ses_id> --dry-run                       # show target paths, write nothing
ocsm export project --from /path/to/proj --dry-run                  # same, for every session of the project
```

Options for exporting:

```bash
--format markdown                    # default, export as markdown. 
--format raw                         # raw JSON (import-safe)
--thinking True                      # include reasoning parts (default True)
--thinking False                     # hide reasoning parts (default)
--tool-call none                     # hide tool calls
--tool-call info                     # show tool name, title, linked session (default)
--tool-call details                  # show tool name, title, session, input, output, error
--tree                               # subagent sessions in nested subdirectories
--flat                               # subagent sessions in same directory
--dry-run                            # show target paths, write nothing
```

Default export path:

- `<project>/.opencode/conversations/` for markdown, and
- `<project>/.opencode/raw_conversations/` for raw JSON (supported by import).
- If the default export path no longer exists, falls back to `cwd`.

#### Importing

**Import** a raw JSON session:

```
ocsm import session --from /path/to/session.json --to-project /path/to/proj        # import one session tree
ocsm import session --from /path/to/session.json --to-project /path/to/proj --no-substitute-paths  # import without replacing paths in the conversations
ocsm import project --from /path/to/proj --to-project /path/to/proj                # import all sessions from a project's raw export
ocsm import project --from /path/to/proj --to-project /path/to/proj --no-substitute-paths
```

Preview what would be imported without writing anything (no DB writes, no backup):

```bash
ocsm import session --from /path/to/session.json --to-project /path/to/proj --dry-run
ocsm import project --from /path/to/proj --to-project /path/to/proj --dry-run
```

Import only accepts raw JSON files (exported with `--format raw`). If the target directory already has sessions, conflicting session IDs are skipped.

> [!NOTE]
> 
> This tool uses a verbose but safe database manipulation pipeline which creates a backup *every time* you imported something:
> 
> 1. SQLite WAL checkpoint to flush all cached data
> 2. Full database backup (timestamped `.bak` file)
> 3. Import sessions (skips existing IDs, replaces `session.directory` and paths in the conversation by default)
> 4. Tree integrity validation
> 5. OpenCode runtime verification (`opencode db PRAGMA integrity_check`)
> 6. Report results. 
> 
> The user must manually delete the backup file after checking the results, or your disk may be filled up with so many backups.

#### Syncing

`sync project` reconciles a project's conversations **both ways** between the OpenCode database and the project's `.opencode/raw_conversations/` folder. Once that folder is kept in sync across machines (git, cloud drive, …), running `sync` on each machine keeps the local DB consistent with it in **both** directions — no manual export/import round-trip.

The simplest way to keep a project portable:

```
# on machine A
ocsm sync project --from /path/to/proj      # writes DB sessions into the folder
# git push / cloud-sync the project folder
# on machine B (after git pull)
ocsm sync project --from /path/to/proj      # pulls new folder sessions into the local DB
```

**Common usage:**

```
ocsm sync project --from /path/to/proj                  # default: prompt on conflicts, deletions on
ocsm sync project --from /path/to/proj --dry-run        # preview the plan, write nothing
ocsm sync project --from /path/to/proj --on-conflict newer   # auto-resolve: newer time_updated wins
ocsm sync project --from /path/to/proj --on-conflict skip    # skip conflicting sessions
ocsm sync project --from /path/to/proj --no-delete      # turn off deletion propagation
ocsm sync project --from /path/to/proj -y               # non-interactive: skip confirmations
```

Options for syncing:

```
--from <path>                         # required, the project directory to sync
--on-conflict ask                     # default, prompt per session: keep DB / keep folder / skip
--on-conflict newer                   # auto-resolve: the side with the newer time_updated wins
--on-conflict skip                    # leave conflicting sessions untouched on both sides
--delete                              # default, propagate deletions via the sync manifest
--no-delete                           # turn off deletion propagation
--yes / -y                            # non-interactive: skip all confirmation prompts
--dry-run                             # show the plan and exit without writing anything
--substitute-paths                    # default, rewrite old paths to the local project dir on import
--no-substitute-paths                 # keep the exporting machine's paths as-is
```

**How it works**

- **Three states are compared**: the DB (sessions whose `directory` matches the project), the folder (`<project>/.opencode/raw_conversations/*.json`), and the *manifest* (`<project>/.opencode/.ocsm-sync.json`, written by `sync` after every successful run).
- **Identity & freshness**: sessions are matched by `id`; which copy is newer is decided by `time_updated`, read from the raw JSON's `session` object (not the file mtime — stable across OSes and clocks).
- **Classification**:
  - only on one side → copied to the other;
  - on both sides, same `time_updated` → no-op;
  - on both sides, different `time_updated` → **conflict**;
  - tracked by the manifest but missing from one side → **deletion**.
- **Conflicts** are resolved by `--on-conflict`:
  - `ask` (default): per-session prompt — *keep DB / keep folder / skip* — like a file-copy dialog. Needs a TTY; in a non-interactive shell it aborts with an error rather than silently overwriting (use `newer`/`skip` to run unattended).
  - `newer`: the copy with the larger `time_updated` wins (tie → skip).
  - `skip`: leave both copies untouched.
- **Deletion propagation**: if a session the manifest remembers later vanishes from one side, `sync` removes it from the other. **The first sync (no manifest) never deletes** — every session is treated as new; deleting the manifest resets to that safe behavior. Deletions are whole-tree (removing a root session removes its subagents) and are confirmed interactively (default `N`) unless `-y` is given.
- **Safety**: DB writes reuse the same pipeline as `import` — WAL checkpoint + timestamped `.bak` backup + single transaction with rollback. Updating an existing session is done as DELETE + INSERT (its messages/parts may have changed). Imported `project_id` is reset to `global` so OpenCode reassigns it on startup.
- **No content merge**: a session is always taken wholesale from one side — `sync` never merges messages field-by-field.

> [!NOTE]
> Subagent sessions (children of a root session) follow their root's direction, so conversation trees stay intact across syncs.

#### Moving/Renaming a project folder

**Move** a project (after renaming/moving the project folder):

```bash
ocsm move project --from /old/path --to-project /new/path     # update all session paths in the database
ocsm move project --from-id <id> --to-id <id>                 # move by project ID
```

> [!NOTE]
>
> - The new path can either be a project existing or not in the database. The "move" command only manipulates the paths in the database, and does not move the project folder and the files.
> - The safe pipeline is also used for this command.

#### Deleting sessions/projects from the database

**export-then-delete** permanently removes sessions (or a whole project) from the database — but only after writing an import-safe raw-JSON export first. There is no standalone delete command: export and delete are deliberately coupled for safety considerations.

```bash
ocsm export-then-delete session --from <ses_id> --to /backup/dir           # export raw JSON, then delete the session
ocsm export-then-delete session --from <ses_id> --to-project /other/proj   # export into another project's .opencode dir
ocsm export-then-delete project --from /path/to/proj --to /backup/dir      # export all sessions, then delete them + the project row

ocsm export-then-delete session --from <ses_id> --to /backup --format markdown  # raw JSON *and* markdown
ocsm export-then-delete project --from /path/to/proj --to /backup --dry-run      # preview, write/delete nothing
```

Because deletion is irreversible, the command asks you to **re-type the exact `--from` value** (session id or project path) at an interactive prompt before touching the database. There is no `-y` bypass.

```bash
--from <id>|<path>                     # required, the session id or project directory to export-then-delete
--to <dir>                             # output directory (required, mutually exclusive with --to-project)
--to-project <path>                    # write to <path>/.opencode/raw_conversations (mutually exclusive with --to)
--format raw                           # default: raw JSON only
--format markdown                      # raw JSON + markdown (both)
--tree / --flat                        # include subagent sessions
--thinking / --no-thinking             # markdown only: include reasoning parts
--tool-call none|info|details          # markdown only: tool-call detail level
--dry-run                              # show the export targets and the deletion list, do nothing
```

> [!IMPORTANT]
>
> - **Export is mandatory and always raw JSON.** Raw JSON is the only format `import` can fully restore, so it is written on every invocation (additionally markdown when `--format markdown`). The deletion phase is skipped entirely if the export fails.
> - **An explicit destination is required.** Exactly one of `--to` / `--to-project` must be given — the default `.opencode/raw_conversations/` location may disappear when a project is deleted, so the export has to land somewhere predictable.
> - **Trees are atomic.** `session` exports and deletes the whole tree (root + subagents); `project` deletes every matching root plus all its subagents, so no orphaned rows are left behind.
> - **Same safe pipeline as import/sync**: WAL checkpoint + timestamped `.bak` backup + single transaction with rollback. The report prints a concrete `ocsm import session --from <file> --to-project <path>` line so you can restore immediately if needed.

### Use custom database path

- `ocsm --db <path> list session`
- `OCSM_DB_PATH=<path> ocsm list session`
- Default: `~/.local/share/opencode/opencode.db`

## Dev Plan

- [x] list sessions and projects
- [x] export sessions and projects
- [x] import raw jsons into database
- [x] move project paths (after rename/relocate)
- [x] sync conversations in two ways with local project folder
- [x] export-then-delete (export raw JSON then permanently remove sessions/projects)

## License

MIT
