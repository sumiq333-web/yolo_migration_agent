import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal
from agent.task_plan_validator import validate_todos_against_task

TaskStatus = Literal["pending", "in_progress", "blocked", "failed", "completed", "deleted"]

VALID_STATUSES = {"pending", "in_progress", "blocked", "failed", "completed", "deleted"}
TODO_STATUSES = {"pending", "in_progress", "completed", "failed", "blocked", "skipped"}
# task 的终态。进入这些状态后，当前任务不应该再被 agent_loop 当成 active work。
TERMINAL_STATUSES = {"completed", "failed", "deleted"}

# 当前执行图里仍需要继续推进或等待收束的状态。
ACTIVE_STATUSES = {"pending", "in_progress", "blocked"}

# todo 层面的 resolved 状态。
# 注意：failed/blocked todo 不算 resolved，必须由 leader 决定 task failed 或重试。
RESOLVED_TODO_STATUSES = {"completed", "skipped"}
BROKEN_TODO_STATUSES = {"failed", "blocked"}


@dataclass
class TaskRecord:
    id: int
    subject: str
    description: str = ""
    status: TaskStatus = "pending"

    # 任务终态或阻塞态的解释原因。
    # 例如：
    # - completed: 可以为空，或者写验收说明
    # - failed: 必须写失败原因
    # - blocked: 建议写需要什么外部输入
    status_reason: str = ""

    blockedBy: list[int] = field(default_factory=list)
    blocks: list[int] = field(default_factory=list)
    owner: str = ""
    todos: list[dict] = field(default_factory=list)
    workspace: str = ""
    cwd: str = ""
    allowed_roots: list[str] = field(default_factory=list)

    def is_ready(self) -> bool:
        return self.status == "pending" and not self.blockedBy


