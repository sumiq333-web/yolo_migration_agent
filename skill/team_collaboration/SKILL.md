---
name: team_collaboration
description: Multi-teammate collaboration protocol: task graph, request protocol, message types, artifact submission, review flow, WRITE_APPROVED, and artifact validation retry.
---

# Team Collaboration Protocol

This is the source of truth for multi-teammate collaboration. Load this skill before creating or dispatching tasks that involve engineer, reviewer, or experiment_runner.

## Four-Layer Protocol

| Layer | Question | Carrier |
|-------|----------|---------|
| Task Graph | Who produces what conclusion? Who is blocked by whom? | TaskManager / `.tasks/` |
| Request Protocol | Is this dispatch completed? | RequestStore / `.team/requests.json` |
| Message Type | What communication type is this message? | TEAM_BUS / `.team/inbox/` |
| Artifact Protocol | What structured output was produced? | RequestStore.result |

These four layers must not be mixed.

## Core Principles

1. **Teammates do not pass key artifacts directly to each other.** All artifacts flow back to lead first.

2. **One task = one owner + one explicit conclusion.** Do not assign one task to multiple teammates.

3. **reviewer only reviews `source_artifact` forwarded by lead.** Reviewer does not chat with engineer.

4. **`review_result` completes the request, but does NOT authorize write, does NOT advance the task graph.** Only lead advances task state.

5. **WRITE_APPROVED can only be issued by lead.** It is not a message type, not a review decision — it is an `authorization` field on the implementation task.

6. **Leader controls task lifecycle.** Teammate can return `task_result` / `review_result` / `error`, but cannot directly set `task_set_status(completed/failed)`.

## Message Types

| Type | Direction | Meaning |
|------|-----------|---------|
| `task_request` | lead → teammate | Formal dispatch of a tracked request |
| `task_result` | teammate → lead | Execution/analysis/implementation completed |
| `review_result` | reviewer → lead | Review completed |
| `error` | teammate → lead | Execution failed |
| `rejected` | teammate → lead | Request rejected |
| `expired` | system → lead | Request expired |
| `message` | any → any | Auxiliary communication, does NOT advance protocol |
| `broadcast` | lead → all | Broadcast, does NOT advance protocol |

Main collaboration flow only uses: `task_request`, `task_result`, `review_result`, `error`, `rejected`, `expired`.

## Artifact Output Rule

Any task with **non-empty `conclusion_type`** requires a structured JSON artifact as the final response.

Rules:
1. `artifact_type` must exactly equal `task.conclusion_type`.
2. Natural-language final responses are only allowed when `conclusion_type` is empty.
3. The artifact is stored in `RequestStore.result` after validation passes.
4. Artifact validation failure **fails the request**, not the task.
5. Leader may retry the same task with `allow_retry=true` when the failure is a protocol/artifact formatting issue.
6. Retry dispatch must include the validator error and required artifact schema.
7. Do not retry indefinitely. Repeated artifact validation failure → `task_set_status(failed)` or new corrective task.

## Request Completion Rules

When lead syncs inbox messages to RequestStore:

```
task_result   → complete_request
review_result → complete_request      (even if decision=reject — reviewer completed their work)
error         → fail_request
rejected      → reject_request
expired       → expire_request
```

Note: `review_result` with `decision=reject` still completes the request. Rejection is a business decision inside the artifact, not a request failure.

## WRITE_APPROVED Flow

```
engineer submits change_plan
  → lead validates artifact
  → lead dispatch review task to reviewer (with source_artifact=change_plan)
  → reviewer submit_review (decision=approve/request_changes/reject)
  → lead reads review_result
  → if approve: lead writes WRITE_APPROVED authorization on implementation task, THEN dispatches engineer
  → if request_changes: lead creates revision proposal task
  → if reject: lead marks task failed or scopes down
```

WRITE_APPROVED is NOT implied by reviewer approve. It is a separate, explicit action by lead.

## Artifact Validation Retry

When lead receives a `task_result` but the artifact fails validation:

1. `RequestStore.fail_request(request_id, reason=validator_error)` — request fails, task stays in_progress.
2. Lead decides: retry with `allow_retry=true` (include validator error + required schema), or mark task `failed`.
3. If retry also fails → `task_set_status(failed, reason="Repeatedly failed to produce required artifact")`.
4. Do NOT auto-fail the task. Artifact validation failure is request-level, not task-level.
