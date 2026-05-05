from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


RuntimeStatus = Literal["running", "completed", "failed", "timeout", "error"]


@dataclass
class RuntimeTaskRecord:
    id: str
    command: str
    status: RuntimeStatus
    started_at: float
    finished_at: float | None
    result_preview: str
    output_file: str
    return_code: int | None


@dataclass
class RuntimeNotification:
    task_id: str
    status: RuntimeStatus
    command: str
    preview: str
    output_file: str


class BackgroundManager:
    """
    Manage background runtime tasks.

    Runtime tasks are execution slots.
    They are not persistent project tasks from s12.
    """

    def __init__(self, *, runtime_dir: Path, cwd: Path):
        self.dir = Path(runtime_dir)
        self.cwd = Path(cwd)
        self.dir.mkdir(parents=True, exist_ok=True)

        self._notifications: list[RuntimeNotification] = []
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 300) -> str:
        command = command.strip()
        if not command:
            raise ValueError("command is required")

        task_id = uuid.uuid4().hex[:8]
        output_file = self._output_path(task_id)

        record = RuntimeTaskRecord(
            id=task_id,
            command=command,
            status="running",
            started_at=time.time(),
            finished_at=None,
            result_preview="",
            output_file=str(output_file),
            return_code=None,
        )
        self._save(record)

        thread = threading.Thread(
            target=self._execute,
            args=(task_id, command, timeout),
            daemon=True,
        )
        thread.start()

        return (
            f"Background task {task_id} started: {command[:80]} "
            f"(output_file={output_file})"
        )

    def check(self, task_id: str | None = None) -> str:
        if task_id:
            record = self._load(task_id)
            visible = {
                "id": record.id,
                "status": record.status,
                "command": record.command,
                "result_preview": record.result_preview,
                "output_file": record.output_file,
                "return_code": record.return_code,
            }
            return json.dumps(visible, ensure_ascii=False, indent=2)

        records = self.list_records()
        if not records:
            return "No background tasks."

        lines = []
        for record in records:
            preview = record.result_preview or "(running)"
            lines.append(
                f"{record.id}: [{record.status}] "
                f"{record.command[:60]} -> {preview}"
            )
        return "\n".join(lines)

    def list_records(self) -> list[RuntimeTaskRecord]:
        records = []

        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(RuntimeTaskRecord(**data))
            except Exception:
                continue

        return records

    def drain_notifications(self) -> list[RuntimeNotification]:
        with self._lock:
            notifications = list(self._notifications)
            self._notifications.clear()
        return notifications

    def render_notifications(self) -> str:
        notifications = self.drain_notifications()
        if not notifications:
            return ""

        lines = ["<background-results>"]
        for item in notifications:
            lines.append(
                f"[bg:{item.task_id}] {item.status}: {item.preview} "
                f"(output_file={item.output_file})"
            )
        lines.append("</background-results>")

        return "\n".join(lines)

    def _execute(self, task_id: str, command: str, timeout: int) -> None:
        record = self._load(task_id)
        output_path = self._output_path(task_id)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            status: RuntimeStatus = "completed" if result.returncode == 0 else "failed"
            return_code = result.returncode

        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            output = (stdout + stderr).strip() or "Error: Timeout"
            status = "timeout"
            return_code = None

        except Exception as e:
            output = f"Error: {e}"
            status = "error"
            return_code = None

        final_output = output or "(no output)"
        output_path.write_text(final_output, encoding="utf-8", errors="replace")

        record.status = status
        record.finished_at = time.time()
        record.result_preview = self._preview(final_output)
        record.output_file = str(output_path)
        record.return_code = return_code
        self._save(record)

        self._notify(
            RuntimeNotification(
                task_id=task_id,
                status=status,
                command=command[:80],
                preview=record.result_preview,
                output_file=str(output_path),
            )
        )

    def _notify(self, notification: RuntimeNotification) -> None:
        with self._lock:
            self._notifications.append(notification)

    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _output_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def _load(self, task_id: str) -> RuntimeTaskRecord:
        path = self._record_path(task_id)
        if not path.exists():
            raise ValueError(f"Unknown background task {task_id}")

        data = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeTaskRecord(**data)

    def _save(self, record: RuntimeTaskRecord) -> None:
        self._record_path(record.id).write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _preview(self, output: str, limit: int = 500) -> str:
        compact = " ".join((output or "(no output)").split())
        return compact[:limit]