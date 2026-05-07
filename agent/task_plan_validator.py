from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


FILE_RE = re.compile(
    r"(?<![\w.-])[\w][\w.-]*\."
    r"(?:py|ya?ml|json|txt|md|toml|cfg|ini|pt|onnx|engine|xml|csv)(?![\w.-])",
    re.IGNORECASE,
)

TARGET_RESOLUTION_WORDS = (
    "resolve",
    "locate",
    "find",
    "confirm",
    "identify",
    "discover",
    "exists",
    "existing",
    "path",
    "target",
    "candidate",
    "确认",
    "查找",
    "找到",
    "定位",
    "存在",
    "路径",
    "候选",
)

VERIFICATION_WORDS = (
    "verify",
    "validate",
    "test",
    "check",
    "parse",
    "run",
    "验证",
    "校验",
    "测试",
    "检查",
    "解析",
    "运行",
)

VERIFICATION_SUCCESS_WORDS = (
    "success",
    "failure",
    "criteria",
    "exit code",
    "exit_code",
    "stdout",
    "stderr",
    "result",
    "expected",
    "must",
    "require",
    "pass",
    "fail",
    "成功",
    "失败",
    "标准",
    "输出",
    "返回",
    "必须",
)

VAGUE_TARGET_REFS = (
    "the file",
    "this file",
    "that file",
    "related file",
    "related config",
    "above file",
    "above files",
    "target file",
    "该文件",
    "这个文件",
    "那个文件",
    "相关文件",
    "相关配置",
    "上述文件",
    "目标文件",
    "文件头部",
)

CREATE_WORDS = (
    "create",
    "generate",
    "write a new",
    "new file",
    "创建",
    "新建",
    "生成",
)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines: list[str] = []

        if self.errors:
            lines.append("Errors:")
            lines.extend(f"- {error}" for error in self.errors)

        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in self.warnings)

        return "\n".join(lines) if lines else "OK"


def extract_file_mentions(text: str) -> set[str]:
    return {match.group(0) for match in FILE_RE.finditer(text or "")}


def _norm(value: str) -> str:
    return value.strip().casefold()


def _contains_any(text: str, words: Iterable[str]) -> bool:
    lowered = (text or "").casefold()
    return any(word.casefold() in lowered for word in words)


def _todo_blob(todo: dict) -> str:
    return "\n".join(
        [
            str(todo.get("activeForm", "")),
            str(todo.get("content", "")),
            " ".join(str(x) for x in todo.get("target_refs", []) or []),
            " ".join(str(x) for x in todo.get("acceptance_criteria", []) or []),
        ]
    )


def _todos_blob(todos: list[dict]) -> str:
    return "\n".join(_todo_blob(todo) for todo in todos)


def _format_missing(items: list[str]) -> str:
    return ", ".join(sorted(items, key=lambda x: x.casefold()))


def validate_todos_against_task(
    *,
    subject: str,
    description: str = "",
    todos: list[dict],
    strict: bool = True,
) -> ValidationResult:
    """
    Validate whether a todo plan is executable, verifiable, and safe enough to persist.

    This validator is intentionally generic. It should not contain YOLO-specific rules.
    Domain-specific validation should live in separate validators or skills.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(todos, list) or not todos:
        return ValidationResult(ok=False, errors=["Todos are required."])

    task_text = f"{subject or ''}\n{description or ''}"
    todo_text = _todos_blob(todos)

    task_files = extract_file_mentions(task_text)
    todo_files = extract_file_mentions(todo_text)

    normalized_task_files = {_norm(item): item for item in task_files}
    normalized_todo_files = {_norm(item): item for item in todo_files}

    missing_files = [
        original
        for normalized, original in normalized_task_files.items()
        if normalized not in normalized_todo_files
    ]

    if missing_files:
        errors.append(
            "Todos do not explicitly mention all file targets from the task subject/description: "
            + _format_missing(missing_files)
        )

    is_multi_file_task = len(task_files) >= 2

    if is_multi_file_task:
        first_todo_text = _todo_blob(todos[0])
        first_todo_files = {_norm(item): item for item in extract_file_mentions(first_todo_text)}

        if not _contains_any(first_todo_text, TARGET_RESOLUTION_WORDS):
            errors.append("Multi-file tasks must start with a target-resolution todo.")

        missing_in_first_todo = [
            original
            for normalized, original in normalized_task_files.items()
            if normalized not in first_todo_files
        ]

        if missing_in_first_todo:
            errors.append(
                "The first target-resolution todo must explicitly mention every file target: "
                + _format_missing(missing_in_first_todo)
            )

    task_requests_creation = _contains_any(task_text, CREATE_WORDS)

    for index, todo in enumerate(todos):
        content = str(todo.get("content", "")).strip()
        active_form = str(todo.get("activeForm", "")).strip()
        status = str(todo.get("status", "pending")).strip().casefold()
        reason = str(todo.get("reason", "")).strip()

        current_text = _todo_blob(todo)
        current_files = extract_file_mentions(current_text)
        normalized_current_files = {_norm(item): item for item in current_files}

        if not content:
            errors.append(f"Todo #{index}: content is required.")

        if not active_form:
            warnings.append(f"Todo #{index}: activeForm is missing.")

        if status in {"blocked", "failed"} and not reason:
            errors.append(f"Todo #{index}: reason is required when status is '{status}'.")

        if is_multi_file_task and not current_files:
            if _contains_any(current_text, VAGUE_TARGET_REFS):
                errors.append(
                    f"Todo #{index}: vague file references are not allowed in multi-file tasks. "
                    "Name exact targets or refer to resolved targets explicitly."
                )

        if _contains_any(current_text, VERIFICATION_WORDS):
            if not _contains_any(current_text, VERIFICATION_SUCCESS_WORDS):
                errors.append(
                    f"Todo #{index}: verification todo lacks explicit success/failure criteria."
                )

            if task_files:
                missing_verify_targets = [
                    original
                    for normalized, original in normalized_task_files.items()
                    if normalized not in normalized_current_files
                ]

                # Allow verification to refer to "resolved targets" instead of repeating all names.
                refers_to_resolved_targets = (
                    "resolved target" in current_text.casefold()
                    or "resolved path" in current_text.casefold()
                    or "resolved files" in current_text.casefold()
                    or "resolved_path" in current_text.casefold()
                )

                if missing_verify_targets and not refers_to_resolved_targets:
                    errors.append(
                        f"Todo #{index}: verification todo does not explicitly cover all file targets: "
                        + _format_missing(missing_verify_targets)
                    )

        if not task_requests_creation and _contains_any(current_text, CREATE_WORDS):
            warnings.append(
                f"Todo #{index}: mentions creating/generating files, but the task does not explicitly request creation."
            )

    return ValidationResult(
        ok=(not errors if strict else True),
        errors=errors,
        warnings=warnings,
    )