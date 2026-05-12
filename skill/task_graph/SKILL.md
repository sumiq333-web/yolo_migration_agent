---
name: task_graph
description: Task graph design: how to split work into tasks, set conclusion_type, manage phase and authorization, and enforce state boundaries.
---

# Task Graph

Load this skill when creating or modifying persistent tasks, especially for multi-teammate work.

## Task Splitting Principles

Do not ask "how many tasks?" Ask:
- **Which teammate must produce what conclusion?**
- **Does this conclusion require an independent owner?**
- **Does this conclusion block the next step?**

If the answer is "needs independent owner + blocks next step" → it should be a separate task.

Examples:
```
engineer needs to produce a change_plan   → Task A (owner=engineer)
reviewer needs to review the change_plan   → Task B (owner=reviewer, depends on Task A)
engineer needs to implement with approval  → Task C (owner=engineer, depends on Task B)
```

Task count is derived, not prescribed. A simple migration may have 3 tasks; a complex one may have 5+.

## Task Required Fields

Every task must have:
- `owner` — the single teammate responsible
- `subject` — what this task is about
- `workspace` — project root (set with `task_set_workspace`)
- `todos` — executable steps (set with `task_set_todos`)

If the task must produce a structured artifact, also set:
- `phase` — proposal / review_plan / implementation / review_diff / validation
- `conclusion_type` — change_plan / review_result / implementation_result / validation_result

## Task Status Boundary

| Status | Meaning |
|--------|---------|
| `pending` | Not yet dispatched |
| `in_progress` | Dispatched, teammate is working |
| `blocked` | Cannot proceed, needs external input |
| `completed` | Conclusion produced successfully |
| `failed` | Cannot produce the required conclusion |
| `deleted` | Removed from the graph |

Terminal: `completed`, `failed`, `deleted`
Active (agent_loop continues): `pending`, `in_progress`, `blocked`

## completed / failed Rules

**completed:**
- All todos must be `completed` or `skipped` (no `pending`, `in_progress`, `failed`, `blocked`)
- If `conclusion_type` is non-empty, the artifact must have passed validation

**failed:**
- Must include a `reason`
- All `pending`/`in_progress` todos are auto-set to `skipped` with a reason
- Does NOT require all todos to be resolved first — leader can fail a task at any time

**Do NOT:**
- Reset `todos` after dispatch (blocked by `TaskManager.set_todos` guard)
- Set `completed` while `failed`/`blocked` todos exist (blocked by `TaskManager.set_status` guard)
- Roll a task back to `pending` after it has an owner

## phase and authorization

`phase` describes where this task sits in the collaboration chain:
- `proposal` — engineer analyzes and outputs a plan, NO writes allowed
- `review_plan` / `review_diff` — reviewer evaluates source artifacts
- `implementation` — engineer executes approved changes
- `validation` — experiment_runner or leader runs verification

`authorization` controls what tools are allowed at runtime:
- `write: false` → all mutating tools blocked (proposal phase)
- `write: true` → write_file, edit_file allowed within `allowed_files`

Only lead sets `authorization` via `task_authorize`. Reviewer cannot authorize writes.

## Task Dependencies

Use `blockedBy` / `blocks` to express that one task cannot start before another completes:
```
Task B (review) blockedBy Task A (proposal)
Task C (implementation) blockedBy Task B (review)
```

When lead calls `task_set_status(Task A, completed)`, `Task B`'s `blockedBy` is automatically cleared.
