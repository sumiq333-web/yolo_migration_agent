from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty, Queue


AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = {0, 30}
JITTER_OFFSET_MAX = 4


def cron_matches(expr: str, dt: datetime) -> bool:
    """
    Match a 5-field cron expression against a datetime.

    Fields:
        minute hour day-of-month month day-of-week

    Supports:
        *       any value
        */N     every N
        N       exact value
        N-M     range
        N,M     list

    Day of week:
        0 = Sunday, 1 = Monday, ..., 6 = Saturday
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    cron_dow = (dt.weekday() + 1) % 7

    values = [
        dt.minute,
        dt.hour,
        dt.day,
        dt.month,
        cron_dow,
    ]

    ranges = [
        (0, 59),
        (0, 23),
        (1, 31),
        (1, 12),
        (0, 6),
    ]

    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False

    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """
    Match one cron field.

    This is intentionally small and dependency-free.
    """
    if field == "*":
        return True

    for part in field.split(","):
        step = 1

        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)

            if step <= 0:
                return False

        if part == "*":
            if (value - lo) % step == 0:
                return True

        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)

            if start < lo or end > hi or start > end:
                return False

            if start <= value <= end and (value - start) % step == 0:
                return True

        else:
            exact = int(part)

            if exact < lo or exact > hi:
                return False

            if exact == value:
                return True

    return False


class CronScheduler:
    """
    Persistent cron scheduler.

    This remembers future prompts and wakes the main agent loop by pushing
    notifications into a queue.

    It does not execute tools directly.
    It does not call the model directly.
    """

    def __init__(
        self,
        *,
        schedule_file: Path,
        check_interval_seconds: int = 1,
    ):
        self.schedule_file = Path(schedule_file)
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)

        self.check_interval_seconds = check_interval_seconds
        self.tasks: list[dict] = []
        self.queue: Queue[str] = Queue()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_check_minute: int | None = None

    # ---------- lifecycle ----------

    def start(self) -> str:
        """
        Load durable schedules and start the checker thread.
        """
        if self._thread is not None and self._thread.is_alive():
            return "Cron scheduler already running."

        self._load_durable()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._check_loop,
            daemon=True,
        )
        self._thread.start()

        return f"Cron scheduler started. Loaded {len(self.tasks)} durable schedule(s)."

    def stop(self) -> None:
        """
        Stop the checker thread.
        """
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=2)

    # ---------- public tool API ----------

    def create(
        self,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
    ) -> str:
        """
        Create a scheduled prompt.

        recurring=True:
            fires repeatedly until deleted or auto-expired

        recurring=False:
            fires once, then is removed
        """
        cron = cron.strip()
        prompt = prompt.strip()

        if not cron:
            raise ValueError("cron is required")

        if not prompt:
            raise ValueError("prompt is required")

        if not self._is_valid_cron(cron):
            raise ValueError(f"Invalid cron expression: {cron}")

        task_id = uuid.uuid4().hex[:8]

        task = {
            "id": task_id,
            "cron": cron,
            "prompt": prompt,
            "recurring": bool(recurring),
            "durable": bool(durable),
            "createdAt": time.time(),
            "last_fired": None,
        }

        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron)

        self.tasks.append(task)

        if durable:
            self._save_durable()

        mode = "recurring" if recurring else "one-shot"
        store = "durable" if durable else "session-only"

        return f"Created cron task {task_id} ({mode}, {store}): cron={cron}"

    def delete(self, task_id: str) -> str:
        """
        Delete a scheduled prompt by id.
        """
        before = len(self.tasks)
        self.tasks = [task for task in self.tasks if task["id"] != task_id]

        if len(self.tasks) == before:
            return f"Task {task_id} not found."

        self._save_durable()
        return f"Deleted cron task {task_id}."

    def list_tasks(self) -> str:
        """
        Render all scheduled prompts.
        """
        if not self.tasks:
            return "No scheduled tasks."

        lines = []

        for task in self.tasks:
            mode = "recurring" if task.get("recurring") else "one-shot"
            store = "durable" if task.get("durable") else "session"
            age_hours = (time.time() - float(task["createdAt"])) / 3600

            last_fired = task.get("last_fired")
            if last_fired:
                last_text = datetime.fromtimestamp(last_fired).isoformat(timespec="seconds")
            else:
                last_text = "never"

            lines.append(
                f"{task['id']}  {task['cron']}  "
                f"[{mode}/{store}] "
                f"age={age_hours:.1f}h last_fired={last_text}: "
                f"{task['prompt'][:80]}"
            )

        return "\n".join(lines)

    def drain_notifications(self) -> list[str]:
        """
        Return all due scheduled prompts and clear the queue.
        """
        notifications: list[str] = []

        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break

        return notifications

    # ---------- checker loop ----------

    def _check_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute

            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            self._stop_event.wait(timeout=self.check_interval_seconds)

    def _check_tasks(self, now: datetime) -> None:
        expired_ids: set[str] = set()
        fired_oneshot_ids: set[str] = set()

        for task in list(self.tasks):
            task_id = task["id"]

            if self._is_expired(task):
                expired_ids.add(task_id)
                continue

            check_time = self._apply_jitter(now, task)

            if not cron_matches(task["cron"], check_time):
                continue

            self._fire(task)

            if not task.get("recurring"):
                fired_oneshot_ids.add(task_id)

        remove_ids = expired_ids | fired_oneshot_ids
        if remove_ids:
            self.tasks = [
                task for task in self.tasks
                if task["id"] not in remove_ids
            ]
            self._save_durable()

    def _fire(self, task: dict) -> None:
        task["last_fired"] = time.time()

        notification = (
            f"[Scheduled task {task['id']}]: {task['prompt']}"
        )

        self.queue.put(notification)

        if task.get("durable"):
            self._save_durable()

    # ---------- helpers ----------

    def _is_valid_cron(self, cron: str) -> bool:
        probe = datetime.now()
        try:
            return cron_matches(cron, probe) or len(cron.strip().split()) == 5
        except Exception:
            return False

    def _is_expired(self, task: dict) -> bool:
        if not task.get("recurring"):
            return False

        age_days = (time.time() - float(task["createdAt"])) / 86400
        return age_days > AUTO_EXPIRY_DAYS

    def _apply_jitter(self, now: datetime, task: dict) -> datetime:
        jitter = int(task.get("jitter_offset", 0) or 0)

        if jitter <= 0:
            return now

        return now - timedelta(minutes=jitter)

    def _compute_jitter(self, cron: str) -> int:
        fields = cron.strip().split()

        if not fields:
            return 0

        minute_field = fields[0]

        try:
            minute = int(minute_field)
        except ValueError:
            return 0

        if minute not in JITTER_MINUTES:
            return 0

        return (hash(cron) % JITTER_OFFSET_MAX) + 1

    def _load_durable(self) -> None:
        if not self.schedule_file.exists():
            self.tasks = []
            return

        try:
            data = json.loads(self.schedule_file.read_text(encoding="utf-8"))
            self.tasks = [
                task for task in data
                if task.get("durable")
            ]
        except Exception:
            self.tasks = []

    def _save_durable(self) -> None:
        durable_tasks = [
            task for task in self.tasks
            if task.get("durable")
        ]

        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        self.schedule_file.write_text(
            json.dumps(durable_tasks, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def create_after(
            self,
            delay_seconds: int,
            prompt: str,
            durable: bool = False,
    ) -> str:
        """
        Create a one-shot schedule after a relative delay.

        This is for user-facing reminders like:
        - remind me in 2 minutes
        - check status in 30 seconds

        It reuses create() by converting the target time into a one-shot cron.
        """
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be > 0")

        run_at = datetime.now() + timedelta(seconds=delay_seconds)
        cron = f"{run_at.minute} {run_at.hour} {run_at.day} {run_at.month} *"

        return self.create(
            cron=cron,
            prompt=prompt,
            recurring=False,
            durable=durable,
        )