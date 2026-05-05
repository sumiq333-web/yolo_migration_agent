from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


ModelRuntimeStatus = Literal[
    "response",
    "retry",
    "done",
]


@dataclass
class ModelRuntimeResult:
    """
    Result of one model-call attempt.

    status:
        response:
            A model response was successfully produced.

        retry:
            The caller should continue the outer agent loop and call the model again
            with the returned messages.

        done:
            The agent loop should stop.

    response:
        The model response, if status == "response".

    messages:
        The possibly modified messages list after recovery.

    reason:
        Human-readable runtime reason for logging.
    """

    status: ModelRuntimeStatus
    response: Any | None
    messages: list[dict]
    reason: str = ""


def call_model_with_recovery(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    create_model_response_fn: Callable[..., Any],
    normalize_messages_fn: Callable[[list[dict]], list[dict]],
    choose_recovery_fn: Callable[[str | None, str | None], Any],
    recovery_state: Any,
    can_attempt_fn: Callable[[Any, str], bool],
    record_attempt_fn: Callable[[Any, str], None],
    apply_continue_recovery_fn: Callable[[list[dict]], list[dict]],
    apply_compact_recovery_fn: Callable[[list[dict], Callable[[list], list]], list[dict]],
    apply_backoff_recovery_fn: Callable[[Any], None],
    compact_fn: Callable[[list], list],
    log_fn: Callable[[str], None] = print,
) -> ModelRuntimeResult:
    """
    Call the model once and apply model-level recovery decisions.

    This function does not execute tools.
    This function does not build system prompts.
    This function does not drain inboxes or external notifications.

    It only owns:
    - model call
    - normalize messages before call
    - choose recovery
    - continue recovery
    - compact recovery
    - backoff recovery
    - fail handling
    """

    try:
        response = create_model_response_fn(
            system=system,
            messages=normalize_messages_fn(messages),
            tools=tools,
            max_tokens=max_tokens,
        )

        if response is None:
            decision = choose_recovery_fn(None, "internal server error")
        else:
            decision = choose_recovery_fn(response.stop_reason, None)

    except Exception as e:
        response = None
        decision = choose_recovery_fn(None, str(e))

    if decision.kind == "continue":
        return _handle_continue(
            messages=messages,
            recovery_state=recovery_state,
            can_attempt_fn=can_attempt_fn,
            record_attempt_fn=record_attempt_fn,
            apply_continue_recovery_fn=apply_continue_recovery_fn,
            log_fn=log_fn,
        )

    if decision.kind == "compact":
        return _handle_compact(
            messages=messages,
            recovery_state=recovery_state,
            can_attempt_fn=can_attempt_fn,
            record_attempt_fn=record_attempt_fn,
            apply_compact_recovery_fn=apply_compact_recovery_fn,
            compact_fn=compact_fn,
            log_fn=log_fn,
        )

    if decision.kind == "backoff":
        return _handle_backoff(
            messages=messages,
            recovery_state=recovery_state,
            can_attempt_fn=can_attempt_fn,
            record_attempt_fn=record_attempt_fn,
            apply_backoff_recovery_fn=apply_backoff_recovery_fn,
            log_fn=log_fn,
        )

    if decision.kind == "fail":
        reason = getattr(decision, "reason", "") or "model recovery failed"
        log_fn(f"[Recovery] fail: {reason}")
        return ModelRuntimeResult(
            status="done",
            response=None,
            messages=messages,
            reason=reason,
        )

    if response is None:
        return ModelRuntimeResult(
            status="done",
            response=None,
            messages=messages,
            reason="model returned no response",
        )

    return ModelRuntimeResult(
        status="response",
        response=response,
        messages=messages,
        reason="model response received",
    )


def _handle_continue(
    *,
    messages: list[dict],
    recovery_state: Any,
    can_attempt_fn: Callable[[Any, str], bool],
    record_attempt_fn: Callable[[Any, str], None],
    apply_continue_recovery_fn: Callable[[list[dict]], list[dict]],
    log_fn: Callable[[str], None],
) -> ModelRuntimeResult:
    if not can_attempt_fn(recovery_state, "continue"):
        log_fn("[Recovery] continue exhausted")
        return ModelRuntimeResult(
            status="done",
            response=None,
            messages=messages,
            reason="continue exhausted",
        )

    record_attempt_fn(recovery_state, "continue")
    log_fn("[Recovery] continue")

    return ModelRuntimeResult(
        status="retry",
        response=None,
        messages=apply_continue_recovery_fn(messages),
        reason="continue recovery applied",
    )


def _handle_compact(
    *,
    messages: list[dict],
    recovery_state: Any,
    can_attempt_fn: Callable[[Any, str], bool],
    record_attempt_fn: Callable[[Any, str], None],
    apply_compact_recovery_fn: Callable[[list[dict], Callable[[list], list]], list[dict]],
    compact_fn: Callable[[list], list],
    log_fn: Callable[[str], None],
) -> ModelRuntimeResult:
    if not can_attempt_fn(recovery_state, "compact"):
        log_fn("[Recovery] compact exhausted")
        return ModelRuntimeResult(
            status="done",
            response=None,
            messages=messages,
            reason="compact exhausted",
        )

    record_attempt_fn(recovery_state, "compact")
    log_fn("[Recovery] compact")

    return ModelRuntimeResult(
        status="retry",
        response=None,
        messages=apply_compact_recovery_fn(messages, compact_fn),
        reason="compact recovery applied",
    )


def _handle_backoff(
    *,
    messages: list[dict],
    recovery_state: Any,
    can_attempt_fn: Callable[[Any, str], bool],
    record_attempt_fn: Callable[[Any, str], None],
    apply_backoff_recovery_fn: Callable[[Any], None],
    log_fn: Callable[[str], None],
) -> ModelRuntimeResult:
    if not can_attempt_fn(recovery_state, "backoff"):
        log_fn("[Recovery] backoff exhausted")
        return ModelRuntimeResult(
            status="done",
            response=None,
            messages=messages,
            reason="backoff exhausted",
        )

    record_attempt_fn(recovery_state, "backoff")
    apply_backoff_recovery_fn(recovery_state)

    return ModelRuntimeResult(
        status="retry",
        response=None,
        messages=messages,
        reason="backoff recovery applied",
    )