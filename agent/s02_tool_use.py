#!/usr/bin/env python3
# Harness: tool dispatch -- expanding what the model can reach.
"""
s02_tool_use.py - Tool dispatch + message normalization

The agent loop from s01 didn't change. We added tools to the dispatch map,
and a normalize_messages() function that cleans up the message list before
each API call.

Key insight: "The loop didn't change at all. I just added tools."
"""
from pathlib import Path
import time
import json
import random
import threading
from agent.skillRegistry import SkillRegistry
from agent.subagent import run_subagent
from agent.state import WORKDIR, WORKSPACE_ROOT, TODO_STATE
from agent.workspace import client, MODEL
from tools.common_tool import (
    todo_note_round_without_update,
    todo_reminder,
    run_bash,
    run_python,
    run_read,
    run_read_code,
    todo_update,
    run_write,
    run_edit,
    normalize_messages,
    prepare_tool_result,
    micro_compact_messages,
)
from tools.yolo_tools import scan_yolo_project, set_yolo_workspace
from tools.paper_tools import paper_tools
from agent.permissionDecision import check_permission
from agent.agent_runner import AgentRunner, AgentRunnerConfig
from agent.hook_manager import HookManager,permission_hook, tool_output_hook, micro_compact_hook
from memory import (
    memory_store,
    run_save_memory,
    run_list_memories,
    run_read_memory,
    run_delete_memory,
)
from build_prompt import SystemPromptBuilder
from restore import (
    new_recovery_state,
    choose_recovery,
    can_attempt,
    record_attempt,
    apply_continue_recovery,
    apply_compact_recovery,
    apply_backoff_recovery,
)
from agent.background_manager import BackgroundManager
from agent.cron_scheduler import CronScheduler
from agent.team_recovery import TeamRecovery
from agent.tool_runtime import execute_tool_block
from agent.model_runtime import call_model_with_recovery
from agent.team_protocol import RequestStore
CRON_SCHEDULE_FILE = WORKSPACE_ROOT / ".schedules" / "scheduled_tasks.json"

CRON_SCHEDULER = CronScheduler(
    schedule_file=CRON_SCHEDULE_FILE,
    check_interval_seconds=1,
)

print(CRON_SCHEDULER.start())

RUNTIME_TASKS_DIR = WORKSPACE_ROOT / ".runtime-tasks"
BACKGROUND_MANAGER = BackgroundManager(
    runtime_dir=RUNTIME_TASKS_DIR,
    cwd=Path(WORKDIR),
)
HOOKS = HookManager()

HOOKS.register(
    event_name="before_model_call",
    name="micro_compact",
    handler=micro_compact_hook,
    priority=100,
)

HOOKS.register(
    event_name="before_tool_execute",
    name="permission",
    handler=permission_hook,
    priority=10,
)

HOOKS.register(
    event_name="after_tool_execute",
    name="tool_output_processing",
    handler=tool_output_hook,
    priority=100,
)
from agent.task_manager import TaskManager
from agent.state import WORKSPACE_ROOT

from agent.message_bus import MessageBus
from agent.team_tools import (
    build_engineer_profile,
    build_reviewer_profile,
    build_experiment_runner_profile,
)
from agent.teammate_manager import TeammateManager

TASKS_DIR = WORKSPACE_ROOT / ".tasks"
TASK_MANAGER = TaskManager(TASKS_DIR)
PROJECT_ROOT = WORKSPACE_ROOT
SKILL_REGISTRY = SkillRegistry(Path(PROJECT_ROOT / "skill"))
MODEL_REQUEST_RETRIES = 4
MODEL_RETRY_DELAY_SECONDS = 2
MODEL_CALL_LOCK = threading.Lock()
TEAM_DIR = WORKSPACE_ROOT / ".team"
TEAM_INBOX_DIR = TEAM_DIR / "inbox"
TEAM_REQUESTS_FILE = TEAM_DIR / "requests.json"
REQUEST_STORE = RequestStore(TEAM_REQUESTS_FILE)
TEAM_BUS = MessageBus(TEAM_INBOX_DIR)

