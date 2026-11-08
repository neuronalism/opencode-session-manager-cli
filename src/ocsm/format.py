"""Rendering helpers for ocsm v2 output (session_v2 + session_message shapes)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text

TODO_STATUS_ICON = {
    "completed": "[x]",
    "in_progress": "[>]",
    "pending": "[ ]",
    "cancelled": "[-]",
}


def fmt_time(value) -> str:
    if value is None:
        return "?"
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def format_timestamp(ms) -> str:
    if ms is None:
        return "?"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def format_projects_list(rows: list) -> Text:
    parts: list[Text] = []
    for row in rows:
        block = Text()
        name = row.get("name") or Path(row["worktree"]).name or row["id"]
        block.append(f"  {name}\n", style="bold cyan")
        block.append(f"    ID: {row['id']}\n", style="dim")
        if row.get("vcs"):
            block.append(f"    vcs: {row['vcs']}\n", style="dim")
        block.append(f"    {format_timestamp(row.get('latest_updated'))}\n", style="dim")
        block.append(f"    {row['worktree']}\n")
        block.append(f"    {row['session_count']} sessions\n", style="dim")
        parts.append(block)
    result = Text()
    for i, block in enumerate(parts):
        if i > 0:
            result.append("\n")
        result.append("- ")
        result.append(block)
    return result


def format_sessions_tree(rows: list) -> Text:
    children_map: dict[str, list] = {}
    for row in rows:
        pid = row["parent_id"]
        if pid:
            children_map.setdefault(pid, []).append(row)
    root_ids = [row["id"] for row in rows if not row["parent_id"]]

    def render_node(row: dict, indent: str) -> Text:
        t = Text()
        t.append(f"{indent}- ", style="bold")
        title = row["title"] or "(untitled)"
        if row["time_archived"]:
            t.append(f"{title} [archived]\n", style="bold cyan")
        else:
            t.append(f"{title}\n", style="bold cyan")
        t.append(f"{indent}  ID: {row['id']}\n", style="dim")
        t.append(f"{indent}  {format_timestamp(row['time_updated'])}\n", style="dim")
        t.append(f"{indent}  {row['directory']}\n")
        for child in children_map.get(row["id"], []):
            t.append(render_node(child, indent + "  "))
        return t

    result = Text()
    by_id = {row["id"]: row for row in rows}
    for i, rid in enumerate(root_ids):
        if i > 0:
            result.append("\n")
        result.append(render_node(by_id[rid], ""))
    return result


def format_sessions_list(rows: list) -> Text:
    parts: list[Text] = []
    for row in rows:
        block = Text()
        title = row["title"] or "(untitled)"
        block.append(f"  {title}\n", style="bold cyan")
        block.append(f"    ID: {row['id']}\n", style="dim")
        block.append(f"    {format_timestamp(row['time_updated'])}\n", style="dim")
        block.append(f"    {row['directory']}\n")
        parts.append(block)
    result = Text()
    for i, block in enumerate(parts):
        if i > 0:
            result.append("\n")
        result.append("- ")
        result.append(block)
    return result


def _render_tool(part: dict, tool_calls: str) -> str:
    state = part.get("state") or {}
    name = part.get("name", "unknown")
    status = state.get("status", "")
    out = f"**Tool: {name}** [{status}]"
    session_id = (state.get("metadata") or {}).get("sessionId", "")
    if session_id:
        out += f" → `{session_id}`"
    out += "\n"
    if tool_calls == "details":
        if state.get("input") is not None:
            out += f"\n**Input:**\n```json\n{json.dumps(state.get('input'), indent=2, ensure_ascii=False)}\n```\n"
        if state.get("status") == "completed" and state.get("output") is not None:
            out += f"\n**Output:**\n```\n{state.get('output')}\n```\n"
        if state.get("status") == "error" and state.get("error") is not None:
            out += f"\n**Error:**\n```\n{state.get('error')}\n```\n"
    return out + "\n"


def _render_todos(todos: list[dict]) -> str:
    out = "## Todos\n\n"
    for todo in todos:
        icon = TODO_STATUS_ICON.get(todo.get("status", ""), "[?]")
        priority = todo.get("priority")
        label = f" ({priority})" if priority else ""
        out += f"- {icon}{label} {todo.get('content', '')}\n"
    return out + "\n"


def session_to_markdown(
    session: dict,
    messages: list,
    *,
    thinking: bool = False,
    tool_calls: str = "info",
    todos: list[dict] | None = None,
) -> str:
    """Render a v2 session (``session_v2`` row + ``session_messages`` with
    parsed ``data``) as markdown. ``todos`` is the derived snapshot from
    :func:`ocsm.queries.latest_todos`."""
    out = f"# {session.get('title') or session['id']}\n\n"
    out += f"**Session ID:** {session['id']}\n"
    out += f"**Directory:** {session.get('directory', '')}\n"
    if session.get("parent_id"):
        out += f"**Parent:** {session['parent_id']}\n"
    out += f"**Created:** {fmt_time(session['time_created'])}\n"
    out += f"**Updated:** {fmt_time(session['time_updated'])}\n"
    out += "\n---\n\n"
    if todos:
        out += _render_todos(todos)
    for message in messages:
        data = message.get("data") or {}
        mtype = message.get("type")
        if mtype == "user":
            out += "## User\n\n"
            out += f"{data.get('text', '')}\n\n"
        elif mtype == "assistant":
            agent = data.get("agent", "assistant")
            model = (data.get("model") or {}).get("id", "unknown")
            created = (data.get("time") or {}).get("created")
            completed = (data.get("time") or {}).get("completed")
            duration = ""
            if isinstance(created, int) and isinstance(completed, int):
                duration = f" · {((completed - created) / 1000):.1f}s"
            out += f"## Assistant ({agent} · {model}{duration})\n\n"
            for part in data.get("content") or []:
                kind = part.get("type")
                if kind == "text" and not part.get("synthetic"):
                    out += f"{part.get('text', '')}\n\n"
                elif kind == "reasoning" and thinking:
                    out += f"_Thinking:_\n\n{part.get('text', '')}\n\n"
                elif kind == "tool" and tool_calls != "none":
                    out += _render_tool(part, tool_calls)
        elif mtype == "system":
            out += "## System\n\n"
            out += f"{data.get('text', '')}\n\n"
        elif mtype == "compaction":
            out += "## Compaction\n\n"
            summary = data.get("summary")
            if summary:
                out += f"{summary}\n\n" if isinstance(summary, str) else f"```json\n{json.dumps(summary, indent=2, ensure_ascii=False)}\n```\n\n"
        else:
            out += f"## {mtype or 'message'}\n\n"
            out += f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```\n\n"
        out += "---\n\n"
    return out


def session_to_json(session_row: dict, messages: list) -> str:
    """Readable JSON dump: full session row + messages with parsed data."""
    payload = {"session": session_row, "messages": messages}
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def session_to_raw_json(session_row: dict, messages: list, blobs: list | None = None) -> str:
    """Import-safe raw payload: ``{"session", "messages", "blobs?"}``.

    ``session`` is the full ``session_v2`` row; ``messages`` are the merged
    ``session_message`` rows (``data`` parsed, ``session_part`` rows folded
    back into ``data.content``); ``blobs`` carries any ``media:`` referents so
    the tree survives a round-trip when ``experimental.media_blob`` was on.
    """
    payload: dict = {"session": session_row, "messages": messages}
    if blobs:
        payload["blobs"] = blobs
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
