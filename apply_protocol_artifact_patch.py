#!/usr/bin/env python3
"""
给 yolo_migration_agent 应用 task 元数据与 artifact 闭环补丁。

使用方式：
  1. 把本文件放到 yolo_migration_agent 仓库根目录
  2. 执行：python apply_protocol_artifact_patch.py
  3. 执行：python -m compileall agent
  4. 检查 git diff 后提交

补丁目标：
  - 第 1 点：把 phase / conclusion_type / authorization 持久化到 TaskRecord、task_create_graph schema、dispatch header。
  - 第 2 点：补 submit_change_plan / submit_review_result / submit_validation_result 工具，并在 lead inbox 同步时按 conclusion_type 验收 artifact。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"找不到替换锚点：{label}")
    return text.replace(old, new, 1)


def regex_replace_once(text: str, pattern: str, repl: str, label: str) -> str:
    new_text, count = re.subn(pattern, repl, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"正则替换失败：{label}，匹配数量={count}")
    return new_text


ARTIFACT_PROTOCOL = r'''from __future__ import annotations

import json
from typing import Any

# lead 可以识别的 artifact/result 类型。
# 注意：这里不直接决定 task 成败，只用于 request 级别验收和 task.artifacts 持久化。
ARTIFACT_TYPES = {
    "change_plan",
    "review_result",
    "validation_result",
    "experiment_result",
    "implementation_result",
    "task_result",
}

RESULT_MESSAGE_TYPES = ARTIFACT_TYPES | {"error"}


def parse_artifact_content(content: Any) -> dict[str, Any]:
    """把 teammate 发来的 content 解析成 artifact dict。

    设计上只接受 JSON object 作为结构化 artifact；普通自然语言仍可作为无
    conclusion_type task 的 request result，但不能通过 artifact 验收。
    """
    if isinstance(content, dict):
        return dict(content)
    if not isinstance(content, str):
        return {}
    text = content.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_artifact(
    artifact: dict[str, Any],
    *,
    artifact_type: str = "",
    task_id: int | None = None,
) -> dict[str, Any]:
    """统一 artifact 字段，避免不同角色提交格式轻微不一致。"""
    data = dict(artifact or {})
    if artifact_type:
        data["artifact_type"] = artifact_type
    data["artifact_type"] = str(data.get("artifact_type", "")).strip()
    if task_id is not None and "task_id" not in data:
        data["task_id"] = int(task_id)
    return data


def validate_artifact(
    artifact: dict[str, Any],
    *,
    expected_type: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    """校验 artifact 是否满足 task.conclusion_type。

    返回：(ok, reason, normalized_artifact)。
    这里故意保持轻量：只校验类型和最低必要结构，不把业务判断写死在 runtime。
    """
    normalized = normalize_artifact(artifact)
    artifact_type = str(normalized.get("artifact_type", "")).strip()
    expected = str(expected_type or "").strip()

    if not artifact_type:
        return False, "artifact_type is required", normalized
    if artifact_type not in ARTIFACT_TYPES:
        return False, f"unsupported artifact_type: {artifact_type}", normalized
    if expected and artifact_type != expected:
        return False, (
            f"artifact_type mismatch: expected {expected}, got {artifact_type}"
        ), normalized

    # 最低内容约束：必须有一个可读摘要或结构化结果，避免空壳 artifact 通过。
    has_any_content = any(
        normalized.get(key)
        for key in ("summary", "content", "result", "decision", "changes", "plan", "status")
    )
    if not has_any_content:
        return False, (
            "artifact must include at least one of: "
            "summary/content/result/decision/changes/plan/status"
        ), normalized

    if artifact_type == "change_plan":
        if not normalized.get("summary"):
            return False, "change_plan.summary is required", normalized
        if not (normalized.get("changes") or normalized.get("plan")):
            return False, "change_plan requires changes or plan", normalized

    if artifact_type == "review_result" and not normalized.get("decision"):
        return False, "review_result.decision is required", normalized

    return True, "", normalized
'''


def patch_task_manager() -> None:
    path = "agent/task_manager.py"
    text = read(path)

    if "VALID_AUTHORIZATIONS" not in text:
        text = replace_once(
            text,
            'BROKEN_TODO_STATUSES = {"failed", "blocked"}\n',
            'BROKEN_TODO_STATUSES = {"failed", "blocked"}\n\n'
            '# 写权限只允许 leader 显式授权，默认不允许 teammate 改源码。\n'
            'VALID_AUTHORIZATIONS = {"NO_WRITE", "WRITE_APPROVED"}\n',
            "task_manager: add VALID_AUTHORIZATIONS",
        )

    if "phase: str = \"\"" not in text:
        text = replace_once(
            text,
            '    owner: str = ""\n    todos: list[dict] = field(default_factory=list)\n',
            '    owner: str = ""\n'
            '    # lead 协议元数据：用于 dispatch、artifact 验收和写权限边界。\n'
            '    phase: str = ""\n'
            '    conclusion_type: str = ""\n'
            '    authorization: str = "NO_WRITE"\n'
            '    todos: list[dict] = field(default_factory=list)\n',
            "task_manager: add task protocol metadata fields",
        )

    if "artifacts: list[dict]" not in text:
        text = replace_once(
            text,
            '    allowed_roots: list[str] = field(default_factory=list)\n',
            '    allowed_roots: list[str] = field(default_factory=list)\n'
            '    # teammate 提交并通过 request-level 验收的结构化产物。\n'
            '    artifacts: list[dict] = field(default_factory=list)\n',
            "task_manager: add artifacts field",
        )

    text = regex_replace_once(
        text,
        r"    def _load\(self, task_id: int\) -> TaskRecord:\n        return TaskRecord\(\*\*self\._load_dict\(task_id\)\)",
        '    def _load(self, task_id: int) -> TaskRecord:\n'
        '        data = self._load_dict(task_id)\n'
        '        # 兼容旧 task json：缺失字段走 dataclass 默认值；额外字段忽略。\n'
        '        fields = TaskRecord.__dataclass_fields__\n'
        '        return TaskRecord(**{key: value for key, value in data.items() if key in fields})',
        "task_manager: make _load backward/forward compatible",
    )

    text = regex_replace_once(
        text,
        r"    def create\(self, subject: str, description: str = \"\", owner: str = \"\"\) -> dict:\n        subject = subject\.strip\(\)\n        if not subject:\n            raise ValueError\(\"Task subject is required\"\)\n        task = TaskRecord\(\n            id=self\._next_id\(\),\n            subject=subject,\n            description=description\.strip\(\),\n            owner=owner\.strip\(\),\n        \)",
        '    def create(\n'
        '        self,\n'
        '        subject: str,\n'
        '        description: str = "",\n'
        '        owner: str = "",\n'
        '        phase: str = "",\n'
        '        conclusion_type: str = "",\n'
        '        authorization: str = "NO_WRITE",\n'
        '    ) -> dict:\n'
        '        subject = subject.strip()\n'
        '        if not subject:\n'
        '            raise ValueError("Task subject is required")\n'
        '        phase = str(phase or "").strip()\n'
        '        conclusion_type = str(conclusion_type or "").strip()\n'
        '        authorization = str(authorization or "NO_WRITE").strip().upper()\n'
        '        if authorization not in VALID_AUTHORIZATIONS:\n'
        '            raise ValueError(\n'
        '                f"Invalid task authorization: {authorization}. "\n'
        '                f"Expected one of {sorted(VALID_AUTHORIZATIONS)}"\n'
        '            )\n'
        '        task = TaskRecord(\n'
        '            id=self._next_id(),\n'
        '            subject=subject,\n'
        '            description=description.strip(),\n'
        '            owner=owner.strip(),\n'
        '            phase=phase,\n'
        '            conclusion_type=conclusion_type,\n'
        '            authorization=authorization,\n'
        '        )',
        "task_manager: extend create signature and constructor",
    )

    if "def add_artifact" not in text:
        text = replace_once(
            text,
            '    def assign(self, task_id: int, owner: str) -> dict:\n',
            '    def add_artifact(\n'
            '        self,\n'
            '        task_id: int,\n'
            '        artifact: dict,\n'
            '        *,\n'
            '        source: str = "",\n'
            '        request_id: str = "",\n'
            '    ) -> dict:\n'
            '        """把通过 request-level 验收的 artifact 持久化到 task。\n\n'
            '        注意：这不是 task 完成判定；task 是否 completed/failed 仍由 lead\n'
            '        显式调用 task_set_status 决定。\n'
            '        """\n'
            '        task = self._load(task_id)\n'
            '        item = dict(artifact or {})\n'
            '        if source:\n'
            '            item["source"] = source\n'
            '        if request_id:\n'
            '            item["request_id"] = request_id\n'
            '        task.artifacts.append(item)\n'
            '        self._save(task)\n'
            '        return self._to_public(task)\n\n'
            '    def assign(self, task_id: int, owner: str) -> dict:\n',
            "task_manager: add add_artifact",
        )

    text = replace_once(
        text,
        '            owner = f" owner={task.owner}" if task.owner else ""\n            reason = f" reason={task.status_reason}" if task.status_reason else ""\n            lines.append(\n                f"{marker} #{task.id}: {task.subject}"\n                f"{owner}{blocked}{blocks}{reason}"\n            )',
        '            owner = f" owner={task.owner}" if task.owner else ""\n'
        '            phase = f" phase={task.phase}" if task.phase else ""\n'
        '            conclusion = (\n'
        '                f" conclusion_type={task.conclusion_type}"\n'
        '                if task.conclusion_type else ""\n'
        '            )\n'
        '            authorization = (\n'
        '                f" authorization={task.authorization}"\n'
        '                if task.authorization else ""\n'
        '            )\n'
        '            artifact_count = (\n'
        '                f" artifacts={len(task.artifacts)}" if task.artifacts else ""\n'
        '            )\n'
        '            reason = f" reason={task.status_reason}" if task.status_reason else ""\n'
        '            lines.append(\n'
        '                f"{marker} #{task.id}: {task.subject}"\n'
        '                f"{owner}{phase}{conclusion}{authorization}"\n'
        '                f"{blocked}{blocks}{artifact_count}{reason}"\n'
        '            )',
        "task_manager: render protocol metadata",
    )

    text = replace_once(
        text,
        '            created_task = self.create(\n                subject=str(item["subject"]).strip(),\n                description=str(item.get("description", "")).strip(),\n                owner=str(item.get("owner", "")).strip(),\n            )',
        '            created_task = self.create(\n'
        '                subject=str(item["subject"]).strip(),\n'
        '                description=str(item.get("description", "")).strip(),\n'
        '                owner=str(item.get("owner", "")).strip(),\n'
        '                phase=str(item.get("phase", "")).strip(),\n'
        '                conclusion_type=str(item.get("conclusion_type", "")).strip(),\n'
        '                authorization=str(item.get("authorization", "NO_WRITE")).strip(),\n'
        '            )',
        "task_manager: pass metadata through create_graph",
    )

    write(path, text)