TEAM_TOOL_PROFILES = {
    "engineer": build_engineer_profile(
        bus=TEAM_BUS,
        read_file_fn=run_read,
        read_code_fn=run_read_code,
        write_file_fn=run_write,
        edit_file_fn=run_edit,
        scan_yolo_fn=scan_yolo_project,
        bash_fn=run_bash,
        run_python_fn=run_python,
        task_manager=TASK_MANAGER,
        get_task_id_fn=lambda name: TEAM_MANAGER.get_active_task_id(name),
    ),
    "reviewer": build_reviewer_profile(
        bus=TEAM_BUS,
        read_file_fn=run_read,
        read_code_fn=run_read_code,
        write_file_fn=run_write,
        edit_file_fn=run_edit,
        scan_yolo_fn=scan_yolo_project,
        bash_fn=run_bash,
        run_python_fn=run_python,
        task_manager=TASK_MANAGER,
        get_task_id_fn=lambda name: TEAM_MANAGER.get_active_task_id(name),
    ),
    "experiment_runner": build_experiment_runner_profile(
        bus=TEAM_BUS,
        background_run_fn=BACKGROUND_MANAGER.run,
        background_check_fn=BACKGROUND_MANAGER.check,
        background_list_fn=lambda: BACKGROUND_MANAGER.check(),
        write_file_fn=run_write,
        edit_file_fn=run_edit,
        task_manager=TASK_MANAGER,
        get_task_id_fn=lambda name: TEAM_MANAGER.get_active_task_id(name),
    ),
}

TEAM_MANAGER = None

# -- Concurrency safety classification --
# Read-only tools can safely run in parallel; mutating tools must be serialized.
CONCURRENCY_SAFE = {"read_file"}
CONCURRENCY_UNSAFE = {"write_file", "edit_file"}
def read_and_sync_lead_inbox() -> list[dict]:
    inbox = TEAM_BUS.read_inbox("lead")

    if inbox:
        sync_requests_from_lead_inbox(inbox)

    return inbox
def maybe_mark_prompt_dirty(tool_name: str, tool_output: str) -> None:
    """
    Mark the stable prompt dirty only when a tool likely changed
    stable prompt inputs.
    """
    if tool_name not in STABLE_PROMPT_MUTATION_TOOLS:
        return

    # 只有成功时才标脏；Error: 开头说明没有真正改动
    if isinstance(tool_output, str) and tool_output.startswith("Error:"):
        return

    PROMPT_BUILDER.mark_stable_dirty()

def _is_task_already_assigned_to(task: dict, teammate: str) -> bool:
    return(
                task.get("owner") == teammate
                and task.get("status") in ("in_progress", "blocked", "failed")
    )
# -- The dispatch map: {tool_name: handler} --
def _handle_final_report(kw: dict) -> str:
    """
    标记最终汇报已完成。

    约束：
    1. summary 必须非空。
    2. 只有所有 task 都进入 terminal 状态后才能调用。
    3. 幂等：重复调用不覆盖第一次报告。
    """
    summary = str(kw.get("summary", "")).strip()

    if not summary:
        return "Error: task_final_report requires a non-empty summary."

    if _has_active_tasks():
        return (
            "Error: task_final_report cannot be called while active tasks still exist. "
            "Resolve pending, in_progress, or blocked tasks first."
        )

    if FINAL_REPORT_STATE["reported"]:
        return "Final report already recorded."

    FINAL_REPORT_STATE["reported"] = True
    FINAL_REPORT_STATE["summary"] = summary

    return f"Final report recorded: {summary[:200]}"


def _handle_dispatch(kw: dict) -> str:
    """Dispatch a task to a teammate. If task_id is set, assign owner + in_progress first."""
    task_id = kw.get("task_id")
    to = kw["to"]
    allow_retry = bool(kw.get("allow_retry", False))

    if task_id is not None:
        try:
            task = TASK_MANAGER.get(int(task_id))

            if not task.get("workspace"):
                return "Error: cannot dispatch task without workspace. Call task_set_workspace first."

            if not task.get("todos"):
                return "Error: cannot dispatch task without todos. Call task_set_todos first."

            if _is_task_already_assigned_to(task, to) and not allow_retry:
                return (
                    f"Error: task #{task_id} is already assigned to {to} "
                    f"with status={task.get('status')}. "
                    "Do not redispatch unless the user explicitly asks to retry and allow_retry=true."
                )

            TASK_MANAGER.assign(int(task_id), to)
            TASK_MANAGER.set_status(int(task_id), "in_progress")

        except ValueError as e:
            return f"Error: {e}"

    return TEAM_MANAGER.dispatch(
        to=to,
        content=_build_dispatch_content(kw),
        msg_type="task_request",
    )

