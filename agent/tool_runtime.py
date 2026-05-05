from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


ToolHandler = Callable[..., Any]

ToolExecutionStatus = Literal[
    "executed",
    "denied",
    "unknown_tool",
    "tool_error",
]


@dataclass
class ToolExecutionResult:
    """
    Normalized result for one tool_use block.

    tool_result:
        The message block appended back to the conversation.

    used_todo:
        Whether this tool call used the todo tool.

    output_preview:
        Short printable preview for CLI logging.

    status:
        Runtime-facing execution status.

    reason:
        Runtime-facing reason for denied, unknown_tool, or tool_error.
    """

    tool_result: dict[str, Any]
    used_todo: bool
    output_preview: str
    status: ToolExecutionStatus
    reason: str = ""
    tool_name: str = ""


def execute_tool_block(
    *,
    block: Any,
    messages: list[dict[str, Any]],
    tool_handlers: dict[str, ToolHandler],
    hooks: Any,
    prompt_dirty_fn: Callable[[str, str], None] | None = None,
    preview_limit: int = 200,
    tool_actor: str | None = None,
) -> ToolExecutionResult:
    """
    Execute one tool_use block through the shared tool runtime.

    This runtime supports allow / deny only.

    It centralizes:
    - before_tool_execute hooks
    - deny handling
    - handler dispatch
    - tool exception capture
    - after_tool_execute hooks
    - output compression metadata
    - normalized tool_result construction
    """

    tool_name = block.name
    tool_input = block.input or {}

    _event, results_before = hooks.emit(
        "before_tool_execute",
        tool_name=tool_name,
        tool_input=tool_input,
        messages=messages,
    )

    stop = hooks.first_stopping_result(results_before)

    if stop is not None:
        output, meta, status, reason = _build_denied_output(
            tool_name=tool_name,
            stop=stop,
        )
    else:
        output, meta, status, reason = _execute_allowed_tool(
            tool_name=tool_name,
            tool_input=tool_input,
            messages=messages,
            tool_handlers=tool_handlers,
            hooks=hooks,
            prompt_dirty_fn=prompt_dirty_fn,
            tool_actor=tool_actor,
        )

    output = str(output)

    tool_result = {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": output,
        "_tool_name": tool_name,
        "_compression_role": meta["role"],
        "_digest_summary": meta["summary"],
        "_saved_to": meta["saved_to"],
        "_externalized": meta["externalized"],
        "_execution_status": status,
        "_execution_reason": reason,
    }

    return ToolExecutionResult(
        tool_result=tool_result,
        used_todo=(tool_name == "todo"),
        output_preview=output[:preview_limit],
        status=status,
        reason=reason,
        tool_name=tool_name,
    )


def _build_denied_output(
    *,
    tool_name: str,
    stop: Any,
) -> tuple[str, dict[str, Any], ToolExecutionStatus, str]:
    """
    Convert any hook stop into a deny result.

    Current policy intentionally has no ask mode.
    """

    reason = getattr(stop, "reason", "") or "No reason provided."
    output = f"Permission denied: tool={tool_name}, reason={reason}"
    meta = _control_meta(summary=reason)

    return output, meta, "denied", reason


def _execute_allowed_tool(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_handlers: dict[str, ToolHandler],
    hooks: Any,
    prompt_dirty_fn: Callable[[str, str], None] | None,
    tool_actor: str | None,
) -> tuple[str, dict[str, Any], ToolExecutionStatus, str]:
    """
    Execute a tool handler and run after_tool_execute hooks.
    """

    handler = tool_handlers.get(tool_name)

    if handler is None:
        raw_output = f"Unknown tool: {tool_name}"
        status: ToolExecutionStatus = "unknown_tool"
        reason = raw_output
    else:
        try:
            if tool_actor is None:
                raw_output = handler(**tool_input)
            else:
                raw_output = handler(tool_actor, **tool_input)

            status = "executed"
            reason = ""
        except Exception as e:
            raw_output = f"Error: {e}"
            status = "tool_error"
            reason = str(e)

    raw_output = str(raw_output)

    if prompt_dirty_fn is not None:
        prompt_dirty_fn(tool_name, raw_output)

    event_after, _results_after = hooks.emit(
        "after_tool_execute",
        tool_name=tool_name,
        tool_input=tool_input,
        raw_output=raw_output,
        messages=messages,
    )

    output = event_after.payload.get("output", raw_output)
    meta = event_after.payload.get("output_meta", _raw_meta())

    return str(output), _normalize_meta(meta), status, reason


def _control_meta(*, summary: str) -> dict[str, Any]:
    return {
        "role": "control",
        "externalized": False,
        "summary": summary,
        "saved_to": "",
    }


def _raw_meta() -> dict[str, Any]:
    return {
        "role": "raw",
        "externalized": False,
        "summary": "",
        "saved_to": "",
    }


def _normalize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure all expected metadata keys exist.
    """

    normalized = _raw_meta()
    normalized.update(meta or {})

    normalized["role"] = str(normalized.get("role", "raw"))
    normalized["summary"] = str(normalized.get("summary", ""))
    normalized["saved_to"] = str(normalized.get("saved_to", ""))
    normalized["externalized"] = bool(normalized.get("externalized", False))

    return normalized