def patch_s02_tool_use() -> None:
    path = "agent/s02_tool_use.py"
    text = read(path)

    if "from agent.artifact_protocol import" not in text:
        text = replace_once(
            text,
            'from agent.team_protocol import RequestStore\n',
            'from agent.team_protocol import RequestStore\n'
            'from agent.artifact_protocol import RESULT_MESSAGE_TYPES, parse_artifact_content, validate_artifact\n',
            "s02: import artifact protocol",
        )

    if "def sync_requests_from_lead_inbox" not in text:
        text = replace_once(
            text,
            'CONCURRENCY_SAFE = {"read_file"}\nCONCURRENCY_UNSAFE = {"write_file", "edit_file"}\n\n\ndef read_and_sync_lead_inbox() -> list[dict]:\n',
            'CONCURRENCY_SAFE = {"read_file"}\n'
            'CONCURRENCY_UNSAFE = {"write_file", "edit_file"}\n\n\n'
            'def _message_type(message: dict) -> str:\n'
            '    return str(message.get("type") or message.get("msg_type") or "").strip()\n\n\n'
            'def _message_request_id(message: dict) -> str:\n'
            '    return str(message.get("request_id") or "").strip()\n\n\n'
            'def sync_requests_from_lead_inbox(inbox: list[dict]) -> None:\n'
            '    """根据 teammate -> lead 消息同步 RequestStore，并验收 artifact。\n\n'
            '    关键边界：\n'
            '    - artifact 校验失败只 fail request，不直接 fail task。\n'
            '    - task 是否 completed/failed 仍必须由 lead 调 task_set_status。\n'
            '    """\n'
            '    for message in inbox:\n'
            '        request_id = _message_request_id(message)\n'
            '        if not request_id:\n'
            '            continue\n\n'
            '        msg_type = _message_type(message)\n'
            '        content = message.get("content", "")\n'
            '        task_id = message.get("task_id")\n\n'
            '        if msg_type == "error":\n'
            '            try:\n'
            '                REQUEST_STORE.fail_request(\n'
            '                    request_id=request_id,\n'
            '                    reason=str(content),\n'
            '                    payload={"message": message},\n'
            '                )\n'
            '            except ValueError:\n'
            '                pass\n'
            '            continue\n\n'
            '        if msg_type not in RESULT_MESSAGE_TYPES:\n'
            '            continue\n\n'
            '        task = None\n'
            '        expected_type = ""\n'
            '        if task_id is not None:\n'
            '            try:\n'
            '                task = TASK_MANAGER.get(int(task_id))\n'
            '                expected_type = str(task.get("conclusion_type", "")).strip()\n'
            '            except (ValueError, TypeError):\n'
            '                REQUEST_STORE.fail_request(\n'
            '                    request_id=request_id,\n'
            '                    reason=f"Invalid task_id in teammate result: {task_id}",\n'
            '                    payload={"message": message},\n'
            '                )\n'
            '                continue\n\n'
            '        # 没有 conclusion_type 的旧任务保持兼容：普通 task_result 可完成 request。\n'
            '        if not expected_type:\n'
            '            try:\n'
            '                REQUEST_STORE.complete_request(\n'
            '                    request_id=request_id,\n'
            '                    result=str(content),\n'
            '                    payload={"message": message},\n'
            '                )\n'
            '            except ValueError:\n'
            '                pass\n'
            '            continue\n\n'
            '        artifact = parse_artifact_content(content)\n'
            '        if task_id is not None and "task_id" not in artifact:\n'
            '            artifact["task_id"] = int(task_id)\n'
            '        ok, reason, normalized = validate_artifact(\n'
            '            artifact, expected_type=expected_type\n'
            '        )\n'
            '        if not ok:\n'
            '            REQUEST_STORE.fail_request(\n'
            '                request_id=request_id,\n'
            '                reason=reason,\n'
            '                payload={"message": message, "artifact": artifact},\n'
            '            )\n'
            '            continue\n\n'
            '        if task_id is not None:\n'
            '            TASK_MANAGER.add_artifact(\n'
            '                int(task_id),\n'
            '                normalized,\n'
            '                source=str(message.get("from") or message.get("sender") or ""),\n'
            '                request_id=request_id,\n'
            '            )\n'
            '        REQUEST_STORE.complete_request(\n'
            '            request_id=request_id,\n'
            '            result=str(content),\n'
            '            payload={"message": message, "artifact": normalized},\n'
            '        )\n\n\n'
            'def read_and_sync_lead_inbox() -> list[dict]:\n',
            "s02: add sync_requests_from_lead_inbox",
        )

    text = replace_once(
        text,
        '        "subject": task["subject"],\n        "workspace": task.get("workspace", ""),\n        "cwd": task.get("cwd", task.get("workspace", "")),\n        "allowed_roots": task.get("allowed_roots", []),\n        "todos": task.get("todos", []),\n',
        '        "subject": task["subject"],\n'
        '        "phase": task.get("phase", ""),\n'
        '        "conclusion_type": task.get("conclusion_type", ""),\n'
        '        "authorization": task.get("authorization", "NO_WRITE"),\n'
        '        "workspace": task.get("workspace", ""),\n'
        '        "cwd": task.get("cwd", task.get("workspace", "")),\n'
        '        "allowed_roots": task.get("allowed_roots", []),\n'
        '        "todos": task.get("todos", []),\n'
        '        "artifacts": task.get("artifacts", []),\n',
        "s02: extend dispatch header",
    )

    if '"conclusion_type": {' not in text:
        text = replace_once(
            text,
            '                                    "owner": {"type": "string"},\n                                },\n',
            '                                    "owner": {"type": "string"},\n'
            '                                    "phase": {\n'
            '                                        "type": "string",\n'
            '                                        "description": "Task phase, e.g. analysis/planning/implementation/review/validation.",\n'
            '                                    },\n'
            '                                    "conclusion_type": {\n'
            '                                        "type": "string",\n'
            '                                        "description": "Expected final artifact type, e.g. change_plan/review_result/validation_result.",\n'
            '                                    },\n'
            '                                    "authorization": {\n'
            '                                        "type": "string",\n'
            '                                        "enum": ["NO_WRITE", "WRITE_APPROVED"],\n'
            '                                        "description": "WRITE_APPROVED is required before source mutation work.",\n'
            '                                    },\n'
            '                                },\n',
            "s02: extend task_create_graph schema",
        )

    write(path, text)