def _build_dispatch_content(kw: dict) -> str:
    """If task_id is present, embed task context in the dispatch message."""
    content = kw.get("content", "")
    task_id = kw.get("task_id")
    if task_id is None:
        return content
    try:
        task = TASK_MANAGER.get(int(task_id))
    except (ValueError, KeyError):
        return content
    header = {
        "task_id": task["id"],
        "subject": task["subject"],
        "workspace": task.get("workspace", ""),
        "cwd": task.get("cwd", task.get("workspace", "")),
        "allowed_roots": task.get("allowed_roots", []),
        "todos": task.get("todos", []),
    }
    return (
        f"<task-context>\n"
        f"{json.dumps(header, ensure_ascii=False, indent=2)}\n"
        f"</task-context>\n\n"
        f"{content}"
    )

def _looks_like_team_wait_prompt(prompt: str) -> bool:
    text = str(prompt or "").lower()
    patterns = (
        "wait for engineer",
        "wait for reviewer",
        "wait for experiment_runner",
        "wait for teammate",
        "waiting for engineer",
        "waiting for teammate",
        "check engineer",
        "check teammate",
        "read inbox",
        "team inbox",
        "follow-up response",
        "teammate response",
    )
    return any(pattern in text for pattern in patterns)


def _handle_schedule_after(kw: dict) -> str:
    prompt = str(kw.get("prompt", ""))

    if _looks_like_team_wait_prompt(prompt):
        return (
            "Error: schedule_after must not be used to wait for teammate responses. "
            "The runtime team message loop handles teammate waiting."
        )

    return CRON_SCHEDULER.create_after(
        kw["delay_seconds"],
        prompt,
        kw.get("durable", False),
    )

def auto_compact_for_recovery(messages: list) -> list:
    compacted_messages, _changed = micro_compact_messages(messages)
    return compacted_messages

def _handle_task_set_todos(kw: dict) -> str:
    task_id = kw.get("task_id", kw.get("id"))
    if task_id is None:
        return "Error: task_set_todos requires task_id. Use task_id, not id."
    try:
        return json.dumps(
            TASK_MANAGER.set_todos(int(task_id), kw["todos"]),
            ensure_ascii=False,
            indent=2,
        )
    except ValueError as e:
        return f"Error: {e}"

