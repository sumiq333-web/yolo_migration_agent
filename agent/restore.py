from __future__ import annotations

import random
import time
from dataclasses import dataclass


RECOVERY_LIMITS = {
    "continue": 3,
    "compact": 2,
    "backoff": 3,
}


CONTINUE_MESSAGE = (
    "Output limit hit. Continue directly from where you stopped. "
    "Do not restart, do not repeat, do not re-summarize."
)


@dataclass
class RecoveryState:
    continuation_attempts: int = 0
    compact_attempts: int = 0
    transport_attempts: int = 0


@dataclass
class RecoveryDecision:
    kind: str           # continue | compact | backoff | fail | none
    reason: str = ""


def new_recovery_state() -> RecoveryState:
    return RecoveryState()


def choose_recovery(stop_reason: str | None, error_text: str | None) -> RecoveryDecision:
    """
    Decide how to recover from a model stop/error.

    Kinds:
    - continue: model hit max_tokens
    - compact: prompt/context too large
    - backoff: transient transport/server failure
    - fail: unrecoverable
    - none: normal success path
    """
    if stop_reason == "max_tokens":
        return RecoveryDecision("continue", "output truncated")

    text = (error_text or "").lower()

    if text:
        if (
            ("prompt" in text or "context" in text)
            and ("too long" in text or "length" in text or "limit" in text)
        ):
            return RecoveryDecision("compact", "context too large")

        transient_words = [
            "timeout",
            "timed out",
            "rate limit",
            "rate_limit",
            "unavailable",
            "connection",
            "server disconnected",
            "internal server error",
            "overloaded",
            "503",
            "502",
            "500",
        ]
        if any(word in text for word in transient_words):
            return RecoveryDecision("backoff", "transient transport failure")

        return RecoveryDecision("fail", text)

    return RecoveryDecision("none", "")


def can_attempt(state: RecoveryState, kind: str) -> bool:
    if kind == "continue":
        return state.continuation_attempts < RECOVERY_LIMITS["continue"]
    if kind == "compact":
        return state.compact_attempts < RECOVERY_LIMITS["compact"]
    if kind == "backoff":
        return state.transport_attempts < RECOVERY_LIMITS["backoff"]
    return False


def record_attempt(state: RecoveryState, kind: str) -> None:
    if kind == "continue":
        state.continuation_attempts += 1
    elif kind == "compact":
        state.compact_attempts += 1
    elif kind == "backoff":
        state.transport_attempts += 1


def backoff_delay(attempt: int) -> float:
    return min(1.0 * (2 ** max(attempt - 1, 0)), 30.0) + random.uniform(0, 0.5)


def apply_continue_recovery(messages: list) -> list:
    messages.append({"role": "user", "content": CONTINUE_MESSAGE})
    return messages


def apply_compact_recovery(messages: list, compact_fn) -> list:
    """
    compact_fn should accept messages and return compacted messages.
    """
    return compact_fn(messages)


def apply_backoff_recovery(state: RecoveryState) -> None:
    delay = backoff_delay(state.transport_attempts)
    print(f"[Recovery] backoff {delay:.1f}s")
    time.sleep(delay)