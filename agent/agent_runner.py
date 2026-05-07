from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from agent.model_runtime import call_model_with_recovery
from agent.tool_runtime import execute_tool_block, ToolExecutionResult


@dataclass
class AgentRunnerConfig:
    """
    Shared runtime configuration for one agent.

    Different agents can provide different:
    - system prompt
    - tools
    - tool handlers
    - stop policy
    - actor name

    But they share:
    - model recovery
    - tool execution
    - normalized tool_result format
    """

    name: str
    tools: list[dict]
    tool_handlers: dict[str, Callable[..., Any]]
    hooks: Any

    system_prompt_fn: Callable[[], str]

    create_model_response_fn: Callable[..., Any]
    normalize_messages_fn: Callable[[list[dict]], list[dict]]
    choose_recovery_fn: Callable[[str | None, str | None], Any]
    new_recovery_state_fn: Callable[[], Any]
    can_attempt_fn: Callable[[Any, str], bool]
    record_attempt_fn: Callable[[Any, str], None]
    apply_continue_recovery_fn: Callable[[list[dict]], list[dict]]
    apply_compact_recovery_fn: Callable[[list[dict], Callable[[list], list]], list[dict]]
    apply_backoff_recovery_fn: Callable[[Any], None]
    compact_fn: Callable[[list], list]

    max_tokens: int = 8000
    tool_actor: str | None = None
    prompt_dirty_fn: Callable[[str, str], None] | None = None
    log_fn: Callable[[str], None] = print

    stop_after_tool_fn: Callable[[list[ToolExecutionResult]], bool] | None = None
    on_text_response_fn: Callable[[str], None] | None = None
    on_tool_errors_fn: Callable[[list[ToolExecutionResult]], None] | None = None
    after_tool_batch_fn: Callable[[list[ToolExecutionResult], list[dict]], list[dict]] | None = None


@dataclass
class AgentRunnerResult:
    """
    单次 AgentRunner.run 的返回结果。

    注意：
    AgentRunner 是 lead / teammate 共用运行器。
    它不能判断 task 是否 terminal，也不能决定是否补最终用户报告。
    它只暴露 text_response / has_text_response 给外层 loop 使用。
    """
    messages: list[dict]
    stop_reason: str
    response: Any | None = None
    tool_executions: list[ToolExecutionResult] = field(default_factory=list)

    # 本轮模型产生的自然语言文本。工具调用轮通常为空。
    text_response: str = ""

    @property
    def has_text_response(self) -> bool:
        """本轮是否产生了可展示/可转发的自然语言文本。"""
        return bool(self.text_response.strip())


class AgentRunner:
    """
    Shared model/tool loop for lead and teammate agents.

    This class does not own:
    - inbox draining
    - background notifications
    - cron notifications
    - task graph policy
    - todo reminder policy

    Those belong to the caller.
    """

    def __init__(self, config: AgentRunnerConfig):
        self.config = config

    def run(self, messages: list[dict]) -> AgentRunnerResult:
        recovery_state = self.config.new_recovery_state_fn()
        all_tool_executions: list[ToolExecutionResult] = []

        while True:
            system_prompt = self.config.system_prompt_fn()

            runtime_result = call_model_with_recovery(
                system=system_prompt,
                messages=messages,
                tools=self.config.tools,
                max_tokens=self.config.max_tokens,
                create_model_response_fn=self.config.create_model_response_fn,
                normalize_messages_fn=self.config.normalize_messages_fn,
                choose_recovery_fn=self.config.choose_recovery_fn,
                recovery_state=recovery_state,
                can_attempt_fn=self.config.can_attempt_fn,
                record_attempt_fn=self.config.record_attempt_fn,
                apply_continue_recovery_fn=self.config.apply_continue_recovery_fn,
                apply_compact_recovery_fn=self.config.apply_compact_recovery_fn,
                apply_backoff_recovery_fn=self.config.apply_backoff_recovery_fn,
                compact_fn=self.config.compact_fn,
                log_fn=lambda text: self.config.log_fn(f"[{self.config.name}] {text}"),
            )

            messages = runtime_result.messages

            if runtime_result.status == "retry":
                continue

            if runtime_result.status == "done":
                return AgentRunnerResult(
                    messages=messages,
                    stop_reason=runtime_result.reason or "done",
                    response=None,
                    tool_executions=all_tool_executions,
                )

            response = runtime_result.response
            if response is None:
                return AgentRunnerResult(
                    messages=messages,
                    stop_reason="empty_response",
                    response=None,
                    tool_executions=all_tool_executions,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                }
            )

            if response.stop_reason != "tool_use":
                text = self._extract_text(response.content).strip()

                if text and self.config.on_text_response_fn is not None:
                    self.config.on_text_response_fn(text)

                return AgentRunnerResult(
                    messages=messages,
                    stop_reason=response.stop_reason or "text_response",
                    response=response,
                    tool_executions=all_tool_executions,
                    text_response=text,
                )

            tool_results = []
            batch_executions: list[ToolExecutionResult] = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                execution = execute_tool_block(
                    block=block,
                    messages=messages,
                    tool_handlers=self.config.tool_handlers,
                    hooks=self.config.hooks,
                    prompt_dirty_fn=self.config.prompt_dirty_fn,
                    tool_actor=self.config.tool_actor,
                )

                self.config.log_fn(
                    f"[{self.config.name}] {block.name}: {execution.output_preview}"
                )

                tool_results.append(execution.tool_result)
                batch_executions.append(execution)
                all_tool_executions.append(execution)

            if self.config.on_tool_errors_fn is not None:
                error_executions = [
                    execution
                    for execution in batch_executions
                    if execution.status in {"denied", "unknown_tool", "tool_error"}
                ]
                if error_executions:
                    self.config.on_tool_errors_fn(error_executions)

            if self.config.after_tool_batch_fn is not None:
                tool_results = self.config.after_tool_batch_fn(
                    batch_executions,
                    tool_results,
                )

            messages.append(
                {
                    "role": "user",
                    "content": tool_results,
                }
            )

            if self.config.stop_after_tool_fn is not None:
                if self.config.stop_after_tool_fn(batch_executions):
                    return AgentRunnerResult(
                        messages=messages,
                        stop_reason="stopped_after_tool",
                        response=response,
                        tool_executions=all_tool_executions,
                    )

    def _extract_text(self, content: Any) -> str:
        parts: list[str] = []

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
                    continue

                if isinstance(block, dict) and block.get("type") == "text":
                    block_text = block.get("text", "")
                    if block_text:
                        parts.append(block_text)

        return "\n".join(parts)