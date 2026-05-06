from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from agent.message_bus import MessageBus


ToolHandler = Callable[..., str]


@dataclass
class ToolProfile:
    """
    A teammate tool profile.

    Each teammate role gets its own tools and handlers.
    This prevents all teammates from sharing the same unrestricted toolbox.
    """

    name: str
    tools: list[dict]
    handlers: dict[str, ToolHandler]


def build_engineer_profile(
    *,
    bus: MessageBus,
    read_file_fn,
    read_code_fn,
    write_file_fn,
    edit_file_fn,
    scan_yolo_fn,
    bash_fn,
    run_python_fn,
    task_manager,
    get_task_id_fn=None,
) -> ToolProfile:
    tools = [
        {
            "name": "scan_yolo_project",
            "description": "Scan a YOLO project directory for its structure.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "read_file",
            "description": "Read file contents.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "read_code",
            "description": "Read a Python code file structurally.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["index", "focus"],
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "goal": {"type": "string"},
                },
                "required": ["path", "mode"],
            },
        },
        {
            "name": "write_file",
            "description": "Write content to a file. Set create=True only when the task explicitly asks to create a new file. Otherwise the file must already exist.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "edit_file",
            "description": "Replace exact text in a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "name": "run_shell",
            "description": "Run a shell command. On Windows this runs under cmd.exe. Use Windows-compatible syntax: dir, type, echo, python. Avoid cat/grep/heredoc.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "run_python",
            "description": "Run a Python code snippet directly (no shell). Use for YAML validation, file parsing, or quick checks.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                },
                "required": ["code"],
            },
        },
        {
            "name": "todo_update",
            "description": "Update status of one todo item in your assigned task. Use failed/blocked with a reason when you cannot complete a step. This tool is granted by the leader at dispatch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "blocked", "skipped"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required when status is failed or blocked.",
                    },
                },
                "required": ["index", "status"],
            },
        },
        {
            "name": "send_message",
            "description": "Send a task result or question to lead.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "msg_type": {
                        "type": "string",
                        "enum": ["task_result", "message", "error"],
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "read_inbox",
            "description": "Read and drain your own inbox.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]

    def _handle_todo_update(sender, task_manager, get_task_id_fn, index, status, reason=""):
        task_id = get_task_id_fn(sender) if get_task_id_fn else None
        if task_id is None:
            return "Error: no active task assigned to you. Wait for a dispatch."
        task = task_manager.get(int(task_id))
        if task["owner"] != sender:
            return f"Error: task #{task_id} is not assigned to {sender}"
        try:
            return json.dumps(
                task_manager.update_todo_item(int(task_id), int(index), status, reason or ""),
                ensure_ascii=False, indent=2,
            )
        except ValueError as e:
            return f"Error: {e}"

    handlers = {
        "scan_yolo_project": lambda sender, **kw: str(
            scan_yolo_fn(kw["path"])
        ),
        "read_file": lambda sender, **kw: str(
            read_file_fn(kw["path"], kw.get("limit"))
        ),
        "read_code": lambda sender, **kw: str(
            read_code_fn(
                kw["path"],
                kw["mode"],
                kw.get("symbols"),
                kw.get("goal"),
            )
        ),
        "write_file": lambda sender, **kw: str(
            write_file_fn(kw["path"], kw["content"], kw.get("create", False))
        ),
        "edit_file": lambda sender, **kw: str(
            edit_file_fn(kw["path"], kw["old_text"], kw["new_text"])
        ),
        "run_shell": lambda sender, **kw: str(
            bash_fn(kw["command"])
        ),
        "run_python": lambda sender, **kw: str(
            run_python_fn(kw["code"])
        ),
        "todo_update": lambda sender, **kw: _handle_todo_update(
            sender, task_manager, get_task_id_fn, kw["index"], kw["status"], kw.get("reason", "")
        ),
        "send_message": lambda sender, **kw: bus.send(
            sender=sender,
            to="lead",
            content=kw["content"],
            msg_type=kw.get("msg_type", "task_result"),
        ),
        "read_inbox": lambda sender, **kw: json.dumps(
            bus.read_inbox(sender),
            ensure_ascii=False,
            indent=2,
        ),
    }

    return ToolProfile(
        name="engineer",
        tools=tools,
        handlers=handlers,
    )


def build_reviewer_profile(
    *,
    bus: MessageBus,
    read_file_fn,
    read_code_fn,
    write_file_fn,
    edit_file_fn,
    scan_yolo_fn,
    bash_fn,
    run_python_fn,
    task_manager,
    get_task_id_fn=None,
) -> ToolProfile:
    base = build_engineer_profile(
        bus=bus,
        read_file_fn=read_file_fn,
        read_code_fn=read_code_fn,
        write_file_fn=write_file_fn,
        edit_file_fn=edit_file_fn,
        scan_yolo_fn=scan_yolo_fn,
        bash_fn=bash_fn,
        run_python_fn=run_python_fn,
        task_manager=task_manager,
        get_task_id_fn=get_task_id_fn,
    )

    MUTATING_TOOLS = {"write_file", "edit_file", "run_shell"}
    tools = []

    for tool in base.tools:
        if tool["name"] in MUTATING_TOOLS:
            continue
        if tool["name"] == "send_message":
            tools.append(
                {
                    "name": "send_message",
                    "description": "Send a review result to lead.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "msg_type": {
                                "type": "string",
                                "enum": ["review_result", "message", "error"],
                            },
                        },
                        "required": ["content"],
                    },
                }
            )
        else:
            tools.append(tool)

    handlers = dict(base.handlers)
    handlers["send_message"] = lambda sender, **kw: bus.send(
        sender=sender,
        to="lead",
        content=kw["content"],
        msg_type=kw.get("msg_type", "review_result"),
    )
    for name in MUTATING_TOOLS:
        handlers.pop(name, None)

    return ToolProfile(
        name="reviewer",
        tools=tools,
        handlers=handlers,
    )