TOOL_HANDLERS = {
    "run_shell":  lambda **kw: run_bash(kw["command"]),
    "run_python": lambda **kw: run_python(kw["code"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"], kw.get("create", False)),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "scan_paper": lambda **kw: paper_tools(),
    "scan_yolo_project": lambda **kw: scan_yolo_project(kw["path"]),
    "set_yolo_workspace": lambda **kw: set_yolo_workspace(kw["path"]),
    "todo": lambda **kw: todo_update(kw["items"]),
    # "task": lambda **kw: run_subagent(kw["prompt"],kw.get("output_name", "subagent_summary.md"),),
    "load_skill": lambda **kw: SKILL_REGISTRY.load_full_text(kw["name"]),
    "read_code": lambda **kw: run_read_code(
        kw["path"],
        kw["mode"],
        kw.get("symbols"),
        kw.get("goal"),
    ),
    "save_memory": lambda **kw: run_save_memory(
        kw["name"], kw["memory_type"], kw["description"], kw["content"]
    ),
    "list_memories": lambda **kw: run_list_memories(),
    "read_memory": lambda **kw: run_read_memory(kw["name"]),
    "delete_memory": lambda **kw: run_delete_memory(kw["name"]),
    "task_create_graph": lambda **kw: TASK_MANAGER.create_graph(
        kw["tasks"],
        kw.get("dependencies", []),
    ),
    "task_list": lambda **kw: TASK_MANAGER.render_list(
        kw.get("include_deleted", False)
    ),
    "task_get": lambda **kw: TASK_MANAGER.get(kw["id"]),
    "task_ready": lambda **kw: TASK_MANAGER.render_ready(),
    "task_final_report": lambda **kw: _handle_final_report(kw),
    "task_set_status": lambda **kw: TASK_MANAGER.set_status(
    kw["id"],
    kw["status"],
    kw.get("reason", ""),
    kw.get("cascade_failed", False),
    ),
    "task_set_workspace": lambda **kw: json.dumps(
        TASK_MANAGER.set_workspace(kw["task_id"], kw["path"]),
        ensure_ascii=False, indent=2,
    ),
    "task_set_todos": lambda **kw: _handle_task_set_todos(kw),
    "background_run": lambda **kw: BACKGROUND_MANAGER.run(
        kw["command"],
        kw.get("timeout", 300),
    ),
    "background_check": lambda **kw: BACKGROUND_MANAGER.check(
        kw.get("id"),
    ),
    "background_list": lambda **kw: BACKGROUND_MANAGER.check(),
    "cron_create": lambda **kw: CRON_SCHEDULER.create(
        kw["cron"],
        kw["prompt"],
        kw.get("recurring", True),
        kw.get("durable", False),
    ),
    "cron_delete": lambda **kw: CRON_SCHEDULER.delete(kw["id"]),
    "cron_list": lambda **kw: CRON_SCHEDULER.list_tasks(),
    "schedule_after": lambda **kw: _handle_schedule_after(kw),
    "team_init": lambda **kw: TEAM_MANAGER.ensure_default_team(),
    "list_teammates": lambda **kw: TEAM_MANAGER.list_all(),
    "wake_teammate": lambda **kw: TEAM_MANAGER.wake(kw["name"]),
    "shutdown_teammate": lambda **kw: TEAM_MANAGER.shutdown(kw["name"]),
    "send_message": lambda **kw: TEAM_BUS.send(
        sender="lead",
        to=kw["to"],
        content=kw["content"],
        msg_type=kw.get("msg_type", "message"),
    ),
    "read_inbox": lambda **kw: json.dumps(
        read_and_sync_lead_inbox(),
        ensure_ascii=False,
        indent=2,
    ),
    "broadcast": lambda **kw: TEAM_BUS.broadcast(
        sender="lead",
        recipients=TEAM_MANAGER.member_names(),
        content=kw["content"],
        msg_type=kw.get("msg_type", "broadcast"),
    ),
    "dispatch_to_teammate": lambda **kw: _handle_dispatch(kw),
    "request_list": lambda **kw: REQUEST_STORE.render_requests(
        include_terminal=kw.get("include_terminal", True),
        limit=kw.get("limit", 20),
    ),
    "request_get": lambda **kw: json.dumps(
        REQUEST_STORE.get_request(kw["request_id"]),
        ensure_ascii=False,
        indent=2,
    ),
    "request_cancel": lambda **kw: json.dumps(
        REQUEST_STORE.cancel_request(
            request_id=kw["request_id"],
            reason=kw.get("reason", "Request cancelled by lead."),
        ),
        ensure_ascii=False,
        indent=2,
    ),
}
STABLE_PROMPT_MUTATION_TOOLS = {
    "save_memory",
    "delete_memory",
    # 以后如果你支持动态增删 skill、改 instruction 文件，也加进来
}
TOOLS = [
    {"name": "run_shell", "description": "Run a shell command. On Windows this runs under cmd.exe. Use Windows-compatible syntax (dir, type, echo, python). For Linux commands like cat/grep use python instead.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "run_python", "description": "Run a Python code snippet. Use this for YAML validation, file inspection, or quick checks. No shell involved — safe and reliable on all platforms.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file. Only existing files unless create=True.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "create": {"type": "boolean"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
{
        "name":"scan_paper",
        "description":"Scan a paper to get the github address",
        "input_schema":{
            "type": "object",
            "properties": {},
            "required": [],
        }
},
    {
        "name":"scan_yolo_project",
        "description":"Inspect a YOLO directory and return a compact summary of its immediate subdirectories and files.",
        "input_schema":{
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    },
{
        "name":"set_yolo_workspace",
        "description":"Set the workspace root for the current YOLO repository.",
        "input_schema":{
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
    },
{
    "name": "todo",
    "description": "Rewrite the current session plan for multi-step work.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"]
                        },
                        "activeForm": {"type": "string"}
                    },
                    "required": ["content", "status", "activeForm"]
                }
            }
        },
        "required": ["items"]
    }
},

