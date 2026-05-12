---
name: todo_contract
description: Todo design rules: executable, verifiable, low-ambiguity todos. Multi-file target resolution, failed/blocked reasons, and validator enforcement.
---

# Todo Contract

Load this skill when setting or updating task todos. The todo plan is an execution contract — it must make the task executable, verifiable, and safe to stop when blocked or failed.

## Planning Order

For coding tasks:
1. Resolve targets
2. Apply changes
3. Verify results
4. Report outcome

Do not skip target resolution or verification for file modification tasks.

## Todo Shape

Every todo item:
```json
{
  "content": "Executable instruction with target, action, success criteria, and failure behavior",
  "activeForm": "Short action phrase",
  "status": "pending"
}
```

## Rules

### 1. Files in subject must appear in todos
Every file path mentioned in `subject` or `description` must appear in at least one todo's `content`.

### 2. Multi-file tasks: first todo resolves all targets
If the task touches multiple files, the first todo must explicitly name and resolve every target file.

### 3. No vague references
Forbidden in multi-file tasks: "the file", "this file", "above files", "related config", "target file", "文件头部".
Every todo must name exact targets or reference resolved targets.

### 4. Verification todos must specify success/failure criteria
Every verification todo must include: what is verified, which tool/command, what counts as success, what counts as failure.

### 5. blocked / failed todos require reason
`todo_update` with status `blocked` or `failed` MUST include a `reason` explaining why.

Use `blocked` when the teammate cannot proceed safely (missing tool, missing file, insufficient permission).
Use `failed` when an attempted action failed (tool_error, verification failure, command non-zero exit).
Use `skipped` when lead or reviewer decides the step is unnecessary.

### 6. No implicit file creation
If a todo mentions creating/generating a file but the task does not explicitly request creation, this is a warning.
If a target file does not exist and the user did not ask to create it, the todo must be `blocked`, not silently turned into "create new file".

### 7. Idempotent modifications
Modification todos should avoid duplicating headers, imports, comments, or generated blocks.

## Teammate Scope
- Teammate can only update todos on their own assigned task (owner check).
- `task_id` is set automatically by the runtime — do not pass it.
- Use `todo_update(index, status, reason="")`.
- A task can only be `completed` when all todos are `completed` or `skipped`.
- `failed` tasks auto-skip remaining `pending`/`in_progress` todos.

## Validator
The `task_plan_validator` enforces rules 1-6 at the code level. `task_set_todos` will reject invalid todo plans before dispatch.
