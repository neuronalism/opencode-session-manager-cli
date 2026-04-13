# ocsm

A simple CLI tool for managing [OpenCode](https://github.com/opencode-ai/opencode) sessions stored in SQLite. It handles renamed/removed project folders, allows exporting all conversations of a project in markdown format, and provides a way to even import raw jsons into database.

Tested on OpenCode v1.4.3.

> [!CAUTION]
>
> This tool may manipulate your local OpenCode database. Use with caution, and make sure to backup your database before using this tool.


## Using this tool

### Installation

```bash
uv tool install .
```

### Commands

**List** local projects/sessions:

```bash
ocsm list project                        # list all projects
ocsm list session                        # list root sessions
ocsm list session --project <path>       # filter by project
ocsm list session --flat                 # include subagent sessions (flat)
ocsm list session --tree                 # include subagent sessions (tree)
```

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
ocsm export project --from /path/to/proj                            # export all sessions of a project
ocsm export project --from /path/to/proj --format raw               # raw JSON
ocsm export project --from /path/to/proj --tree                     # with subagents
ocsm export project --from /path/to/proj --flat                     # with subagents (flat)
```

Options for exporting:

```bash
--format markdown                    # default, export as markdown. 
--format raw                         # raw JSON, faithful DB snapshot for re-import. Default export path: <project>/.opencode/raw_conversations/
--thinking True                      # include reasoning parts (default)
--thinking False                     # hide reasoning parts
--tool-call none                     # hide tool calls
--tool-call info                     # show tool name, title, linked session (default)
--tool-call details                  # show tool name, title, session, input, output, error
--tree                               # subagent sessions in nested subdirectories
--flat                               # subagent sessions in same directory
```

Default export path: 
- <project>/.opencode/conversations/ for markdown, and <project>/.opencode/raw_conversations/ for raw JSON.
- If the default export path no longer exists, falls back to `cwd`.

### Use custom database path

- `ocsm --db <path> list session`
- `OCSM_DB_PATH=<path> ocsm list session`
- Default: `~/.config/opencode/opencode.db`

## License

MIT