def build_experiment_runner_profile(
    *,
    bus: MessageBus,
    background_run_fn=None,
    background_check_fn=None,
    background_list_fn=None,
    write_file_fn=None,
    edit_file_fn=None,
    task_manager=None,
    get_task_id_fn=None,
) -> ToolProfile:
    tools = [
        {
            "name": "send_message",
            "description": "Send an experiment result or command proposal to lead.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "msg_type": {
                        "type": "string",
                        "enum": ["experiment_result", "message", "error"],
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "read_inbox",
            "description": "Read and drain your own inbox.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "write_file",
            "description": "Write content to a file. Set create=True only when the task explicitly asks to create a new file. Otherwise the file must already exist.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "edit_file",
            "description": "Replace exact text in a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "name": "todo_update",
            "description": "Update status of one todo item in your assigned task. Use failed/blocked with a reason when you cannot complete a step. This tool is granted by the leader at dispatch.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "blocked", "skipped"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Required when status is failed or blocked.",
                    },
                },
                "required": ["index", "status"],
            },
        },
    ]

    def _handle_todo_update(sender, task_manager, get_task_id_fn, index, status, reason=""):
        task_id = get_task_id_fn(sender) if get_task_id_fn else None
        if task_id is None:
            return "Error: no active task assigned to you. Wait for a dispatch."
        task = task_manager.get(int(task_id))
        if task["owner"] != sender:
            return f"Error: task #{task_id} is not assigned to {sender}"
        try:
            return json.dumps(
                task_manager.update_todo_item(int(task_id), int(index), status, reason or ""),
                ensure_ascii=False, indent=2,
            )
        except ValueError as e:
            return f"Error: {e}"

    handlers: dict[str, ToolHandler] = {
        "send_message": lambda sender, **kw: bus.send(
            sender=sender,
            to="lead",
            content=kw["content"],
            msg_type=kw.get("msg_type", "experiment_result"),
        ),
        "read_inbox": lambda sender, **kw: json.dumps(
            bus.read_inbox(sender),
            ensure_ascii=False,
            indent=2,
        ),
        "write_file": lambda sender, **kw: str(
            write_file_fn(kw["path"], kw["content"], kw.get("create", False))
        ) if write_file_fn else "Error: write_file not available",
        "edit_file": lambda sender, **kw: str(
            edit_file_fn(kw["path"], kw["old_text"], kw["new_text"])
        ) if edit_file_fn else "Error: edit_file not available",
        "todo_update": lambda sender, **kw: _handle_todo_update(
            sender, task_manager, get_task_id_fn, kw["index"], kw["status"], kw.get("reason", "")
        ) if task_manager else "Error: todo_update not available",
    }

    if background_run_fn is not None:
        tools.append(
            {
                "name": "background_run",
                "description": "Run a test or experiment command in the background.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer"},
                    },
                    "required": ["command"],
                },
            }
        )
        handlers["background_run"] = lambda sender, **kw: str(
            background_run_fn(kw["command"], kw.get("timeout", 300))
        )

    if background_check_fn is not None:
        tools.append(
            {
                "name": "background_check",
                "description": "Check one background runtime task.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                    "required": ["id"],
                },
            }
        )
        handlers["background_check"] = lambda sender, **kw: str(
            background_check_fn(kw["id"])
        )

    if background_list_fn is not None:
        tools.append(
            {
                "name": "background_list",
                "description": "List background runtime tasks.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
        )
        handlers["background_list"] = lambda sender, **kw: str(
            background_list_fn()
        )

    return ToolProfile(
        name="experiment_runner",
        tools=tools,
        handlers=handlers,
    )