from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from agent.message_bus import MessageBus
from agent.team_tools import ToolProfile
from agent.agent_runner import AgentRunner, AgentRunnerConfig


class TeammateManager:
    """
    Persistent teammate registry and teammate worker loops.

    This version uses a fixed team:
        engineer
        reviewer
        experiment_runner

    The model can wake teammates and send messages.
    The model cannot create arbitrary new teammates.
    """

    def __init__(
            self,
            *,
            team_dir: Path,
            bus: MessageBus,
            request_store,
            client,
            model: str,
            workdir: Path,
            tool_profiles: dict[str, ToolProfile],
            hooks,

            create_model_response_fn,
            normalize_messages_fn,
            choose_recovery_fn,
            new_recovery_state_fn,
            can_attempt_fn,
            record_attempt_fn,
            apply_continue_recovery_fn,
            apply_compact_recovery_fn,
            apply_backoff_recovery_fn,
            compact_fn,
    ):
        self.dir = Path(team_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.dir / "config.json"
        self.bus = bus
        self.client = client
        self.model = model
        self.workdir = Path(workdir)
        self.tool_profiles = tool_profiles
        self.hooks = hooks

        self.create_model_response_fn = create_model_response_fn
        self.normalize_messages_fn = normalize_messages_fn
        self.choose_recovery_fn = choose_recovery_fn
        self.new_recovery_state_fn = new_recovery_state_fn
        self.can_attempt_fn = can_attempt_fn
        self.record_attempt_fn = record_attempt_fn
        self.apply_continue_recovery_fn = apply_continue_recovery_fn
        self.apply_compact_recovery_fn = apply_compact_recovery_fn
        self.apply_backoff_recovery_fn = apply_backoff_recovery_fn
        self.compact_fn = compact_fn
        self.request_store = request_store
        self.config = self._load_config()
        self.threads: dict[str, threading.Thread] = {}
        self.active_request_ids: dict[str, str] = {}
        self._active_task_ids: dict[str, int] = {}
        self._active_task_contexts: dict[str, dict] = {}
        self._active_todo_indexes: dict[str, int] = {}
        self._finalize_prompt_sent: set[str] = set()
        self._forced_tool_actions: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # Team registry
    # ------------------------------------------------------------------
    def _send_teammate_error(self, actor: str, content: str) -> None:
        self.bus.send(
            sender=actor,
            to="lead",
            content=content,
            msg_type="error",
            extra=self._request_extra_for(actor),
        )

    def _report_tool_errors(self, name: str, executions) -> None:
        """
        把工具错误报告给 lead。

        注意：
        - write_file/edit_file 这类 mutating file failure 会由 forced failure finalization
          统一执行 todo_update(failed) + send_message(error)。
        - 这里跳过它们，避免 lead 收到两条重复 error。
        """
        payload = []

        for execution in executions:
            if self._is_mutating_file_failure(execution):
                continue

            payload.append(
                {
                    "tool": execution.tool_name,
                    "status": execution.status,
                    "reason": execution.reason,
                }
            )

        if not payload:
            return

        self.bus.send(
            sender=name,
            to="lead",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            msg_type="error",
            extra=self._request_extra_for(name),
        )

    def _send_text_response_to_lead(self, name: str, member: dict, text: str) -> None:
        self.bus.send(
            sender=name,
            to="lead",
            content=text,
            msg_type=self._default_result_type(member),
            extra=self._request_extra_for(name),
        )

    STATE_SYNC_TOOLS = {"todo_update"}
    MUTATING_FILE_TOOLS = {"write_file", "edit_file"}

    def _is_state_sync_failure(self, execution) -> bool:
        return (
                execution.tool_name in self.STATE_SYNC_TOOLS
                and execution.status != "executed"
        )

    def _is_mutating_file_failure(self, execution) -> bool:
        return (
                execution.tool_name in self.MUTATING_FILE_TOOLS
                and execution.status != "executed"
        )



    def _should_stop_after_teammate_tools(self, actor: str, executions) -> bool:
        for execution in executions:
            if execution.tool_name == "send_message" and execution.status == "executed":
                return True

            if self._is_state_sync_failure(execution):
                self._send_teammate_error(
                    actor,
                    (
                        "Teammate stopped after state-sync tool failure. "
                        f"tool={execution.tool_name}, status={execution.status}, reason={execution.reason}"
                    ),
                )
                print(
                    "Stopping after state-sync failure: "
                    f"{execution.tool_name} status={execution.status} reason={execution.reason}"
                )
                return True

            if self._is_mutating_file_failure(execution):
                self._schedule_forced_failure_finalization(actor, execution)
                print(
                    "Scheduling forced failure finalization after mutating file tool failure: "
                    f"{execution.tool_name} status={execution.status} reason={execution.reason}"
                )
                return True

        return False

    def reload_config(self) -> str:
        self.config = self._load_config()
        return "Team config reloaded."

    def _is_thread_alive(self, name: str) -> bool:
        thread = self.threads.get(name)
        return thread is not None and thread.is_alive()

    def dispatch(
            self,
            *,
            to: str,
            content: str,
            msg_type: str = "task_request",
    ) -> str:
        member = self._find_member(to)

        if member is None:
            return f"Error: teammate '{to}' not found."

        if msg_type != "task_request":
            return f"Error: unsupported dispatch msg_type: {msg_type}"

        kind = self._default_request_kind()
        protocol = self._protocol_name()
        existing = self.request_store.find_open_request(
            to=to,
            content=content,
            kind=kind,
        )

        if existing is not None:
            request_id = existing["request_id"]

            if member.get("status") == "working" and self._is_thread_alive(to):
                return (
                    f"Duplicate dispatch ignored. Existing request_id={request_id}. "
                    f"Teammate is already working."
                )

            wake_result = self.wake(to)

            return (
                f"Duplicate dispatch ignored. Existing request_id={request_id}. "
                f"{wake_result}"
            )
        request = self.request_store.create_request(
            kind=kind,
            sender="lead",
            to=to,
            content=content,
            payload={
                "msg_type": msg_type,
                "protocol": protocol,
                "kind": kind,
            },
        )

        request_id = request["request_id"]

        send_result = self.bus.send(
            sender="lead",
            to=to,
            content=content,
            msg_type=msg_type,
            extra={
                "request_id": request_id,
                "protocol": protocol,
                "kind": kind,
            },
        )

        if send_result.startswith("Error:"):
            self.request_store.fail_request(
                request_id=request_id,
                reason=send_result,
            )
            return f"Error: dispatch failed for {request_id}. {send_result}"

        if member.get("status") == "working" and self._is_thread_alive(to):
            return (
                f"Dispatched {msg_type} to {to} "
                f"with request_id={request_id}. "
                f"Teammate is already working."
            )

        wake_result = self.wake(to)

        return (
            f"Dispatched {msg_type} to {to} "
            f"with request_id={request_id}. "
            f"{wake_result}"
        )

    #
    # def _request_kind_from_msg_type(self, msg_type: str) -> str:
    #     mapping = {
    #         "task_request": "assignment",
    #         "review_request": "review",
    #         "experiment_request": "experiment",
    #         "message": "message",
    #     }
    #     return mapping.get(msg_type, "message")

    def ensure_default_team(self) -> str:
        defaults = [
            {
                "name": "engineer",
                "role": "code engineer",
                "tool_profile": "engineer",
                "instructions": (
                    "Directly responsible for all code-related work: repository inspection, "
                    "model architecture analysis, migration planning, change_plan artifacts, "
                    "and implementation after lead issues WRITE_APPROVED. "
                    "If task.conclusion_type=change_plan, the final action MUST be submit_change_plan; "
                    "do not finish with natural language or manually composed JSON. "
                    "Do not write code unless the task phase and authorization allow it. "
                    "Do not decide task status."
                ),
            },
            {
                "name": "reviewer",
                "role": "technical reviewer",
                "tool_profile": "reviewer",
                "instructions": (
                    "Independently review source_artifacts forwarded by lead, including change_plan "
                    "artifacts and implementation results. Submit review_result with decision=approve, "
                    "request_changes, or reject. Do not write code, do not authorize writes, and do not "
                    "decide task status."
                ),
            },
            {
                "name": "experiment_runner",
                "role": "validation executor",
                "tool_profile": "experiment_runner",
                "instructions": (
                    "Run or summarize validation work assigned by lead, including tests, training smoke tests, "
                    "model build checks, command verification, logs, metrics, and failure reasons. "
                    "If task.conclusion_type=validation_result, submit a valid validation_result artifact. "
                    "Do not change source files unless explicitly authorized by lead."
                ),
            },
        ]

        created = []
        updated = []

        for item in defaults:
            member = self._find_member(item["name"])

            if member is None:
                self.config["members"].append(
                    {
                        **item,
                        "status": "idle",
                        "created_at": time.time(),
                        "updated_at": time.time(),
                    }
                )
                created.append(item["name"])
                continue

            changed = False

            for key in ("role", "tool_profile", "instructions"):
                if member.get(key) != item[key]:
                    member[key] = item[key]
                    changed = True

            if member.get("status") not in {"idle", "working", "shutdown"}:
                member["status"] = "idle"
                changed = True

            if changed:
                member["updated_at"] = time.time()
                updated.append(item["name"])

        self._save_config()

        parts = []
        if created:
            parts.append(f"created={created}")
        if updated:
            parts.append(f"updated={updated}")

        if not parts:
            return "Default migration team already exists."

        return "Default migration team initialized: " + ", ".join(parts)

    def list_all(self) -> str:
        members = self.config.get("members", [])

        if not members:
            return "No teammates."

        lines = [f"Team: {self.config.get('team_name', 'migration_team')}"]

        for member in members:
            lines.append(
                f"- {member['name']} "
                f"({member['role']}, profile={member.get('tool_profile', '')}): "
                f"{member['status']}"
            )

        return "\n".join(lines)

    def member_names(self) -> list[str]:
        return [
            member["name"]
            for member in self.config.get("members", [])
            if member.get("status") != "shutdown"
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def wake(self, name: str) -> str:
        name = name.strip()
        member = self._find_member(name)

        if member is None:
            return f"Error: teammate '{name}' not found."

        if member.get("status") == "working":
            if self._is_thread_alive(name):
                return f"Teammate '{name}' is already working."

            member["status"] = "idle"
            member["previous_status"] = "working"
            member["recovered_at"] = time.time()
            member["updated_at"] = time.time()
            self._save_config()

        if member.get("status") == "shutdown":
            member["status"] = "idle"

        member["status"] = "working"
        member["updated_at"] = time.time()
        self._save_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name,),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()

        return f"Woke teammate '{name}'."

    def shutdown(self, name: str) -> str:
        name = name.strip()
        member = self._find_member(name)

        if member is None:
            return f"Error: teammate '{name}' not found."

        member["status"] = "shutdown"
        member["updated_at"] = time.time()
        self._save_config()

        self.bus.send(
            sender="lead",
            to=name,
            content="Shutdown requested. Finish current step and stop.",
            msg_type="shutdown_request",
        )

        return f"Shutdown requested for teammate '{name}'."

    # ------------------------------------------------------------------
    # Teammate loop
    # ------------------------------------------------------------------


    def _is_active_change_plan_task(self, actor: str) -> bool:
        ctx = self._active_task_contexts.get(actor, {}) or {}
        return str(ctx.get("conclusion_type", "")).strip() == "change_plan"

    def _build_change_plan_finalize_prompt(self, actor: str) -> str:
        ctx = self._active_task_contexts.get(actor, {}) or {}
        subject = str(ctx.get("subject", "the assigned proposal task"))
        return (
            "<system-reminder>\n"
            f"You are working on a tracked change_plan task: {subject}.\n"
            "You have already gathered evidence or reached the recovery limit. Stop collecting more context.\n"
            "Your next action MUST be exactly one of:\n"
            "1. Call submit_change_plan with the best plan you can produce from the evidence already gathered.\n"
            "2. Call send_message with msg_type=error and a concise blocking reason.\n"
            "Do not call scan_yolo_project, read_file, read_code, run_shell, run_python, or todo_update again before choosing one of those final actions.\n"
            "</system-reminder>"
        )

    def _teammate_loop(self, name: str) -> None:
        member = self._find_member(name)
        if member is None:
            return

        messages: list[dict] = []

        try:
            while True:
                member = self._find_member(name)
                if member is None:
                    break

                if member.get("status") == "shutdown":
                    break

                inbox = self.bus.read_inbox(name)

                if not inbox and not messages:
                    break

                for message in inbox:
                    request_id = message.get("request_id")
                    if request_id:
                        self.active_request_ids[name] = request_id

                    if message.get("type") == "shutdown_request":
                        self.bus.send(
                            sender=name,
                            to="lead",
                            content="Shutdown acknowledged.",
                            msg_type="shutdown_response",
                        )
                        self._set_status(name, "shutdown")
                        return

                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "<teammate-inbox-message>\n"
                                f"{json.dumps(message, ensure_ascii=False, indent=2)}\n"
                                "</teammate-inbox-message>"
                            ),
                        }
                    )

                self._activate_task_workspace(inbox, name)

                runner = AgentRunner(
                    AgentRunnerConfig(
                        name=name,
                        tools=self._tools_for_member(member),
                        tool_handlers=self._handlers_for_member(member),
                        hooks=self.hooks,
                        system_prompt_fn=lambda member=member: self._build_teammate_system_prompt(member),

                        create_model_response_fn=self.create_model_response_fn,
                        normalize_messages_fn=self.normalize_messages_fn,
                        choose_recovery_fn=self.choose_recovery_fn,
                        new_recovery_state_fn=self.new_recovery_state_fn,
                        can_attempt_fn=self.can_attempt_fn,
                        record_attempt_fn=self.record_attempt_fn,
                        apply_continue_recovery_fn=self.apply_continue_recovery_fn,
                        apply_compact_recovery_fn=self.apply_compact_recovery_fn,
                        apply_backoff_recovery_fn=self.apply_backoff_recovery_fn,
                        compact_fn=self.compact_fn,

                        max_tokens=4000,
                        tool_actor=name,
                        prompt_dirty_fn=None,
                        log_fn=print,

                        stop_after_tool_fn=lambda executions, name=name: (
                            self._should_stop_after_teammate_tools(name, executions)
                        ),
                        on_text_response_fn=lambda text, name=name, member=member: (
                            self._send_text_response_to_lead(name, member, text)
                        ),
                        on_tool_errors_fn=lambda executions, name=name: (
                            self._report_tool_errors(name, executions)
                        ),
                    )
                )

                runner_result = runner.run(messages)
                messages = runner_result.messages

                # 先执行 forced failure finalization。
                # 如果 edit_file/write_file 失败，这里会强制：
                # 1. todo_update(status="failed", reason=...)
                # 2. send_message(msg_type="error")
                self._drain_forced_tool_actions(name, member)

                if runner_result.stop_reason in ("stopped_after_tool",):
                    break

                # teammate 如果直接自然语言回复，AgentRunner 已经通过 on_text_response_fn
                # 把文本转发给 lead。这里应该停止，不能继续空转。
                if runner_result.has_text_response:
                    break

                if "exhausted" in runner_result.stop_reason or runner_result.stop_reason in ("empty_response",):
                    # change_plan proposal tasks often fail at the finalization boundary: the model keeps
                    # reading/scanning but never submits the artifact. Give it one forced finalization turn.
                    if self._is_active_change_plan_task(name) and name not in self._finalize_prompt_sent:
                        self._finalize_prompt_sent.add(name)
                        messages.append({
                            "role": "user",
                            "content": self._build_change_plan_finalize_prompt(name),
                        })
                        continue

                    self.bus.send(
                        sender=name,
                        to="lead",
                        content=f"Teammate loop stopped: {runner_result.stop_reason}",
                        msg_type="error",
                        extra=self._request_extra_for(name),
                    )
                    break

        except Exception as e:
            self.bus.send(
                sender=name,
                to="lead",
                content=f"Teammate loop error: {e}",
                msg_type="error",
            )


        finally:

            self.active_request_ids.pop(name, None)
            self._active_task_ids.pop(name, None)
            self._active_task_contexts.pop(name, None)
            self._finalize_prompt_sent.discard(name)
            self._active_todo_indexes.pop(name, None)
            self._forced_tool_actions.pop(name, None)

            member = self._find_member(name)

            if member and member.get("status") != "shutdown":
                self._set_status(name, "idle")

    # ------------------------------------------------------------------
    # Tool routing
    # ------------------------------------------------------------------

    def _tools_for_member(self, member: dict) -> list[dict]:
        profile_name = member.get("tool_profile", "")
        profile = self.tool_profiles.get(profile_name)

        if profile is None:
            return []

        return profile.tools

    def _wrap_handler_for_runtime_tracking(self, member: dict, tool_name: str, handler):
        def wrapped(sender: str, **kw) -> str:
            output = handler(sender, **kw)

            if tool_name == "todo_update":
                try:
                    index = int(kw["index"])
                    status = str(kw.get("status", "")).strip().lower()

                    if status == "in_progress":
                        self._active_todo_indexes[sender] = index

                    elif status in {"completed", "failed", "blocked", "skipped"}:
                        if self._active_todo_indexes.get(sender) == index:
                            self._active_todo_indexes.pop(sender, None)

                except Exception:
                    pass

            return output

        return wrapped

    def _handlers_for_member(self, member: dict) -> dict:
        profile_name = member.get("tool_profile", "")
        profile = self.tool_profiles.get(profile_name)

        if profile is None:
            return {}

        handlers = dict(profile.handlers)

        if "send_message" in handlers:
            handlers["send_message"] = self._build_send_message_handler(member)

        return {
            tool_name: self._wrap_handler_for_runtime_tracking(member, tool_name, handler)
            for tool_name, handler in handlers.items()
        }

    def _build_send_message_handler(self, member: dict):
        def send_message(sender: str, **kw) -> str:
            content = kw.get("content", "")
            to = kw.get("to", "lead")

            extra = self._request_extra_for(sender)
            has_active_request = bool(extra)

            msg_type = kw.get("msg_type") or self._default_result_type(member)

            if has_active_request and msg_type == "message":
                msg_type = self._default_result_type(member)

            return self.bus.send(
                sender=sender,
                to=to,
                content=content,
                msg_type=msg_type,
                extra=extra,
            )

        return send_message



    def _drain_forced_tool_actions(self, actor: str, member: dict) -> None:
        actions = self._forced_tool_actions.pop(actor, [])

        if not actions:
            return

        handlers = self._handlers_for_member(member)

        for action in actions:
            tool_name = action["tool_name"]
            args = action.get("args", {})

            handler = handlers.get(tool_name)
            if handler is None:
                self._send_teammate_error(
                    actor,
                    f"Forced tool action failed: tool '{tool_name}' is not available.",
                )
                continue

            try:
                output = str(handler(actor, **args))
                print(f"[{actor}] forced {tool_name}: {output[:500]}")

                if output.lstrip().startswith("Error:"):
                    self._send_teammate_error(
                        actor,
                        (
                            "Forced tool action returned an error. "
                            f"tool={tool_name}, output={output}"
                        ),
                    )

            except Exception as e:
                self._send_teammate_error(
                    actor,
                    f"Forced tool action failed: tool={tool_name}, error={e}",
                )

    def _exec_tool_for_member(self, member: dict, tool_name: str, args: dict) -> str:
        profile_name = member.get("tool_profile", "")
        profile = self.tool_profiles.get(profile_name)

        if profile is None:
            return f"Error: unknown tool profile '{profile_name}'."

        handler = profile.handlers.get(tool_name)

        if handler is None:
            return f"Error: tool '{tool_name}' is not allowed for profile '{profile_name}'."

        try:
            return str(handler(member["name"], **args))
        except Exception as e:
            return f"Error: {e}"

    def _schedule_forced_failure_finalization(self, actor: str, execution) -> None:
        todo_index = self._active_todo_indexes.get(actor)

        if todo_index is None:
            self._forced_tool_actions.setdefault(actor, []).append(
                {
                    "tool_name": "send_message",
                    "args": {
                        "content": (
                            "Mutating file tool failed, but no active in_progress todo index "
                            "was recorded for forced failure finalization. "
                            f"tool={execution.tool_name}, status={execution.status}, reason={execution.reason}"
                        ),
                        "msg_type": "error",
                    },
                }
            )
            return

        reason = (
            f"Mutating file tool failed during teammate execution. "
            f"tool={execution.tool_name}; status={execution.status}; reason={execution.reason}"
        )

        self._forced_tool_actions.setdefault(actor, []).extend(
            [
                {
                    "tool_name": "todo_update",
                    "args": {
                        "index": todo_index,
                        "status": "failed",
                        "reason": reason,
                    },
                },
                {
                    "tool_name": "send_message",
                    "args": {
                        "content": reason,
                        "msg_type": "error",
                    },
                },
            ]
        )

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------

    def _build_teammate_system_prompt(self, member: dict) -> str:
        return f"""
    You are '{member['name']}', role: {member['role']}, at {self.workdir}.

    Standing instructions:
    {member.get('instructions', '').strip()}

    You are a persistent teammate with your own inbox.
    Use your available tools to complete assigned inbox messages.

    Protocol rules:
    - Messages with request_id are tracked protocol messages.
    - The dispatch message may contain a <task-context> block with a "todos" list. This is your work plan. Work through each todo item in order.
    - Use todo_update to mark each todo item: set in_progress before starting, then completed when finished. If a step cannot be done, set blocked or failed with a reason.
    - If you complete ALL todos for the task, send the final answer to lead with msg_type=task_result. Sending task_result ends this work session.
    - If you cannot complete a tracked assignment, send msg_type=error and include the failure reason.
    - Do not use msg_type=message for a tracked request final response.
    - Use msg_type=message only for ordinary non-tracked communication or to ask the lead a question mid-work.
    - Do not invent request_id. The runtime attaches request_id automatically when needed.

    Tool behavior notes:
    - run_shell on Windows runs cmd.exe. Use dir / type / echo / python. Avoid cat, grep, heredoc.
    - write_file requires create=True to make a new file. Otherwise the file must already exist.
    - When validating a YOLO model YAML, it must be a dict with keys: nc, backbone, head. Empty/comment-only files are NOT valid.
    """.strip()

    def _default_result_type(self, member: dict) -> str:
        profile = member.get("tool_profile")

        if profile == "reviewer":
            return "review_result"

        if profile == "experiment_runner":
            return "experiment_result"

        return "task_result"

    def _extract_text(self, content) -> str:
        parts = []

        for block in content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {
                "team_name": "migration_team",
                "members": [],
            }

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "team_name": "migration_team",
                "members": [],
            }

        data.setdefault("team_name", "migration_team")
        data.setdefault("members", [])

        for member in data["members"]:
            if "prompt" in member and "instructions" not in member:
                member["instructions"] = member.pop("prompt")
            member.setdefault("status", "idle")
            member.setdefault("tool_profile", member.get("name", "engineer"))

        return data

    def _save_config(self) -> None:
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _find_member(self, name: str) -> dict | None:
        for member in self.config.get("members", []):
            if member.get("name") == name:
                return member
        return None

    def _set_status(self, name: str, status: str) -> None:
        member = self._find_member(name)

        if member is None:
            return

        member["status"] = status
        member["updated_at"] = time.time()
        self._save_config()

    def _protocol_name(self) -> str:
        return "team_request_v1"

    def _activate_task_workspace(self, inbox: list[dict], actor: str) -> None:
        import re
        for msg in inbox:
            content = msg.get("content", "")
            m = re.search(r"<task-context>(.*?)</task-context>", content, re.DOTALL)
            if not m:
                continue
            try:
                ctx = json.loads(m.group(1))
                workspace = ctx.get("workspace", "")
                task_id = ctx.get("task_id")
                if task_id is not None:
                    self._active_task_ids[actor] = int(task_id)
                    self._active_task_contexts[actor] = ctx
                if workspace:
                    from tools.yolo_tools import set_yolo_workspace
                    set_yolo_workspace(workspace)
                    print(f"  [{actor}] workspace activated: {workspace}")
            except (json.JSONDecodeError, Exception):
                pass

    def get_active_task_id(self, name: str) -> int | None:
        return self._active_task_ids.get(name)

    def _default_request_kind(self) -> str:
        return "assignment"

    def _request_extra_for(self, name: str) -> dict:
        """
        给 teammate -> lead 的消息附加协议上下文。

        request_id 用于 RequestStore 同步；
        task_id 用于 lead 判断哪个 task 需要 retry / failed。
        """
        request_id = self.active_request_ids.get(name)
        if not request_id:
            return {}

        extra = {
            "request_id": request_id,
            "protocol": self._protocol_name(),
            "kind": self._default_request_kind(),
        }

        task_id = self._active_task_ids.get(name)
        if task_id is not None:
            extra["task_id"] = task_id

        return extra