{
    "name": "load_skill",
    "description": "Load the full text of a skill when specialized guidance is needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}
        },
        "required": ["name"]
    }
},
{
    "name": "read_code",
    "description": "Read a Python code file structurally. Use this for large source files when read_file is too coarse. Supports indexing classes/functions and focusing on specific symbols.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": ["index", "focus"]
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"}
            },
            "goal": {"type": "string"}
        },
        "required": ["path", "mode"]
    }
},
{
    "name": "save_memory",
    "description": "Save a persistent memory that survives across sessions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "memory_type": {
                "type": "string",
                "enum": ["user", "feedback", "project", "reference"]
            },
            "description": {"type": "string"},
            "content": {"type": "string"}
        },
        "required": ["name", "memory_type", "description", "content"]
    }
},
{
    "name": "list_memories",
    "description": "List current persistent memories.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
},
{
    "name": "read_memory",
    "description": "Read one persistent memory by name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}
        },
        "required": ["name"]
    }
},
{
    "name": "delete_memory",
    "description": "Delete one persistent memory by name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}
        },
        "required": ["name"]
    }
},
{
    "name": "task_create_graph",
    "description": (
    "Create a persistent task graph in one operation. "
    "Use this for multiple related tasks with dependencies. "
    "This tool only changes durable task state. "
    "After creating the graph, continue into execution only if the user explicitly asked to start, continue, execute, analyze, implement, or work on a ready task. "
    "Otherwise, report the graph state and ready tasks."
),
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "subject": {"type": "string"},
                        "description": {"type": "string"},
                        "owner": {"type": "string"},
                    },
                    "required": ["key", "subject"],
                },
            },
            "dependencies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "blocker": {
                            "type": "string",
                            "description": "Task key that must complete first",
                        },
                        "blocked": {
                            "type": "string",
                            "description": "Task key that is blocked",
                        },
                    },
                    "required": ["blocker", "blocked"],
                },
            },
        },
        "required": ["tasks"],
    },
},
{
    "name": "task_list",
    "description": "List persistent project tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "include_deleted": {"type": "boolean"},
        },
        "required": [],
    },
},
{
    "name": "task_get",
    "description": "Get one persistent task by id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
        },
        "required": ["id"],
    },
},
{
    "name": "task_ready",
    "description": (
    "List tasks that are pending and have no remaining blockers. "
    "Use this to decide what can be started next. "
    "Starting a ready task requires explicit user intent or an active execution mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},
{
    "name": "task_final_report",
    "description": "Call this to deliver the final natural-language summary to the user and exit the agent loop. Required when all tasks have reached a terminal state (completed / failed / deleted).",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Short natural-language summary of what happened across all tasks."},
        },
        "required": ["summary"],
    },
},
{
    "name": "task_set_status",
    "description": (
        "Set the terminal or runtime status of one persistent task. "
        "Use status='completed' only for successful completion. "
        "Use status='failed' when the task cannot be completed under current constraints. "
        "Failed status requires a reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "blocked", "failed", "completed", "deleted"],
            },
            "reason": {
                "type": "string",
                "description": (
                    "Required when status='failed'. "
                    "Explain the failed todo, failed tool, root cause, and whether retry was considered."
                ),
            },
            "cascade_failed": {
                "type": "boolean",
                "description": (
                    "When status='failed', set true to mark downstream tasks that depend on this task as failed too. "
                    "Use this only when the failed task is critical for its blocked tasks."
                ),
            },
        },
        "required": ["id", "status"],
    },
},
{
    "name": "task_set_workspace",
    "description": "Set the YOLO workspace root for a task. Required before dispatching. The path must exist and be a directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "path": {"type": "string"},
        },
        "required": ["task_id", "path"],
    },
},
{
    "name": "task_set_todos",
    "description": (
        "Set todos for a task. Todos must be executable, verifiable, and safe. "
        "Multi-file tasks must start with target resolution. "
        "Every file target mentioned in the task subject or description must be explicitly covered. "
        "Invalid todo plans are rejected by validation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "failed", "blocked", "skipped"],
                        },
                        "activeForm": {"type": "string"},
                    },
                    "required": ["content", "status", "activeForm"],
                },
            },
        },
        "required": ["task_id", "todos"],
    },
},
{
    "name": "background_run",
    "description": (
        "Run a long command in a background runtime slot. "
        "Returns immediately with a background task id. "
        "Use this for slow tests, builds, installs, or checks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Default is 300.",
            },
        },
        "required": ["command"],
    },
},
{
    "name": "background_check",
    "description": "Check one background runtime task by id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
        },
        "required": ["id"],
    },
},
{
    "name": "background_list",
    "description": "List background runtime tasks.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},
{
    "name": "cron_create",
    "description": "Schedule a recurring or one-shot prompt with a 5-field cron expression.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field cron expression: 'min hour dom month dow'",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to inject when the schedule fires",
            },
            "recurring": {
                "type": "boolean",
                "description": "true=repeat, false=fire once then delete. Default true.",
            },
            "durable": {
                "type": "boolean",
                "description": "true=persist to disk, false=session-only. Default false.",
            },
        },
        "required": ["cron", "prompt"],
    },
},
{
    "name": "cron_delete",
    "description": "Delete a scheduled prompt by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
        },
        "required": ["id"],
    },
},
{
    "name": "cron_list",
    "description": "List scheduled prompts.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},{
    "name": "schedule_after",
    "description": (
        "Schedule a one-shot prompt after a relative delay in seconds. "
        "Use this for reminders like 'in 2 minutes' or 'after 10 minutes'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delay_seconds": {
                "type": "integer",
                "description": "Relative delay in seconds before the prompt fires",
            },
            "prompt": {
                "type": "string",
                "description": "The prompt to inject when the delay elapses",
            },
            "durable": {
                "type": "boolean",
                "description": "true=persist to disk, false=session-only. Default false.",
            },
        },
        "required": ["delay_seconds", "prompt"],
    },

},
{
    "name": "team_init",
    "description": "Initialize the fixed migration team if missing.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},
{
    "name": "list_teammates",
    "description": "List fixed teammates with role, profile, and status.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},

{
    "name": "shutdown_teammate",
    "description": "Request shutdown for an existing teammate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": ["engineer", "reviewer", "experiment_runner"],
            },
        },
        "required": ["name"],
    },
},

