import os
from pathlib import Path

from agent.state import WORKDIR
from agent.workspace import client, MODEL
from tools.common_tool import run_read, normalize_messages, make_safe_filename
from tools.yolo_tools import scan_yolo_project, set_yolo_workspace
PROJECT_ROOT = WORKDIR.parent
SUBAGENT_OUTPUT_DIR = PROJECT_ROOT / ".agent_outputs"
SUBAGENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SUB_HANDLERS = {
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "scan_yolo_project": lambda **kw: scan_yolo_project(kw["path"]),
    "set_yolo_workspace": lambda **kw: set_yolo_workspace(kw["path"]),
}

SUB_TOOLS = [
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "scan_yolo_project",
        "description": "Scan one directory in a YOLO repo and return a compact JSON summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "set_yolo_workspace",
        "description": "Set yolo workspace when you get the yolo path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": ["path"]
        }
    },
]

SUB_SYSTEM = f"""
You are a YOLO analysis subagent at {WORKDIR}.

Rules:
- Work only on the delegated task
- Use tools first
- Do not ask the user questions
- Stop as soon as you have enough information for a concise final summary
- Do not keep exploring once the core architecture files are identified
- Return a concise final summary when finished
- Focus on repository analysis, not prose
- First call set_yolo_workspace with the YOLO workspace root
- After that, all scan_yolo_project and read_file calls must use paths under that YOLO workspace root
"""

def extract_response_text(content) -> str:
    texts = []
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)
    return "\\n".join(texts).strip()

def write_subagent_summary(filename: str, content: str) -> Path:
    path = SUBAGENT_OUTPUT_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path

def write_subagent_trace(filename: str, content: str) -> Path:
    path = SUBAGENT_OUTPUT_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path

def run_subagent(prompt: str, output_name: str = "subagent_summary.md") -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    final_summary = ""
    trace_lines = []
    trace_lines.append("# Subagent Trace")
    trace_lines.append("")
    trace_lines.append("## Prompt")
    trace_lines.append(prompt)
    trace_lines.append("")

    for i in range(10):
        trace_lines.append(f"## Turn {i + 1}")

        if i == 6:
            reminder = "You have enough information. Stop using tools now and write the final summary."
            sub_messages.append({
                "role": "user",
                "content": reminder
            })
            trace_lines.append("### Injected Reminder")
            trace_lines.append(reminder)
            trace_lines.append("")

        response = client.messages.create(
            model=MODEL,
            system=SUB_SYSTEM,
            messages=normalize_messages(sub_messages),
            tools=SUB_TOOLS,
            max_tokens=4000,
        )

        sub_messages.append({"role": "assistant", "content": response.content})

        trace_lines.append(f"### stop_reason: {response.stop_reason}")

        assistant_text = extract_response_text(response.content)
        if assistant_text:
            trace_lines.append("### Assistant Text")
            trace_lines.append(assistant_text)
            trace_lines.append("")

        tool_names = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                tool_names.append(block.name)
        if tool_names:
            trace_lines.append("### Tool Calls")
            for name in tool_names:
                trace_lines.append(f"- {name}")
            trace_lines.append("")

        if response.stop_reason != "tool_use":
            final_summary = assistant_text or ""
            trace_lines.append("### Final Summary Candidate")
            trace_lines.append(final_summary or "(empty)")
            trace_lines.append("")
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"

                trace_lines.append(f"#### Tool: {block.name}")
                trace_lines.append("Input:")
                trace_lines.append(str(block.input))
                trace_lines.append("")
                trace_lines.append("Output Preview:")
                trace_lines.append(str(output)[:500])
                trace_lines.append("")

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })

        sub_messages.append({"role": "user", "content": results})

    if not final_summary:
        for msg in reversed(sub_messages):
            if msg["role"] == "assistant":
                text = extract_response_text(msg["content"])
                if text:
                    final_summary = text
                    break

    if not final_summary:
        final_summary = "\n".join(trace_lines[-40:])

    safe_name = make_safe_filename(output_name)
    if not safe_name.endswith(".md"):
        safe_name += ".md"

    summary_path = write_subagent_summary(safe_name, final_summary)
    trace_name = safe_name.replace(".md", "_trace.md")
    trace_path = write_subagent_trace(trace_name, "\n".join(trace_lines))

    rel_summary = os.path.relpath(summary_path, PROJECT_ROOT)
    rel_trace = os.path.relpath(trace_path, PROJECT_ROOT)
    preview = final_summary[:200].strip()

    return (
        f"Subagent finished.\n"
        f"Summary saved to {rel_summary}\n"
        f"Trace saved to {rel_trace}\n"
        f"Preview:\n{preview}"
    )