class TaskManager:
    """
    Persistent task graph.

    This manages durable work items, not runtime workers.
    One task is one JSON file:
        .tasks/task_1.json
        .tasks/task_2.json

    Design goals:
    - lightweight
    - explicit dependency operations
    - easy to plug into tool dispatch
    - future-friendly for multi-agent / runtime tasks
    """

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------- low-level storage ----------


    def _task_path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _task_paths(self) -> list[Path]:
        return sorted(
            self.dir.glob("task_*.json"),
            key=lambda path: self._id_from_path(path),
        )

    def _id_from_path(self, path: Path) -> int:
        try:
            return int(path.stem.split("_", 1)[1])
        except Exception:
            return 0

    def _next_id(self) -> int:
        ids = [self._id_from_path(path) for path in self._task_paths()]
        return max(ids, default=0) + 1

    def _load_dict(self, task_id: int) -> dict:
        path = self._task_path(task_id)
        if not path.exists():
            raise ValueError(f"Task not found: {task_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self, task_id: int) -> TaskRecord:
        return TaskRecord(**self._load_dict(task_id))

    def _save(self, task: TaskRecord) -> None:
        path = self._task_path(task.id)
        path.write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _all(self, include_deleted: bool = False) -> list[TaskRecord]:
        tasks: list[TaskRecord] = []

        for path in self._task_paths():
            data = json.loads(path.read_text(encoding="utf-8"))
            task = TaskRecord(**data)

            if not include_deleted and task.status == "deleted":
                continue

            tasks.append(task)

        return tasks

    # ---------- public query API ----------

    def create(self, subject: str, description: str = "", owner: str = "") -> dict:
        subject = subject.strip()
        if not subject:
            raise ValueError("Task subject is required")

        task = TaskRecord(
            id=self._next_id(),
            subject=subject,
            description=description.strip(),
            owner=owner.strip(),
        )

        self._save(task)
        return self._to_public(task)

    def get(self, task_id: int) -> dict:
        return self._to_public(self._load(task_id))

    def list_tasks(self, include_deleted: bool = False) -> list[dict]:
        return [self._to_public(task) for task in self._all(include_deleted)]

    def ready(self) -> list[dict]:
        return [
            self._to_public(task)
            for task in self._all(include_deleted=False)
            if task.is_ready()
        ]

    # ---------- public mutation API ----------

    def set_status(
            self,
            task_id: int,
            status: str,
            reason: str = "",
            cascade_failed: bool = False,
    ) -> dict:
        """
        设置 task 状态。

        设计原则：
        1. completed 表示成功终止，必须所有 todo 都 completed/skipped。
        2. failed 表示失败终止，可以存在 failed todo，但必须写 reason。
        3. pending/in_progress 是非终态，agent_loop 不应该在这些状态下结束。
        4. cascade_failed=True 时，leader 可以把当前 task 的下游依赖任务一起置为 failed。
        """
        status = str(status).strip().lower()
        reason = str(reason or "").strip()

        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid task status: {status}")

        task = self._load(task_id)

        # 已经分配给 teammate 的 task 不允许回到 pending。
        # retry 应该通过 dispatch_to_teammate(allow_retry=True)，而不是把 task 回滚到 pending。
        if status == "pending" and task.owner:
            raise ValueError(
                f"Cannot reset task #{task_id} to pending while it is assigned to {task.owner}. "
                "Use dispatch_to_teammate with allow_retry=true for retry, or create a new task."
            )

        # failed 是终态，必须有原因，方便最终向用户汇报，也方便后续审计。
        if status == "failed" and not reason:
            raise ValueError(
                f"Cannot mark task #{task_id} as failed without a reason."
            )

        # completed 是成功终态，不能有 pending/in_progress/failed/blocked todo。
        if status == "failed" and task.todos:
            for i, todo in enumerate(task.todos):
                if todo.get("status") in ("pending", "in_progress"):
                    task.todos[i]["status"] = "skipped"
                    task.todos[i]["reason"] = (
                        f"Skipped because parent task #{task_id} was set to failed"
                        + (f": {reason}" if reason else ".")
                    )

        if status == "completed" and task.todos:
            undone = [
                t for t in task.todos
                if t.get("status") not in RESOLVED_TODO_STATUSES
            ]
            broken = [
                t for t in task.todos
                if t.get("status") in BROKEN_TODO_STATUSES
            ]

            if undone:
                items = ", ".join(
                    f"#{i}({t.get('status')}): {t.get('content')}"
                    for i, t in enumerate(undone)
                )
                hint = ""
                if broken:
                    hint = f" ({len(broken)} broken — mark task failed or retry instead)"
                raise ValueError(
                    f"Cannot complete: {len(undone)} todo(s) not resolved: {items}{hint}"
                )

        task.status = status  # type: ignore[assignment]

        # 只有提供 reason 时才覆盖，避免 completed 空 reason 抹掉历史信息。
        # failed 强制有 reason，所以 failed 一定会落盘。
        if reason:
            task.status_reason = reason

        self._save(task)

        if status == "completed":
            self._unlock_tasks_blocked_by(task_id)

        if status == "failed" and cascade_failed:
            self._cascade_fail_tasks_blocked_by(
                failed_id=task_id,
                reason=reason,
            )

        return self._to_public(self._load(task_id))

    def set_workspace(self, task_id: int, path: str) -> dict:
        p = Path(path).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {p}")
        task = self._load(task_id)
        task.workspace = str(p)
        task.cwd = str(p)
        task.allowed_roots = [str(p)]
        self._save(task)
        # Also activate for the current process (lead or teammate)
        from agent.state import STATE
        STATE["yolo_path"] = str(p)
        root_str = str(p)
        if root_str not in STATE["workspace_roots"]:
            STATE["workspace_roots"].append(root_str)
        return self._to_public(task)

    def set_todos(self, task_id: int, todos: list[dict]) -> dict:
        task = self._load(task_id)
        if task.todos:
            raise ValueError(
                f"Cannot reset todos for task #{task_id} because todos already exist. "
                "Use todo_update or create a new task instead."
            )
        if task.status != "pending":
            raise ValueError(
                f"Cannot set initial todos for task #{task_id} after it has started "
                f"(status={task.status}). Use todo_update or create a new task instead."
            )
        validation = validate_todos_against_task(
            subject=task.subject,
            description=task.description,
            todos=todos,
            strict=True,
        )
        if not validation.ok:
            raise ValueError("Invalid task todo plan:\n" + validation.render())

        normalized_todos: list[dict] = []

        for i, item in enumerate(todos):
            content = str(item.get("content", "")).strip()
            active_form = str(item.get("activeForm", "")).strip()
            status = str(item.get("status", "pending")).lower().strip()
            reason = str(item.get("reason", "")).strip()

            if not content:
                raise ValueError(f"Todo {i}: content required")

            if status not in TODO_STATUSES:
                raise ValueError(f"Todo {i}: invalid status '{status}'")

            if status != "pending":
                raise ValueError(
                    f"Todo {i}: initial status must be 'pending', got '{status}'. "
                    "Use todo_update after dispatch to record progress."
                )

            if status in ("blocked", "failed") and not reason:
                raise ValueError(f"Todo {i}: reason required for status '{status}'")

            normalized = dict(item)
            normalized["content"] = content
            normalized["status"] = status

            if active_form:
                normalized["activeForm"] = active_form

            if reason:
                normalized["reason"] = reason

            normalized_todos.append(normalized)

        task.todos = normalized_todos
        self._save(task)
        return self._to_public(task)

    def update_todo_item(self, task_id: int, index: int, status: str, reason: str = "") -> dict:
        task = self._load(task_id)

        if index < 0 or index >= len(task.todos):
            raise ValueError(f"Todo index {index} out of range")

        status = str(status).lower().strip()
        reason = str(reason or "").strip()

        if status not in TODO_STATUSES:
            raise ValueError(f"Invalid todo status: {status}")

        if status in ("blocked", "failed") and not reason:
            raise ValueError(f"Todo reason required for status '{status}'")

        task.todos[index]["status"] = status

        if reason:
            task.todos[index]["reason"] = reason

        self._save(task)
        return self._to_public(task)

    def assign(self, task_id: int, owner: str) -> dict:
        task = self._load(task_id)
        task.owner = owner.strip()
        self._save(task)
        return self._to_public(task)

    def add_dependency(self, blocker_id: int, blocked_id: int) -> dict:
        """
        Declare:
            blocker_id must complete before blocked_id can start.

        Meaning:
            blocker.blocks contains blocked_id
            blocked.blockedBy contains blocker_id
        """
        if blocker_id == blocked_id:
            raise ValueError("A task cannot depend on itself")

        blocker = self._load(blocker_id)
        blocked = self._load(blocked_id)

        if blocker.status == "deleted":
            raise ValueError(f"Cannot use deleted task as blocker: {blocker_id}")

        if blocked.status == "deleted":
            raise ValueError(f"Cannot block deleted task: {blocked_id}")

        if blocked_id not in blocker.blocks:
            blocker.blocks.append(blocked_id)

        if blocker_id not in blocked.blockedBy:
            blocked.blockedBy.append(blocker_id)

        blocker.blocks = self._unique_sorted(blocker.blocks)
        blocked.blockedBy = self._unique_sorted(blocked.blockedBy)

        self._save(blocker)
        self._save(blocked)

        return {
            "blocker": self._to_public(blocker),
            "blocked": self._to_public(blocked),
        }

    def remove_dependency(self, blocker_id: int, blocked_id: int) -> dict:
        blocker = self._load(blocker_id)
        blocked = self._load(blocked_id)

        blocker.blocks = [item for item in blocker.blocks if item != blocked_id]
        blocked.blockedBy = [item for item in blocked.blockedBy if item != blocker_id]

        self._save(blocker)
        self._save(blocked)

        return {
            "blocker": self._to_public(blocker),
            "blocked": self._to_public(blocked),
        }

    def delete(self, task_id: int) -> dict:
        """
        Logical delete.

        Also removes this task from other tasks' dependency fields so the graph
        does not keep dangling edges.
        """
        task = self._load(task_id)
        task.status = "deleted"
        task.blockedBy = []
        task.blocks = []
        self._save(task)

        for other in self._all(include_deleted=True):
            if other.id == task_id:
                continue

            changed = False

            if task_id in other.blockedBy:
                other.blockedBy = [item for item in other.blockedBy if item != task_id]
                changed = True

            if task_id in other.blocks:
                other.blocks = [item for item in other.blocks if item != task_id]
                changed = True

            if changed:
                self._save(other)

        return self._to_public(task)

    # ---------- graph mechanics ----------

    def _unlock_tasks_blocked_by(self, completed_id: int) -> None:
        """
        When task A completes, remove A from every other task's blockedBy.
        """
        for task in self._all(include_deleted=False):
            if completed_id not in task.blockedBy:
                continue

            task.blockedBy = [
                blocker_id for blocker_id in task.blockedBy
                if blocker_id != completed_id
            ]
            self._save(task)

    def _cascade_fail_tasks_blocked_by(
            self,
            failed_id: int,
            reason: str,
            visited: set[int] | None = None,
    ) -> None:
        """
        当一个关键 task 失败时，把所有依赖它的下游 task 一起置为 failed。

        注意：
        - 只处理当前 task.blocks 中记录的下游任务。
        - completed/deleted 任务不回滚。
        - already failed 的任务不重复写。
        - 用 visited 防止依赖图异常时递归死循环。
        """
        visited = visited or set()

        if failed_id in visited:
            return

        visited.add(failed_id)

        try:
            failed_task = self._load(failed_id)
        except ValueError:
            return

        for blocked_id in failed_task.blocks:
            if blocked_id in visited:
                continue

            try:
                blocked_task = self._load(blocked_id)
            except ValueError:
                continue

            # 已经成功/删除/失败的任务不再覆盖。
            if blocked_task.status not in {"completed", "deleted", "failed"}:
                blocked_task.status = "failed"
                blocked_task.status_reason = (
                    f"Upstream task #{failed_id} failed, so this dependent task cannot proceed. "
                    f"Upstream failure reason: {reason}"
                )
                self._save(blocked_task)

            # 继续向下游传播失败。
            self._cascade_fail_tasks_blocked_by(
                failed_id=blocked_id,
                reason=reason,
                visited=visited,
            )

    def repair_graph(self) -> dict:
        """
        Optional maintenance tool.

        Rebuild blockedBy from blocks.
        Useful if files were manually edited.
        """
        tasks = {task.id: task for task in self._all(include_deleted=True)}

        for task in tasks.values():
            task.blockedBy = []

        for blocker in tasks.values():
            if blocker.status == "deleted":
                continue

            valid_blocks = []

            for blocked_id in blocker.blocks:
                blocked = tasks.get(blocked_id)
                if blocked is None or blocked.status == "deleted":
                    continue

                valid_blocks.append(blocked_id)

                if blocker.id not in blocked.blockedBy:
                    blocked.blockedBy.append(blocker.id)

            blocker.blocks = self._unique_sorted(valid_blocks)

        for task in tasks.values():
            task.blockedBy = self._unique_sorted(task.blockedBy)
            self._save(task)

        return {
            "repaired": len(tasks),
            "tasks": [self._to_public(task) for task in tasks.values()],
        }

    # ---------- rendering ----------

    def render_list(self, include_deleted: bool = False) -> str:
        tasks = self._all(include_deleted=include_deleted)

        if not tasks:
            return "No tasks."

        lines = []

        for task in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "blocked": "[!]",
                "failed": "[X]",
                "completed": "[x]",
                "deleted": "[-]",
            }.get(task.status, "[?]")

            blocked = f" blockedBy={task.blockedBy}" if task.blockedBy else ""
            blocks = f" blocks={task.blocks}" if task.blocks else ""
            owner = f" owner={task.owner}" if task.owner else ""
            reason = f" reason={task.status_reason}" if task.status_reason else ""
            lines.append(
                f"{marker} #{task.id}: {task.subject}"
                f"{owner}{blocked}{blocks}{reason}"
            )

        return "\n".join(lines)

    def render_ready(self) -> str:
        tasks = [
            task for task in self._all(include_deleted=False)
            if task.is_ready()
        ]

        if not tasks:
            return "No ready tasks."

        lines = []
        for task in tasks:
            owner = f" owner={task.owner}" if task.owner else ""
            lines.append(f"[ready] #{task.id}: {task.subject}{owner}")

        return "\n".join(lines)

    def create_graph(
            self,
            tasks: list[dict],
            dependencies: list[dict] | None = None,
    ) -> dict:
        """
        Create a persistent task graph in one operation.

        This is a high-level orchestration method.
        It reuses existing single-purpose methods:
        - create()
        - add_dependency()
        - ready()

        It should not duplicate task creation or dependency maintenance logic.
        """
        dependencies = dependencies or []

        if not tasks:
            raise ValueError("tasks is required")

        # 1. Validate all task keys before writing anything.
        seen_keys: set[str] = set()

        for item in tasks:
            key = str(item.get("key", "")).strip()
            subject = str(item.get("subject", "")).strip()

            if not key:
                raise ValueError("Each task requires a non-empty key")

            if not subject:
                raise ValueError(f"Task {key!r} requires a non-empty subject")

            if key in seen_keys:
                raise ValueError(f"Duplicate task key: {key}")

            seen_keys.add(key)

        # 2. Validate all dependency keys before writing anything.
        for dep in dependencies:
            blocker_key = str(dep.get("blocker", "")).strip()
            blocked_key = str(dep.get("blocked", "")).strip()

            if blocker_key not in seen_keys:
                raise ValueError(f"Unknown dependency blocker key: {blocker_key}")

            if blocked_key not in seen_keys:
                raise ValueError(f"Unknown dependency blocked key: {blocked_key}")

            if blocker_key == blocked_key:
                raise ValueError(f"Task cannot depend on itself: {blocker_key}")

        # 3. Create tasks by reusing create().
        key_to_id: dict[str, int] = {}
        created: list[dict] = []

        for item in tasks:
            key = str(item["key"]).strip()

            created_task = self.create(
                subject=str(item["subject"]).strip(),
                description=str(item.get("description", "")).strip(),
                owner=str(item.get("owner", "")).strip(),
            )

            key_to_id[key] = created_task["id"]
            created.append(created_task)

        # 4. Add dependencies by reusing add_dependency().
        linked: list[dict] = []

        for dep in dependencies:
            blocker_key = str(dep["blocker"]).strip()
            blocked_key = str(dep["blocked"]).strip()

            linked.append(
                self.add_dependency(
                    blocker_id=key_to_id[blocker_key],
                    blocked_id=key_to_id[blocked_key],
                )
            )

        # 5. Compute ready tasks by reusing ready().
        return {
            "created": created,
            "key_to_id": key_to_id,
            "dependencies": linked,
            "ready": self.ready(),
        }

    # ---------- helpers ----------

    def _to_public(self, task: TaskRecord) -> dict:
        data = asdict(task)
        data["ready"] = task.is_ready()
        return data

    def _unique_sorted(self, values: list[int]) -> list[int]:
        return sorted(set(int(value) for value in values))