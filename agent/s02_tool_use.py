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
from agent.skillRegistry import SkillRegistry
from agent.subagent import run_subagent
from agent.state import WORKDIR, WORKSPACE_ROOT, TODO_STATE
from agent.workspace import client, MODEL
from tools.common_tool import (
    todo_note_round_without_update,
    todo_reminder,
    run_bash,
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
MODEL_REQUEST_RETRIES = 3
MODEL_RETRY_DELAY_SECONDS = 2
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
        task_manager=TASK_MANAGER,
    ),
    "reviewer": build_reviewer_profile(
        bus=TEAM_BUS,
        read_file_fn=run_read,
        read_code_fn=run_read_code,
        write_file_fn=run_write,
        edit_file_fn=run_edit,
        scan_yolo_fn=scan_yolo_project,
        task_manager=TASK_MANAGER,
    ),
    "experiment_runner": build_experiment_runner_profile(
        bus=TEAM_BUS,
        background_run_fn=BACKGROUND_MANAGER.run,
        background_check_fn=BACKGROUND_MANAGER.check,
        background_list_fn=lambda: BACKGROUND_MANAGER.check(),
        write_file_fn=run_write,
        edit_file_fn=run_edit,
        task_manager=TASK_MANAGER,
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
# -- The dispatch map: {tool_name: handler} --
def _handle_dispatch(kw: dict) -> str:
    """Dispatch a task to a teammate. If task_id is set, assign owner + in_progress first."""
    task_id = kw.get("task_id")
    to = kw["to"]
    if task_id is not None:
        try:
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
    content = kw["content"]
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
        "todos": task.get("todos", []),
    }
    return (
        f"<task-context>\n"
        f"{json.dumps(header, ensure_ascii=False, indent=2)}\n"
        f"</task-context>\n\n"
        f"{content}"
    )


def auto_compact_for_recovery(messages: list) -> list:
    compacted_messages, _changed = micro_compact_messages(messages)
    return compacted_messages

TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
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
    "task_set_status": lambda **kw: TASK_MANAGER.set_status(
        kw["id"],
        kw["status"],
    ),
    "task_set_todos": lambda **kw: json.dumps(
        TASK_MANAGER.set_todos(kw["task_id"], kw["todos"]),
        ensure_ascii=False, indent=2,
    ),
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
    "schedule_after": lambda **kw: CRON_SCHEDULER.create_after(
        kw["delay_seconds"],
        kw["prompt"],
        kw.get("durable", False),
    ),
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
}
STABLE_PROMPT_MUTATION_TOOLS = {
    "save_memory",
    "delete_memory",
    # 以后如果你支持动态增删 skill、改 instruction 文件，也加进来
}
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
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
    "name": "task_set_status",
    "description": "Set the status of one persistent task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
            },
        },
        "required": ["id", "status"],
    },
},
{
    "name": "task_set_todos",
    "description": (
        "Set the todo list for a task. "
        "Use this before dispatching a task to a teammate. "
        "Each todo item needs content, status (pending/in_progress/completed), and activeForm."
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
                            "enum": ["pending", "in_progress", "completed"],
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
        "Include task_id to send the task and its todos to the teammate."
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
        },
        "required": ["to", "content"],
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
] 
PROMPT_BUILDER = SystemPromptBuilder(
    workdir=Path(WORKDIR),
    tools=TOOLS,
    model_name=MODEL,
    skill_registry=SKILL_REGISTRY,
    memory_store=memory_store,
)

def create_model_response(*, system: str, messages: list, tools: list, max_tokens: int):
    last_error = None

    for attempt in range(1, MODEL_REQUEST_RETRIES + 1):
        try:
            return client.messages.create(
                model=MODEL,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            is_internal_server_error = (
                status_code == 500
                or e.__class__.__name__ == "InternalServerError"
            )

            if not is_internal_server_error:
                raise

            last_error = e
            if attempt >= MODEL_REQUEST_RETRIES:
                print(f"Model request failed after {attempt} attempts: {e}")
                return None

            delay = MODEL_RETRY_DELAY_SECONDS * attempt
            print(
                f"Model request failed with 500, retrying in {delay}s "
                f"({attempt}/{MODEL_REQUEST_RETRIES})"
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

def _has_active_tasks() -> bool:
    for f in TASKS_DIR.glob("task_*.json"):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            if t.get("status") in ("pending", "in_progress"):
                return True
        except Exception:
            pass
    return False


def agent_loop(messages: list):
    recovery_state = new_recovery_state()
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

                max_tokens=8000,
                prompt_dirty_fn=maybe_mark_prompt_dirty,
                log_fn=print,
                after_tool_batch_fn=lead_after_tool_batch,
            )
        )

        runner_result = runner.run(messages)
        messages[:] = runner_result.messages

        if not _has_active_tasks():
            return
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