{
    "name": "read_inbox",
    "description": "Read and drain lead's team inbox.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
},
{
    "name": "broadcast",
    "description": "Broadcast a message from lead to all active teammates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "msg_type": {
                "type": "string",
                "enum": ["broadcast", "message"],
            },
        },
        "required": ["content"],
    },
},
{
    "name": "dispatch_to_teammate",
    "description": (
        "Create one tracked assignment request, send it from lead to a fixed teammate, "
        "attach a request_id, and wake that teammate if needed. "
        "Use this for normal teammate task dispatch. "
        "Include task_id to send the task and its todos to the teammate. "
        "Do not redispatch an already assigned task unless allow_retry=true and the user explicitly asked to retry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "enum": ["engineer", "reviewer", "experiment_runner"],
            },
            "content": {"type": "string"},
            "task_id": {
                "type": "integer",
                "description": "Task ID to dispatch. The task's todos will be sent with it.",
            },
            "allow_retry": {
                "type": "boolean",
                "description": "Only true when the user explicitly asks to retry the same task.",
            },
        },
        "required": ["to"],
    },
},
{
    "name": "request_list",
    "description": "List tracked team protocol requests.",
    "input_schema": {
        "type": "object",
        "properties": {
            "include_terminal": {"type": "boolean"},
            "limit": {"type": "integer"},
        },
        "required": [],
    },
},
{
    "name": "request_get",
    "description": "Get one tracked team protocol request by request_id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
        },
        "required": ["request_id"],
    },
},
{
    "name": "request_cancel",
    "description": "Cancel one open tracked request before retrying or changing direction.",
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["request_id", "reason"],
    },
},
] 
PROMPT_BUILDER = SystemPromptBuilder(
    workdir=Path(WORKDIR),
    tools=TOOLS,
    model_name=MODEL,
    skill_registry=SKILL_REGISTRY,
    memory_store=memory_store,
)

def _is_retryable_model_error(e: Exception) -> bool:
    status_code = getattr(e, "status_code", None)
    name = e.__class__.__name__
    return status_code in {429, 500, 502, 503, 504} or name in {"RateLimitError", "InternalServerError"}


def _retry_delay_for_model_error(e: Exception, attempt: int) -> float:
    headers = getattr(e, "headers", {}) or {}
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except Exception:
            pass
    status_code = getattr(e, "status_code", None)
    name = e.__class__.__name__
    if status_code == 429 or name == "RateLimitError":
        return min(75.0, 20.0 * attempt + random.uniform(0.0, 3.0))
    return min(30.0, MODEL_RETRY_DELAY_SECONDS * attempt + random.uniform(0.0, 1.0))


def create_model_response(*, system: str, messages: list, tools: list, max_tokens: int):
    last_error = None

    for attempt in range(1, MODEL_REQUEST_RETRIES + 1):
        try:
            with MODEL_CALL_LOCK:
                return client.messages.create(
                    model=MODEL,
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                )
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            body = getattr(e, "body", None) or getattr(e, "message", "") or str(e)[:500]
            print(f"[API error] status={status_code} type={e.__class__.__name__} body={body}")

            if not _is_retryable_model_error(e):
                raise

            last_error = e
            if attempt >= MODEL_REQUEST_RETRIES:
                print(f"Model request failed after {attempt} attempts: {e}")
                return None

            delay = _retry_delay_for_model_error(e, attempt)
            print(
                f"[Model retry] status={status_code} type={e.__class__.__name__}; "
                f"sleep {delay:.1f}s ({attempt}/{MODEL_REQUEST_RETRIES})"
            )
            time.sleep(delay)

    if last_error:
        print(f"Model request failed: {last_error}")
    return None


