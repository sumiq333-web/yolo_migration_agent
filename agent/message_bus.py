from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "task_request",
    "task_result",
    "review_request",
    "review_result",
    "experiment_request",
    "experiment_result",
    "shutdown_request",
    "shutdown_response",
    "error",
}


class MessageBus:
    """
    File-based message bus for persistent teammates.

    Each participant has one JSONL inbox:
        .team/inbox/lead.jsonl
        .team/inbox/engineer.jsonl
        .team/inbox/reviewer.jsonl
        .team/inbox/experiment_runner.jsonl

    Sending is append-only.
    Reading drains the inbox.
    """

    def __init__(self, inbox_dir: Path):
        self.dir = Path(inbox_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lead_event = threading.Event()

    def send(
        self,
        *,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict[str, Any] | None = None,
    ) -> str:
        sender = sender.strip()
        to = to.strip()
        content = content.strip()

        if msg_type not in VALID_MSG_TYPES:
            return f"Error: invalid message type '{msg_type}'."

        if not sender:
            return "Error: sender is required."

        if not to:
            return "Error: recipient is required."

        if not content:
            return "Error: content is required."

        message = {
            "type": msg_type,
            "from": sender,
            "to": to,
            "content": content,
            "timestamp": time.time(),
        }

        if extra:
            message.update(extra)

        inbox_path = self._inbox_path(to)

        with inbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

        if to == "lead":
            self._lead_event.set()

        return f"Sent {msg_type} from {sender} to {to}."

    def read_inbox(self, name: str) -> list[dict[str, Any]]:
        """
        Read and drain an inbox.
        """
        name = name.strip()
        if not name:
            return []

        inbox_path = self._inbox_path(name)

        if not inbox_path.exists():
            return []

        text = inbox_path.read_text(encoding="utf-8").strip()
        inbox_path.write_text("", encoding="utf-8")

        if not text:
            return []

        messages: list[dict[str, Any]] = []

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue

            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                messages.append(
                    {
                        "type": "error",
                        "from": "system",
                        "to": name,
                        "content": f"Unreadable inbox line: {line}",
                        "timestamp": time.time(),
                    }
                )

        return messages

    def wait_for_lead(self, timeout: float = 10) -> bool:
        """Block until a message arrives for the lead, or timeout. Returns True if woken."""
        triggered = self._lead_event.wait(timeout=timeout)
        if triggered:
            self._lead_event.clear()
        return triggered

    def broadcast(
        self,
        *,
        sender: str,
        recipients: list[str],
        content: str,
        msg_type: str = "broadcast",
    ) -> str:
        count = 0

        for recipient in recipients:
            if recipient == sender:
                continue

            result = self.send(
                sender=sender,
                to=recipient,
                content=content,
                msg_type=msg_type,
            )

            if not result.startswith("Error:"):
                count += 1

        return f"Broadcast sent to {count} teammate(s)."

    def _inbox_path(self, name: str) -> Path:
        safe_name = self._safe_name(name)
        return self.dir / f"{safe_name}.jsonl"

    def _safe_name(self, name: str) -> str:
        cleaned = "".join(
            ch for ch in name.strip()
            if ch.isalnum() or ch in {"_", "-"}
        )
        return cleaned or "unknown"