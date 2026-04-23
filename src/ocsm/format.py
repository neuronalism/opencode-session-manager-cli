from __future__ import annotations

import json
from datetime import datetime, timezone

from pathlib import Path

from rich.text import Text


def fmt_time(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def format_timestamp(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def format_projects_list(rows: list) -> Text:
    parts: list[Text] = []
    for row in rows:
        block = Text()
        name = row["project_name"] or Path(row["directory"]).name
        block.append(f"  {name}\n", style="bold cyan")
        block.append(f"    {format_timestamp(row['latest_updated'])}\n", style="dim")
        block.append(f"    {row['directory']}\n")
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
    # Build parent -> children map, and identify roots
    children_map: dict[str, list] = {}
    rows_by_id: dict[str, dict] = {}
    for row in rows:
        rid = row["id"]
        rows_by_id[rid] = row
        pid = row["parent_id"]
        if pid:
            children_map.setdefault(pid, []).append(row)
        # track all child ids so we can find roots
    root_ids = [row["id"] for row in rows if row["parent_id"] is None]

    def render_node(row: dict, indent: str) -> Text:
        t = Text()
        t.append(f"{indent}- ", style="bold")
        t.append(f"{row['id']}\n", style="bold cyan")
        t.append(f"{indent}  {row['title'] or '(untitled)'}\n")
        t.append(f"{indent}  {format_timestamp(row['time_updated'])}\n", style="dim")
        t.append(f"{indent}  {row['directory']}\n")
        for child in children_map.get(row["id"], []):
            t.append(render_node(child, indent + "  "))
        return t

    result = Text()
    for i, rid in enumerate(root_ids):
        if i > 0:
            result.append("\n")
        result.append(render_node(rows_by_id[rid], ""))
    return result


def format_sessions_list(rows: list) -> Text:
    parts: list[Text] = []
    for row in rows:
        block = Text()
        block.append(f"  {row['id']}\n", style="bold cyan")
        title = row["title"] or "(untitled)"
        block.append(f"    {title}\n")
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


def session_to_markdown(session: dict, messages: list, *, thinking: bool = False, tool_calls: str = "info") -> str:
    out = f"# {session.get('title', session['id'])}\n\n"
    out += f"**Session ID:** {session['id']}\n"
    out += f"**Directory:** {session.get('directory', '')}\n"
    out += f"**Created:** {fmt_time(session['time_created'])}\n"
    out += f"**Updated:** {fmt_time(session['time_updated'])}\n\n"
    out += "---\n\n"
    for msg in messages:
        info = msg["info"]
        if info.get("role") == "user":
            out += "## User\n\n"
        else:
            agent = info.get("agent", "assistant")
            model = info.get("modelID", "unknown")
            created = info.get("time", {}).get("created")
            completed = info.get("time", {}).get("completed")
            duration = ""
            if isinstance(created, int) and isinstance(completed, int):
                duration = f" · {((completed - created) / 1000):.1f}s"
            out += f"## Assistant ({agent} · {model}{duration})\n\n"
        for part in msg.get("parts", []):
            kind = part.get("type")
            if kind == "text" and not part.get("synthetic"):
                out += f"{part.get('text', '')}\n\n"
            elif kind == "reasoning" and thinking:
                out += f"_Thinking:_\n\n{part.get('text', '')}\n\n"
            elif kind == "tool" and tool_calls != "none":
                state = part.get("state", {})
                tool_name = part.get("tool", "unknown")
                title = state.get("title", "")
                session_id = (state.get("metadata") or {}).get("sessionId", "")
                out += f"**Tool: {tool_name}** {title}"
                if session_id:
                    out += f" → `{session_id}`"
                out += "\n"
                if tool_calls == "details":
                    if state.get("input") is not None:
                        out += f"\n**Input:**\n```json\n{json.dumps(state.get('input'), indent=2)}\n```\n"
                    if state.get("status") == "completed" and state.get("output") is not None:
                        out += f"\n**Output:**\n```\n{state.get('output')}\n```\n"
                    if state.get("status") == "error" and state.get("error") is not None:
                        out += f"\n**Error:**\n```\n{state.get('error')}\n```\n"
                out += "\n"
        out += "---\n\n"
    return out


def session_to_json(session: dict, messages: list) -> str:
    payload = {"info": session, "messages": messages}
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def session_to_raw_json(session_row: dict, raw: dict) -> str:
    """Export session, messages, and parts as faithful table snapshots for re-import."""
    payload = {
        "session": session_row,
        "messages": raw["messages"],
        "parts": raw["parts"],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
