from typing import Literal, TypedDict

Behavior = Literal["allow", "deny", "ask"]


class PermissionDecision(TypedDict):
    behavior: Behavior
    reason: str


READ_ONLY_TOOLS = {
    "read_file",
    "read_code",
    "run_python",
    "load_skill",
    "todo",
    "set_yolo_workspace",
    "scan_yolo_project",
    "scan_paper",

    # s12 task graph query tools
    "task_get",
    "task_list",
    "task_ready",
    "background_check",
    "background_list",
    "cron_list",
    "list_teammates",
    "read_inbox",

    "request_list",
    "request_get",
}

LOW_RISK_STATE_TOOLS = {
    "task_create_graph",
    "task_final_report",
    "task_set_status",
    "task_set_workspace",
    "task_set_todos",
    "todo_update",
    "cron_create",
    "cron_delete",
    "schedule_after",
    "team_init",
    "wake_teammate",
    "shutdown_teammate",
    "send_message",
    "broadcast",
    "dispatch_to_teammate",
    "request_create_test",
    "request_complete_test",
}

MUTATING_TOOLS = {
    "write_file",
    "edit_file",
    "run_shell",
    "task",
}

DENIED_TOOLS: set[str] = set()

LOW_RISK_BACKGROUND_PREFIXES = (
    "python -c",
    "python -m pytest",
    "pytest",
    "python -m unittest",
)

WAIT_COMMAND_PATTERNS = (
    "timeout",
    "sleep",
    "ping -n",
    "start /wait",
    "wait for engineer",
    "wait for reviewer",
    "wait for teammate",
    "waiting for engineer",
    "waiting for teammate",
)

def _is_wait_only_shell_command(command: str) -> bool:
    cmd = str(command or "").strip().lower()
    return any(pattern in cmd for pattern in WAIT_COMMAND_PATTERNS)

def _is_wait_only_python_code(code: str) -> bool:
    """
    拦截只用于等待 teammate 的 Python 代码。

    这里不是禁止所有 time.sleep，而是拦截明显的等待型脚本。
    如果以后 run_python 被用于真实测试且包含 sleep，可以再细化白名单。
    """
    text = str(code or "").strip().lower()

    wait_patterns = (
        "time.sleep",
        "asyncio.sleep",
        "waiting for engineer",
        "waiting for teammate",
        "wait for engineer",
        "wait for teammate",
    )

    return any(pattern in text for pattern in wait_patterns)

def check_permission(tool_name: str, tool_input: dict) -> PermissionDecision:
    """
    Minimal permission gate.

    Rules:
    1. Explicitly denied tools -> deny
    2. Waiting commands are denied before generic allow rules
    3. Read-only / low-risk query tools -> allow
    4. Low-risk agent state tools -> allow
    5. Mutating user files / execution flow -> ask
    6. Unknown tools -> ask
    """
    if tool_name in DENIED_TOOLS:
        return {
            "behavior": "deny",
            "reason": f"tool '{tool_name}' is disabled",
        }

    # 必须放在 READ_ONLY_TOOLS 前面。
    # 否则 run_python 属于 READ_ONLY_TOOLS，会被直接 allow。
    if tool_name == "run_python":
        code = str(tool_input.get("code", ""))
        if _is_wait_only_python_code(code):
            return {
                "behavior": "deny",
                "reason": (
                    "Do not use Python sleep/wait code to wait for teammates. "
                    "The runtime team message loop handles teammate waiting."
                ),
            }

    # 必须放在 MUTATING_TOOLS 前面。
    # 否则 run_shell 会进入 ask，而不是直接 deny 等待命令。
    if tool_name == "run_shell":
        cmd = str(tool_input.get("command", "")).lower()
        if _is_wait_only_shell_command(cmd):
            return {
                "behavior": "deny",
                "reason": (
                    "Do not use shell commands to wait for teammates. "
                    "The runtime team message loop handles teammate waiting."
                ),
            }

    if tool_name in READ_ONLY_TOOLS:
        return {
            "behavior": "allow",
            "reason": f"tool '{tool_name}' is read-only or low-risk",
        }

    if tool_name in LOW_RISK_STATE_TOOLS:
        return {
            "behavior": "allow",
            "reason": f"tool '{tool_name}' only mutates agent control state",
        }

    if tool_name in MUTATING_TOOLS:
        return {
            "behavior": "ask",
            "reason": f"tool '{tool_name}' may modify files, state, or execution flow",
        }

    if tool_name == "background_run":
        command = str(tool_input.get("command", "")).strip().lower()

        if command.startswith(LOW_RISK_BACKGROUND_PREFIXES):
            return {
                "behavior": "allow",
                "reason": "background command is an allowed low-risk command",
            }

        return {
            "behavior": "deny",
            "reason": "background command is not in the allowed command list",
        }

    return {
        "behavior": "ask",
        "reason": f"tool '{tool_name}' has no explicit permission rule",
    }