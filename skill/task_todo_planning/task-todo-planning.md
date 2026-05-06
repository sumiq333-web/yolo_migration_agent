---
name: task_todo_planning
description: Create executable, verifiable, and safe todos for coding-agent tasks.
---

# Task Todo Planning

Use this skill whenever you create or update todos for a persistent task.

A todo plan is an execution contract. It must make the task executable, verifiable, and safe to stop when blocked or failed.

## Planning order

For coding tasks, prefer this order:

1. Resolve targets
2. Apply changes
3. Verify results
4. Report outcome

For file modification tasks, do not skip target resolution or verification.

## Rules

1. Extract all concrete targets from the task subject and description before writing todos.
   Targets may include files, directories, modules, functions, configs, commands, or generated artifacts.

2. Every concrete target mentioned in the task subject or description must appear explicitly in the todos.

3. If the task has multiple file targets, the first todo must resolve all of them.
   The target-resolution todo must say whether each target must already exist, what to do with similar candidates, and whether creating missing files is allowed.

4. If a requested file target does not exist and the user did not explicitly ask to create it, the task must be blocked. Do not create a missing file as a workaround.

5. Modification todos must name exact targets or refer to previously resolved targets.
   Avoid vague references such as "the file", "this file", "related config", or "above files" when multiple targets exist.

6. Modification todos should be idempotent when possible.
   Do not duplicate headers, imports, comments, or generated blocks.

7. Verification todos must specify:
   - what is verified
   - which targets are verified
   - which tool or command should be used
   - what counts as success
   - what counts as failure
   - what output should be reported

8. Use `blocked` when the teammate cannot proceed safely.
   Use `failed` when an attempted action or verification fails.
   Blocked or failed todos must include a clear reason.

## Todo shape

Use this basic shape:

```json
{
  "activeForm": "Short action phrase",
  "content": "Executable instruction with targets, constraints, success criteria, and failure behavior.",
  "status": "pending"
}