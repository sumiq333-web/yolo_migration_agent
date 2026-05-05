from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {
    "completed",
    "failed",
    "rejected",
    "expired",
}

VALID_STATUSES = {
    "pending",
    "completed",
    "failed",
    "rejected",
    "expired",
}


class RequestStore:
    """
    Lightweight request tracking store.

    Storage:
        .team/requests.json

    This store tracks collaboration requests between lead and teammates.

    Important distinction:
        Protocol messages live in .team/inbox/*.jsonl.
        Request records live in .team/requests.json.

    The inbox is drained when read.
    The request store is durable state.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self._save({"requests": {}})

    def create_request(
        self,
        *,
        kind: str,
        sender: str,
        to: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create one tracked request.
        """
        data = self._load()
        now = time.time()
        request_id = self._new_request_id(data)

        record = {
            "request_id": request_id,
            "kind": kind,
            "from": sender,
            "to": to,
            "status": "pending",
            "content": content,
            "payload": payload or {},
            "result": "",
            "reason": "",
            "created_at": now,
            "updated_at": now,
        }

        data["requests"][request_id] = record
        self._save(data)

        return record

    def complete_request(
        self,
        *,
        request_id: str,
        result: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Mark one request as completed.
        """
        return self._set_status(
            request_id=request_id,
            status="completed",
            result=result,
            payload=payload,
        )

    def fail_request(
        self,
        *,
        request_id: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Mark one request as failed.
        """
        return self._set_status(
            request_id=request_id,
            status="failed",
            reason=reason,
            payload=payload,
        )

    def reject_request(
        self,
        *,
        request_id: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Mark one request as rejected.
        """
        return self._set_status(
            request_id=request_id,
            status="rejected",
            reason=reason,
            payload=payload,
        )

    def expire_request(
        self,
        *,
        request_id: str,
        reason: str = "Request expired.",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Mark one request as expired.

        Expired means the request is no longer relevant and should not be waited on.
        It is not the same as failed.
        """
        return self._set_status(
            request_id=request_id,
            status="expired",
            reason=reason,
            payload=payload,
        )

    def get_request(self, request_id: str) -> dict[str, Any]:
        """
        Get one request by id.
        """
        data = self._load()
        record = data["requests"].get(request_id)

        if record is None:
            raise ValueError(f"Request not found: {request_id}")

        return record

    def list_requests(
        self,
        *,
        include_terminal: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List requests, newest first.
        """
        data = self._load()
        requests = list(data["requests"].values())

        if not include_terminal:
            requests = [
                item for item in requests
                if item.get("status") not in TERMINAL_STATUSES
            ]

        requests.sort(
            key=lambda item: item.get("updated_at", item.get("created_at", 0)),
            reverse=True,
        )

        return requests[:limit]

    def render_requests(
        self,
        *,
        include_terminal: bool = True,
        limit: int = 20,
    ) -> str:
        """
        Render requests for CLI/model consumption.
        """
        requests = self.list_requests(
            include_terminal=include_terminal,
            limit=limit,
        )

        if not requests:
            return "No requests."

        lines = []

        for item in requests:
            status = item.get("status", "unknown")
            marker = {
                "pending": "[ ]",
                "completed": "[x]",
                "failed": "[!]",
                "rejected": "[-]",
                "expired": "[~]",
            }.get(status, "[?]")

            request_id = item.get("request_id", "")
            kind = item.get("kind", "")
            sender = item.get("from", "")
            to = item.get("to", "")
            content = self._compact_text(item.get("content", ""), 80)

            lines.append(
                f"{marker} {request_id} {kind} {sender}->{to}: {content}"
            )

        return "\n".join(lines)

    def _set_status(
        self,
        *,
        request_id: str,
        status: str,
        result: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid request status: {status}")

        data = self._load()
        record = data["requests"].get(request_id)

        if record is None:
            raise ValueError(f"Request not found: {request_id}")

        if record.get("status") in TERMINAL_STATUSES:
            return record

        record["status"] = status
        record["updated_at"] = time.time()

        if result:
            record["result"] = result

        if reason:
            record["reason"] = reason

        if payload:
            record["last_payload"] = payload

        self._save(data)

        return record

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"requests": {}}

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"requests": {}}

        if not isinstance(data, dict):
            return {"requests": {}}

        if not isinstance(data.get("requests"), dict):
            data["requests"] = {}

        return data

    def _save(self, data: dict[str, Any]) -> None:
        """
        Atomic write.

        Write to a temporary file first, then replace the target file.
        This reduces the chance of corrupting requests.json if the process exits mid-write.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def _new_request_id(self, data: dict[str, Any]) -> str:
        requests = data.get("requests", {})

        for _ in range(100):
            request_id = "req_" + uuid.uuid4().hex[:8]
            if request_id not in requests:
                return request_id

        raise RuntimeError("Failed to generate unique request_id.")

    def _compact_text(self, text: str, limit: int) -> str:
        text = str(text).replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def find_open_request(
            self,
            *,
            to: str,
            content: str,
            kind: str,
    ) -> dict[str, Any] | None:
        data = self._load()

        normalized_content = content.strip()

        for record in data["requests"].values():
            if record.get("status") in TERMINAL_STATUSES:
                continue

            if record.get("to") != to:
                continue

            if record.get("kind") != kind:
                continue

            if str(record.get("content", "")).strip() != normalized_content:
                continue

            return record

        return None