def lead_after_tool_batch(executions, tool_results):
    used_todo = any(execution.tool_name == "todo" for execution in executions)

    if used_todo:
        TODO_STATE["rounds_since_update"] = 0
        return tool_results

    todo_note_round_without_update()
    reminder = todo_reminder()

    if reminder:
        return [{"type": "text", "text": reminder}] + tool_results

    return tool_results

def init_team_manager():
    global TEAM_MANAGER

    TEAM_MANAGER = TeammateManager(
        team_dir=TEAM_DIR,
        bus=TEAM_BUS,
        request_store=REQUEST_STORE,
        client=client,
        model=MODEL,
        workdir=Path(WORKDIR),
        tool_profiles=TEAM_TOOL_PROFILES,
        hooks=HOOKS,
        create_model_response_fn=create_model_response,
        normalize_messages_fn=normalize_messages,
        choose_recovery_fn=choose_recovery,
        new_recovery_state_fn=new_recovery_state,
        can_attempt_fn=can_attempt,
        record_attempt_fn=record_attempt,
        apply_continue_recovery_fn=apply_continue_recovery,
        apply_compact_recovery_fn=apply_compact_recovery,
        apply_backoff_recovery_fn=apply_backoff_recovery,
        compact_fn=auto_compact_for_recovery,

    )

    TEAM_MANAGER.ensure_default_team()

    team_recovery = TeamRecovery(team_dir=TEAM_DIR)
    recovery_report = team_recovery.recover()
    print(recovery_report.render())

    TEAM_MANAGER.reload_config()

def sync_requests_from_lead_inbox(inbox: list[dict]) -> None:
    for message in inbox:
        request_id = message.get("request_id")
        if not request_id:
            continue

        msg_type = message.get("type", "")
        content = message.get("content", "")

        payload = {
            "from": message.get("from", ""),
            "message": message,
        }

        try:
            if msg_type == "task_result":
                REQUEST_STORE.complete_request(
                    request_id=request_id,
                    result=content,
                    payload=payload,
                )

            elif msg_type == "error":
                REQUEST_STORE.fail_request(
                    request_id=request_id,
                    reason=content or "Teammate reported an error.",
                    payload=payload,
                )

            elif msg_type == "rejected":
                REQUEST_STORE.reject_request(
                    request_id=request_id,
                    reason=content or "Teammate rejected the request.",
                    payload=payload,
                )

            elif msg_type == "expired":
                REQUEST_STORE.expire_request(
                    request_id=request_id,
                    reason=content or "Request expired.",
                    payload=payload,
                )

        except ValueError as e:
            print(f"[RequestStore] {e}")

# -- terminal report --------------------------------------------------
# agent_loop 退出前必须用自然语言向用户汇报。此状态由 task_final_report 工具写入。

FINAL_REPORT_STATE = {"reported": False, "summary": ""}


def _build_terminal_report_reminder() -> str:
    """当所有 task 已 terminal 但模型还没做最终汇报时，注入提醒。"""
    lines = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lines.append(f"  #{t['id']} [{t.get('status', '?')}] {t.get('subject', '')}")
    block = "\n".join(lines) if lines else "(no tasks)"
    return (
        "<system-reminder>\n"
        "All tasks have reached a terminal state (completed / failed / deleted).\n"
        "You MUST give the user a short natural-language summary of what happened.\n"
        "Then call task_final_report with the summary string.\n\n"
        f"Current task state:\n{block}\n"
        "</system-reminder>"
    )


def _last_assistant_has_text(messages: list) -> bool:
    """最近一条 assistant 消息是否包含自然语言文本。"""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            return bool(content.strip())
        for b in content if isinstance(content, list) else []:
            text = b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
            if text.strip():
                return True
    return False


def _append_fallback_terminal_report(messages: list) -> None:
    """
    兜底：二次提醒后模型仍未产出文本时，用 FINAL_REPORT_STATE 或任务摘要
    作为 assistant 消息插入，避免静默退出。
    """
    summary = FINAL_REPORT_STATE.get("summary", "").strip()
    if not summary:
        tasks: list[dict] = []
        for f in TASKS_DIR.glob("task_*.json"):
            try:
                tasks.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        done = [t for t in tasks if t.get("status") == "completed"]
        failed = [t for t in tasks if t.get("status") == "failed"]
        parts = []
        if done:
            parts.append(f"{len(done)} task(s) completed")
        if failed:
            parts.append(f"{len(failed)} task(s) failed")
        summary = "Work finished. " + (", ".join(parts) if parts else "No task changes.")
    if not _last_assistant_has_text(messages):
        messages.append({"role": "assistant", "content": summary})


