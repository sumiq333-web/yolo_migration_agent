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
) -> ToolProfile:
    tools = [
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

    handlers = {
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
) -> ToolProfile:
    base = build_engineer_profile(
        bus=bus,
        read_file_fn=read_file_fn,
        read_code_fn=read_code_fn,
    )

    tools = []

    for tool in base.tools:
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
    ]

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