def patch_team_tools() -> None:
    path = "agent/team_tools.py"
    text = read(path)

    if '"name": "submit_change_plan"' not in text:
        text = replace_once(
            text,
            '        {\n            "name": "send_message",\n            "description": "Send a task result or question to lead.",\n',
            '        {\n'
            '            "name": "submit_change_plan",\n'
            '            "description": "Submit the final change_plan artifact for the assigned task.",\n'
            '            "input_schema": {\n'
            '                "type": "object",\n'
            '                "properties": {\n'
            '                    "summary": {"type": "string"},\n'
            '                    "files": {"type": "array", "items": {"type": "string"}},\n'
            '                    "changes": {"type": "array", "items": {"type": "string"}},\n'
            '                    "risk": {"type": "string"},\n'
            '                    "verification": {"type": "string"},\n'
            '                    "notes": {"type": "string"},\n'
            '                },\n'
            '                "required": ["summary", "changes"],\n'
            '            },\n'
            '        },\n'
            '        {\n'
            '            "name": "send_message",\n'
            '            "description": "Send a task result or question to lead.",\n',
            "team_tools: add submit_change_plan tool",
        )

        text = replace_once(
            text,
            '        "todo_update": lambda sender, **kw: _handle_todo_update(\n            sender, task_manager, get_task_id_fn, kw["index"], kw["status"], kw.get("reason", "")\n        ),\n        "send_message": lambda sender, **kw: bus.send(\n',
            '        "todo_update": lambda sender, **kw: _handle_todo_update(\n'
            '            sender, task_manager, get_task_id_fn, kw["index"], kw["status"], kw.get("reason", "")\n'
            '        ),\n'
            '        # 实际发送逻辑由 TeammateManager 注入 request_id/task_id 后覆盖。\n'
            '        "submit_change_plan": lambda sender, **kw: "submit_change_plan is managed by TeammateManager",\n'
            '        "send_message": lambda sender, **kw: bus.send(\n',
            "team_tools: add submit_change_plan handler placeholder",
        )

    if '"name": "submit_review_result"' not in text:
        text = replace_once(
            text,
            '    MUTATING_TOOLS = {"write_file", "edit_file", "run_shell"}\n\n    tools = []\n    for tool in base.tools:\n        if tool["name"] in MUTATING_TOOLS:\n            continue\n',
            '    MUTATING_TOOLS = {"write_file", "edit_file", "run_shell"}\n'
            '    ENGINEER_ONLY_TOOLS = {"submit_change_plan"}\n\n'
            '    tools = []\n'
            '    for tool in base.tools:\n'
            '        if tool["name"] in MUTATING_TOOLS or tool["name"] in ENGINEER_ONLY_TOOLS:\n'
            '            continue\n',
            "team_tools: hide engineer-only tool from reviewer",
        )

        text = replace_once(
            text,
            '    handlers = dict(base.handlers)\n    handlers["send_message"] = lambda sender, **kw: bus.send(\n',
            '    tools.insert(\n'
            '        0,\n'
            '        {\n'
            '            "name": "submit_review_result",\n'
            '            "description": "Submit the final review_result artifact to lead.",\n'
            '            "input_schema": {\n'
            '                "type": "object",\n'
            '                "properties": {\n'
            '                    "decision": {\n'
            '                        "type": "string",\n'
            '                        "enum": ["approve", "request_changes", "reject"],\n'
            '                    },\n'
            '                    "summary": {"type": "string"},\n'
            '                    "issues": {"type": "array", "items": {"type": "string"}},\n'
            '                    "required_changes": {"type": "array", "items": {"type": "string"}},\n'
            '                },\n'
            '                "required": ["decision", "summary"],\n'
            '            },\n'
            '        },\n'
            '    )\n'
            '    handlers = dict(base.handlers)\n'
            '    handlers.pop("submit_change_plan", None)\n'
            '    handlers["submit_review_result"] = lambda sender, **kw: "submit_review_result is managed by TeammateManager"\n'
            '    handlers["send_message"] = lambda sender, **kw: bus.send(\n',
            "team_tools: add reviewer artifact tool",
        )

    if '"name": "submit_validation_result"' not in text:
        text = replace_once(
            text,
            '    tools = [\n        {\n            "name": "send_message",\n',
            '    tools = [\n'
            '        {\n'
            '            "name": "submit_validation_result",\n'
            '            "description": "Submit the final validation_result artifact to lead.",\n'
            '            "input_schema": {\n'
            '                "type": "object",\n'
            '                "properties": {\n'
            '                    "status": {\n'
            '                        "type": "string",\n'
            '                        "enum": ["passed", "failed", "blocked", "not_run"],\n'
            '                    },\n'
            '                    "summary": {"type": "string"},\n'
            '                    "commands": {"type": "array", "items": {"type": "string"}},\n'
            '                    "evidence": {"type": "array", "items": {"type": "string"}},\n'
            '                    "failure_reason": {"type": "string"},\n'
            '                },\n'
            '                "required": ["status", "summary"],\n'
            '            },\n'
            '        },\n'
            '        {\n'
            '            "name": "send_message",\n',
            "team_tools: add validation artifact tool",
        )

        text = replace_once(
            text,
            '        "send_message": lambda sender, **kw: bus.send(\n            sender=sender,\n            to="lead",\n            content=kw["content"],\n            msg_type=kw.get("msg_type", "experiment_result"),\n        ),\n',
            '        "submit_validation_result": lambda sender, **kw: "submit_validation_result is managed by TeammateManager",\n'
            '        "send_message": lambda sender, **kw: bus.send(\n'
            '            sender=sender,\n'
            '            to="lead",\n'
            '            content=kw["content"],\n'
            '            msg_type=kw.get("msg_type", "experiment_result"),\n'
            '        ),\n',
            "team_tools: add validation handler placeholder",
        )

    write(path, text)


