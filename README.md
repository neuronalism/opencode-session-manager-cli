# ocsm

CLI tool for managing [OpenCode](https://github.com/opencode-ai/opencode) sessions stored in SQLite.

## Installation

```bash
uv tool install .
```

## Usage

### List projects

```bash
ocsm list project
```

### List sessions

```bash
ocsm list session                    # root sessions only
ocsm list session --project <path>   # filter by project
ocsm list session --flat             # include subagent sessions (flat)
ocsm list session --tree             # include subagent sessions (tree)
```

### Export sessions

```bash
ocsm export session --from <id>               # export as markdown
ocsm export session --from <id> --format raw  # export as raw JSON (import-safe)
ocsm export session --from <id> --tree        # export with subagents
```

### Custom database path

```bash
ocsm --db <path> list session
# or
OCSM_DB_PATH=<path> ocsm list session
```

## License

MIT
