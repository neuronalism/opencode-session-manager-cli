# OpenCode Session Manager CLI

**tl;dr:** A simple CLI tool for managing [OpenCode](https://github.com/opencode-ai/opencode) sessions stored in SQLite. It handles renamed/removed project folders, allows exporting all conversations of a project in markdown format, and provides a way to even import raw jsons into database.

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

## How this compares to OpenCode's built-in export/import

OpenCode now ships its own `opencode export [sessionID]` and `opencode import <file>` commands. This tool exists to fill the gaps they leave.

**What OpenCode's built-in commands do:**
- `opencode export <sessionID>` exports a **single session** as JSON to stdout (its `info` + `messages`).
- `opencode import <file>` imports that single session back into the database.

**What this tool adds on top:**

- **Full conversation trees.** OpenCode's `export` only emits the one session you name. Its subagent sessions (the `@explore`/`task` children a conversation spawns) are **not** exported — their IDs only appear as strings inside a parent's tool-call metadata. Re-importing such an export therefore **loses every subagent session permanently**. This tool exports and imports the **entire tree** (root + all descendants), so a multi-agent conversation round-trips intact.
- **Batch operations.** `export project` / `import project` work on every session of a project at once, instead of one session at a time.
- **Two-way sync.** `sync project` reconciles the database and the project folder in both directions (new / updated / deleted), with conflict resolution and deletion propagation — none of which the built-in commands offer.
- **Cross-machine path handling.** Imported sessions get their absolute paths rewritten to the local project directory (handling Windows ↔ Mac differences automatically); the built-in import keeps the exporting machine's paths.
- **No runtime dependency.** This tool talks to the SQLite database directly (`--db <path>`), so it works even when the `opencode` CLI itself can't run, and can target any database file.
- **Safe writes.** Every database mutation goes through a WAL checkpoint + timestamped backup + single transaction with rollback.

## Acknowledgments

- Motivated by Issues [#11231](https://github.com/anomalyco/opencode/issues/11231), [#14292](https://github.com/anomalyco/opencode/issues/14292), [#19017](https://github.com/anomalyco/opencode/issues/19017) of OpenCode, and inspired by [BrianLan's export-opencode-sessions Skills](https://github.com/brianlan/improved-ai-agent/tree/master/skills/export-opencode-sessions). 

- Works on OpenCode v1.17.7 on Windows and Mac.

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
```

Default export path:

- `<project>/.opencode/conversations/` for markdown, and
- `<project>/.opencode/raw_conversations/` for raw JSON (supported by import).
- If the default export path no longer exists, falls back to `cwd`.

#### Importing

**Import** a session:

```bash
ocsm import session --from /path/to/session.json --to-project /path/to/proj        # import one session tree
ocsm import session --from /path/to/session.json --to-project /path/to/proj --no-substitute-paths  # import without replacing paths in the conversations
ocsm import project --from /path/to/proj --to-project /path/to/proj                # import all sessions from a project's raw export
ocsm import project --from /path/to/proj --to-project /path/to/proj --no-substitute-paths
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

```bash
# on machine A
ocsm sync project --from /path/to/proj      # writes DB sessions into the folder
# git push / cloud-sync the project folder
# on machine B (after git pull)
ocsm sync project --from /path/to/proj      # pulls new folder sessions into the local DB
```

**Common usage:**

```bash
ocsm sync project --from /path/to/proj                  # default: prompt on conflicts, deletions on
ocsm sync project --from /path/to/proj --dry-run        # preview the plan, write nothing
ocsm sync project --from /path/to/proj --on-conflict newer   # auto-resolve: newer time_updated wins
ocsm sync project --from /path/to/proj --on-conflict skip    # skip conflicting sessions
ocsm sync project --from /path/to/proj --no-delete      # turn off deletion propagation
ocsm sync project --from /path/to/proj -y               # non-interactive: skip confirmations
```

Full option list: `--on-conflict {ask,newer,skip}`, `--delete/--no-delete`, `--yes/-y`, `--dry-run`, `--substitute-paths/--no-substitute-paths`.

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

## License

MIT