def _has_active_tasks() -> bool:
    """
    当前执行图只允许在 task 进入 terminal 状态后结束。

    terminal:
    - completed：成功终止
    - failed：失败终止
    - deleted：删除/忽略

    non-terminal:
    - pending：已进入当前执行图，但尚未调度
    - in_progress：正在执行
    - blocked：当前先视为未收束，需要 leader 决策 failed / retry / ask user
    """
    for f in TASKS_DIR.glob("task_*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            if t.get("status") in ("pending", "in_progress", "blocked"):
                return True
        except Exception:
            pass
    return False


def agent_loop(messages: list):
    # 每次用户输入都会重新进入 agent_loop，重置汇报状态。
    FINAL_REPORT_STATE["reported"] = False
    FINAL_REPORT_STATE["summary"] = ""
    terminal_report_attempted = False
    while True:
        notification_text = BACKGROUND_MANAGER.render_notifications()
        if notification_text:
            messages.append({
                "role": "user",
                "content": notification_text,
            })
        cron_notifications = CRON_SCHEDULER.drain_notifications()
        for note in cron_notifications:
            print(f"[Cron notification] {note[:100]}")
            messages.append({
                "role": "user",
                "content": note,
            })
        lead_inbox = read_and_sync_lead_inbox()
        if lead_inbox:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<team-inbox>\n"
                        f"{json.dumps(lead_inbox, ensure_ascii=False, indent=2)}\n"
                        "</team-inbox>"
                    ),
                }
            )
        event, _results = HOOKS.emit(
            "before_model_call",
            messages=messages,
        )

        messages = event.payload["messages"]

        runner = AgentRunner(
            AgentRunnerConfig(
                name="lead",
                tools=TOOLS,
                tool_handlers=TOOL_HANDLERS,
                hooks=HOOKS,
                system_prompt_fn=lambda: PROMPT_BUILDER.build(),

                create_model_response_fn=create_model_response,
                normalize_messages_fn=normalize_messages,
                choose_recovery_fn=choose_recovery,
                new_recovery_state_fn=new_recovery_state,
                can_attempt_fn=can_attempt,
                record_attempt_fn=record_attempt,
                apply_continue_recovery_fn=apply_continue_recovery,
                apply_compact_recovery_fn=apply_compact_recovery,
                apply_backoff_recovery_fn=apply_backoff_recovery,
                compact_fn=auto_compact_for_recovery,

                max_tokens=2500,
                prompt_dirty_fn=maybe_mark_prompt_dirty,
                log_fn=print,
                after_tool_batch_fn=lead_after_tool_batch,
            )
        )

        runner_result = runner.run(messages)
        messages[:] = runner_result.messages
        if not _has_active_tasks():
            # 所有 task 已 terminal 后，只有 task_final_report 才是退出凭证。
            if FINAL_REPORT_STATE["reported"]:
                # 如果 task_final_report 是作为工具调用完成的，最后一条 assistant 可能没有自然语言文本。
                # 这里把 summary 补成 assistant 文本，保证 CLI 能打印出来。
                if not _last_assistant_has_text(messages):
                    messages.append({
                        "role": "assistant",
                        "content": FINAL_REPORT_STATE["summary"],
                    })
                return

            if not terminal_report_attempted:
                terminal_report_attempted = True
                messages.append({
                    "role": "user",
                    "content": _build_terminal_report_reminder(),
                })
                continue

            _append_fallback_terminal_report(messages)
            return

        # 仍有非 terminal 任务，等待 teammate 消息再继续。
        TEAM_BUS.wait_for_lead(timeout=10)


def extract_text_blocks(content) -> list[str]:
    texts = []

    if isinstance(content, str):
        if content.strip():
            texts.append(content)
        return texts

    if isinstance(content, list):
        for block in content:
            # SDK block object
            block_text = getattr(block, "text", None)
            if block_text:
                texts.append(block_text)
                continue

            # dict-style block
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)

    return texts


def print_last_assistant_text(history: list) -> None:
    """
    Print the most recent assistant natural-language response.
    """
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue

        texts = extract_text_blocks(msg.get("content"))
        if texts:
            print("\n".join(texts))
            return





if __name__ == "__main__":
    init_team_manager()
    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit"):
            break
        if query.strip().lower()  == "":
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print_last_assistant_text(history)
        print()
