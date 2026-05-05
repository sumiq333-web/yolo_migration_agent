from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal


HookEventName = Literal[
    "before_model_call",
    "after_model_response",
    "before_tool_execute",
    "after_tool_execute",
    "before_compact",
]


@dataclass
class HookEvent:
    """
    Runtime event dispatched by the HookManager.
    """
    name: HookEventName
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """
    Generic hook return object.

    Semantics:
    - continue_: whether the pipeline should continue
    - stop_reason: optional explanation if a hook short-circuits execution
    - decision: e.g. allow / deny / ask for permission-like hooks
    - output_override: replace tool output or other computed output
    - payload_updates: merge into event payload / runtime state if desired
    - messages_override: replace messages list in compact / preprocess hooks
    """
    continue_: bool = True
    stop_reason: str = ""
    decision: str | None = None
    reason: str = ""
    output_override: Any = None
    payload_updates: dict[str, Any] = field(default_factory=dict)
    messages_override: list | None = None

    @classmethod
    def noop(cls) -> "HookResult":
        return cls()

    @classmethod
    def stop(cls, reason: str, decision: str | None = None) -> "HookResult":
        return cls(
            continue_=False,
            stop_reason=reason,
            decision=decision,
            reason=reason,
        )


@dataclass(order=True)
class RegisteredHook:
    """
    Internal registry item.
    """
    priority: int
    name: str = field(compare=False)
    event_name: HookEventName = field(compare=False)
    handler: Callable[[HookEvent], HookResult | None] = field(compare=False)
    short_circuit_on_stop: bool = field(default=True, compare=False)
    enabled: bool = field(default=True, compare=False)


class HookManager:
    """
    Central event-driven control layer.

    Responsibilities:
    - register hooks by event
    - execute hooks in priority order
    - merge payload updates
    - optionally short-circuit on stop
    - keep event-specific control logic out of the main agent loop
    """

    def __init__(self) -> None:
        self._registry: dict[HookEventName, list[RegisteredHook]] = {
            "before_model_call": [],
            "after_model_response": [],
            "before_tool_execute": [],
            "after_tool_execute": [],
            "before_compact": [],
        }

    # ---------- registration ----------
    def register(
        self,
        *,
        event_name: HookEventName,
        name: str,
        handler: Callable[[HookEvent], HookResult | None],
        priority: int = 100,
        short_circuit_on_stop: bool = True,
        enabled: bool = True,
    ) -> None:
        item = RegisteredHook(
            priority=priority,
            name=name,
            event_name=event_name,
            handler=handler,
            short_circuit_on_stop=short_circuit_on_stop,
            enabled=enabled,
        )
        self._registry[event_name].append(item)
        self._registry[event_name].sort()

    def unregister(self, event_name: HookEventName, name: str) -> None:
        self._registry[event_name] = [
            item for item in self._registry[event_name] if item.name != name
        ]

    def enable(self, event_name: HookEventName, name: str) -> None:
        for item in self._registry[event_name]:
            if item.name == name:
                item.enabled = True
                return

    def disable(self, event_name: HookEventName, name: str) -> None:
        for item in self._registry[event_name]:
            if item.name == name:
                item.enabled = False
                return

    def list_hooks(self, event_name: HookEventName | None = None) -> list[dict[str, Any]]:
        if event_name is None:
            out = []
            for name, items in self._registry.items():
                for item in items:
                    out.append({
                        "event_name": name,
                        "name": item.name,
                        "priority": item.priority,
                        "enabled": item.enabled,
                        "short_circuit_on_stop": item.short_circuit_on_stop,
                    })
            return out

        return [
            {
                "event_name": event_name,
                "name": item.name,
                "priority": item.priority,
                "enabled": item.enabled,
                "short_circuit_on_stop": item.short_circuit_on_stop,
            }
            for item in self._registry[event_name]
        ]

    # ---------- dispatch ----------
    def emit(self, event_name: HookEventName, **payload: Any) -> tuple[HookEvent, list[HookResult]]:
        """
        Run hooks for one event.

        Returns:
        - possibly mutated HookEvent
        - all HookResults returned before termination (or completion)
        """
        event = HookEvent(name=event_name, payload=dict(payload))
        results: list[HookResult] = []

        for item in self._registry[event_name]:
            if not item.enabled:
                continue

            result = item.handler(event)
            if result is None:
                result = HookResult.noop()

            results.append(result)

            # merge payload updates
            if result.payload_updates:
                event.payload.update(result.payload_updates)

            # replace messages if relevant
            if result.messages_override is not None:
                event.payload["messages"] = result.messages_override

            # override output if relevant
            if result.output_override is not None:
                event.payload["output"] = result.output_override

            # short-circuit
            if not result.continue_ and item.short_circuit_on_stop:
                break

        return event, results

    # ---------- convenience helpers ----------
    def first_stopping_result(self, results: list[HookResult]) -> HookResult | None:
        for result in results:
            if not result.continue_:
                return result
        return None

    def last_decision(self, results: list[HookResult]) -> str | None:
        last = None
        for result in results:
            if result.decision is not None:
                last = result.decision
        return last

from agent.permissionDecision import check_permission


def permission_hook(event: HookEvent) -> HookResult:
    if event.name != "before_tool_execute":
        return HookResult.noop()

    tool_name = event.payload["tool_name"]
    tool_input = event.payload["tool_input"]

    decision = check_permission(tool_name, tool_input)

    behavior = decision["behavior"]
    reason = decision["reason"]

    if behavior == "allow":
        return HookResult(
            continue_=True,
            decision="allow",
            reason=reason,
        )

    if behavior == "deny":
        return HookResult.stop(reason=reason, decision="deny")

    # ask
    return HookResult.stop(reason=reason, decision="ask")

from tools.common_tool import prepare_tool_result

def tool_output_hook(event: HookEvent) -> HookResult:
    if event.name != "after_tool_execute":
        return HookResult.noop()

    tool_name = event.payload["tool_name"]
    tool_input = event.payload["tool_input"]
    raw_output = event.payload["raw_output"]

    output, meta = prepare_tool_result(tool_name, tool_input, raw_output)

    return HookResult(
        continue_=True,
        output_override=output,
        payload_updates={
            "output_meta": meta,
        },
    )
from tools.common_tool import micro_compact_messages


def micro_compact_hook(event: HookEvent) -> HookResult:
    if event.name != "before_model_call":
        return HookResult.noop()

    messages = event.payload["messages"]
    compacted_messages, changed = micro_compact_messages(messages)

    return HookResult(
        continue_=True,
        messages_override=compacted_messages,
        payload_updates={
            "compacted": changed,
        },
    )