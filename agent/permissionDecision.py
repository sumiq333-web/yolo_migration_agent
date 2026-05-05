from typing import Literal, TypedDict

Behavior = Literal["allow", "deny", "ask"]


class PermissionDecision(TypedDict):
    behavior: Behavior
    reason: str


READ_ONLY_TOOLS = {
    "read_file",
    "read_code",
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
    "task_set_status",
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
    "task",
}

DENIED_TOOLS = {
    "bash",
}

LOW_RISK_BACKGROUND_PREFIXES = (
    "python -c",
    "python -m pytest",
    "pytest",
    "python -m unittest",
)



def check_permission(tool_name: str, tool_input: dict) -> PermissionDecision:
    """
    Minimal permission gate.

    Rules:
    1. Explicitly denied tools -> deny
    2. Read-only / low-risk query tools -> allow
    3. Low-risk agent state tools -> allow
    4. Mutating user files / execution flow -> ask
    5. Unknown tools -> ask
    """
    if tool_name in DENIED_TOOLS:
        return {
            "behavior": "deny",
            "reason": f"tool '{tool_name}' is disabled",
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