def patch_teammate_manager() -> None:
    path = "agent/teammate_manager.py"
    text = read(path)

    if "ARTIFACT_SUBMIT_TOOL_TYPES" not in text:
        text = replace_once(
            text,
            'class TeammateManager:\n    """\n',
            'class TeammateManager:\n'
            '    # teammate 的结构化 artifact 提交工具。\n'
            '    # 提交后 runtime 会自动附加 request_id/task_id，并唤醒 lead。\n'
            '    ARTIFACT_SUBMIT_TOOL_TYPES = {\n'
            '        "submit_change_plan": "change_plan",\n'
            '        "submit_review_result": "review_result",\n'
            '        "submit_validation_result": "validation_result",\n'
            '    }\n\n'
            '    """\n',
            "teammate_manager: add artifact tool mapping",
        )

    text = replace_once(
        text,
        '            if execution.tool_name == "send_message" and execution.status == "executed":\n                return True\n',
        '            if execution.tool_name == "send_message" and execution.status == "executed":\n'
        '                return True\n'
        '            if (\n'
        '                execution.tool_name in self.ARTIFACT_SUBMIT_TOOL_TYPES\n'
        '                and execution.status == "executed"\n'
        '            ):\n'
        '                return True\n',
        "teammate_manager: stop after artifact submit",
    )

    text = replace_once(
        text,
        '        if "send_message" in handlers:\n            handlers["send_message"] = self._build_send_message_handler(member)\n        return {\n',
        '        if "send_message" in handlers:\n'
        '            handlers["send_message"] = self._build_send_message_handler(member)\n'
        '        for tool_name, artifact_type in self.ARTIFACT_SUBMIT_TOOL_TYPES.items():\n'
        '            if tool_name in handlers:\n'
        '                handlers[tool_name] = self._build_artifact_submit_handler(\n'
        '                    member, artifact_type\n'
        '                )\n'
        '        return {\n',
        "teammate_manager: inject artifact submit handlers",
    )

    if "def _build_artifact_submit_handler" not in text:
        text = replace_once(
            text,
            '    def _drain_forced_tool_actions(self, actor: str, member: dict) -> None:\n',
            '    def _build_artifact_submit_handler(self, member: dict, artifact_type: str):\n'
            '        def submit_artifact(sender: str, **kw) -> str:\n'
            '            # 兼容两种调用形式：\n'
            '            # 1. submit_change_plan(summary=..., changes=...)\n'
            '            # 2. submit_xxx(artifact={...})\n'
            '            artifact = {}\n'
            '            raw_artifact = kw.get("artifact")\n'
            '            if isinstance(raw_artifact, dict):\n'
            '                artifact.update(raw_artifact)\n'
            '            for key, value in kw.items():\n'
            '                if key != "artifact" and value is not None:\n'
            '                    artifact[key] = value\n'
            '            artifact["artifact_type"] = artifact_type\n'
            '            task_id = self._active_task_ids.get(sender)\n'
            '            if task_id is not None:\n'
            '                artifact["task_id"] = task_id\n'
            '            return self.bus.send(\n'
            '                sender=sender,\n'
            '                to="lead",\n'
            '                content=json.dumps(artifact, ensure_ascii=False, indent=2),\n'
            '                msg_type=artifact_type,\n'
            '                extra=self._request_extra_for(sender),\n'
            '            )\n\n'
            '        return submit_artifact\n\n'
            '    def _drain_forced_tool_actions(self, actor: str, member: dict) -> None:\n',
            "teammate_manager: add artifact submit handler builder",
        )

    write(path, text)


def main() -> None:
    if not (ROOT / "agent").exists():
        raise SystemExit("请在 yolo_migration_agent 仓库根目录执行本脚本。")

    (ROOT / "agent" / "artifact_protocol.py").write_text(
        ARTIFACT_PROTOCOL,
        encoding="utf-8",
    )
    patch_task_manager()
    patch_s02_tool_use()
    patch_team_tools()
    patch_teammate_manager()
    print("补丁已应用。建议继续执行：python -m compileall agent && git diff")


if __name__ == "__main__":
    main()
