#!/usr/bin/env python3
# s10_system_prompt.py
"""
Lightweight expandable system prompt builder.

Design goals:
- keep the current single-file section-builder style
- avoid one giant hardcoded blob
- separate stable and dynamic sections
- cache stable prefix
- make new sections easy to add
"""

from __future__ import annotations

import datetime
import os
import platform
import re
from pathlib import Path


DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="


class SystemPromptBuilder:
    """
    Assemble the system prompt from clear sections.

    This is intentionally lightweight:
    no provider framework yet, but enough structure to scale cleanly.
    """

    def __init__(
        self,
        *,
        workdir: Path,
        tools: list[dict],
        model_name: str,
        skill_registry=None,
        memory_store=None,
    ):
        self.workdir = Path(workdir)
        self.tools = tools or []
        self.model_name = model_name
        self.skill_registry = skill_registry
        self.memory_store = memory_store

        self.skills_dir = self.workdir / "skills"
        self.memory_dir = self.workdir / ".memory"

        self._cached_stable: str | None = None
        self._stable_dirty: bool = True

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def mark_stable_dirty(self) -> None:
        self._stable_dirty = True

    def build(self) -> str:
        stable = self.build_stable()
        dynamic = self.build_dynamic()

        parts: list[str] = []
        if stable:
            parts.append(stable)
        parts.append(DYNAMIC_BOUNDARY)
        if dynamic:
            parts.append(dynamic)

        return "\n\n".join(parts).strip()

    def build_stable(self) -> str:
        if self._cached_stable is not None and not self._stable_dirty:
            return self._cached_stable

        sections: list[str] = []
        for builder in self._stable_section_builders():
            text = builder()
            if text and text.strip():
                sections.append(text.strip())

        self._cached_stable = "\n\n".join(sections).strip()
        self._stable_dirty = False
        return self._cached_stable

    def build_dynamic(self) -> str:
        sections: list[str] = []
        for builder in self._dynamic_section_builders():
            text = builder()
            if text and text.strip():
                sections.append(text.strip())
        return "\n\n".join(sections).strip()

    def list_section_names(self) -> list[str]:
        names = [fn.__name__ for fn in self._stable_section_builders()]
        names.append(DYNAMIC_BOUNDARY)
        names.extend(fn.__name__ for fn in self._dynamic_section_builders())
        return names

    # ------------------------------------------------------------
    # Section registry
    # ------------------------------------------------------------

    def _stable_section_builders(self):
        return [
            self._build_core,
            self._build_tool_listing,
            self._build_skill_listing,
            self._build_memory_section,
            self._build_instruction_chain,
        ]

    def _dynamic_section_builders(self):
        return [
            self._build_dynamic_context,
        ]

    # ------------------------------------------------------------
    # Stable sections
    # ------------------------------------------------------------

    def _build_core(self) -> str:
        return f"""
    # Core instructions

You are the lead agent in a code project. You are the sole decision-maker and task dispatcher for a fixed multi-agent code collaboration team.
You must solve tasks using the team. During execution, you must not bypass tools and answer the user directly with free-form text. The final result may only be reported to the user after the task graph has been resolved. Your first action must be a tool call.

Mandatory workflow：
For every substantive user request, you must create or reuse a persistent task graph. Simple requests may create one task. Complex requests must be split into multiple tasks.
You can build a relatively accurate task and todo by reading the skill file.
Before calling scan_yolo_project, read_file, read_code, run_shell, run_python, write_file, edit_file, background_run, or dispatch_to_teammate, you must first create or reuse a persistent task graph.
Before task_create_graph, you may only:
1. use load_skill to read relevant skills;
2. call set_yolo_workspace if the user explicitly provided a workspace path.

Complex coding tasks must follow this order:
1. load_skill("task_graph"), then use task_create_graph to create a persistent task graph;
2. load_skill("todo_contract"), then use task_set_todos to create executable and verifiable todos for each task;
3. load_skill("team_collaboration"), then dispatch tasks according to the team collaboration protocol.
Any task that accesses files or a repository must call task_set_workspace before dispatch.
Do not use the session todo as a substitute for the persistent task graph.

Task / todo / artifact rules：

-Each task must have exactly one owner and one explicit conclusion. If a task requires a structured output, it must set conclusion_type.
-An artifact is a structured JSON output submitted by a teammate, such as change_plan, review_result, implementation_result, or validation_result.
-If task.conclusion_type is non-empty, the teammate’s final result must be a JSON artifact, and artifact_type must exactly match conclusion_type. Natural-language final results are only allowed when conclusion_type is empty.
-Artifact validation failure means the request failed. It does not automatically mean the task failed. You must decide whether to retry with allow_retry=true, create a corrective task, or set the task status to failed with task_set_status.
-After dispatch, do not use task_set_todos to rewrite task evidence or bypass failed todos.

Team members：
- lead: yourself. You understand the user goal, define task boundaries, create the persistent task graph, assign tasks, collect artifacts, issue WRITE_APPROVED, decide task status, and report the final result to the user.
- engineer: the programmer. Responsible for code reading, code analysis, change plans, and implementation. The engineer may write code only after receiving WRITE_APPROVED from lead.
- reviewer: the independent reviewer. Only reviews source_artifact forwarded by lead and submits review_result. The reviewer must not write code and must not authorize write operations.
- experiment_runner: the validation executor. Responsible for running or summarizing tests, training smoke tests, command verification, and logs.

Teammate communication：
-Communicate with teammates through requests. You have your own lead inbox. After dispatching a task, wait for the teammate to complete the task and reply.
-Never use run_shell, run_python, background_run, schedule_after, cron_create, sleep, timeout, ping, or any command to wait for teammate responses.
-After dispatch_to_teammate, stop the current tool sequence and let the runtime team message loop wait for teammate messages. Use read_inbox only after control returns to lead.

 Authorization and dispatch rules：
-You must complete tasks through team collaboration. Do not bypass teammates and complete all work by yourself.
-Tasks that require code modification must first have the engineer produce a change_plan, then have the reviewer review it. Only after the reviewer approves may you issue WRITE_APPROVED to the implementation task.
-WRITE_APPROVED can only be issued by lead. It is not a message type and not the reviewer’s decision. It is the authorization field of the implementation task.
-Before dispatching an implementation task that may modify files or execute code-changing commands, ask the user for confirmation. Explain what will be changed, which files will be affected, and how verification will be performed. Then wait for user approval.

##Correcting user mistakes：
Sometimes the user’s description may be inaccurate. If teammates, file contents, or tool results provide sufficient evidence, you may correct the task goal or replan the task graph. Any correction must preserve the user’s original intent, and the final report must explain the discrepancy, evidence, and reason for the adjustment.

## Tool authorization note
For dispatched teammate tasks, do not use any tool unless the current task phase and authorization allow it. In proposal or review phases, use scan_yolo_project, read_file, and read_code instead.
    """.strip()

    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""

        lines = ["# Available tools"]
        for tool in self.tools:
            props = tool.get("input_schema", {}).get("properties", {})
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}({params}): {tool['description']}")
        return "\n".join(lines)



    def _build_skill_listing(self) -> str:
        """
        Prefer SkillRegistry summary if available.
        Fall back to scanning skills/ directory.
        """
        if self.skill_registry is not None:
            text = self.skill_registry.describe_available().strip()
            if text:
                return "# Available skills\n" + text

        if not self.skills_dir.exists():
            return ""

        skills = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            text = skill_md.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                continue

            meta = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()

            name = meta.get("name", skill_dir.name)
            desc = meta.get("description", "")
            skills.append(f"- {name}: {desc}")

        if not skills:
            return ""
        return "# Available skills\n" + "\n".join(skills)

    def _build_memory_section(self) -> str:
        """
        Prefer memory_store summary if available.
        Fall back to empty if memory system is absent.
        """
        if self.memory_store is not None:
            summary = self.memory_store.render_memory_summary()
            if summary.strip():
                return "# Persistent memory\n" + summary

        return ""

    def _build_instruction_chain(self) -> str:
        """
        A generalized replacement for hardcoding only CLAUDE.md.

        Current supported sources:
        - ~/.claude/CLAUDE.md
        - <project-root>/CLAUDE.md

        If these files do not exist, this section is simply omitted.
        """
        sources: list[tuple[str, Path]] = [
            ("user global (~/.claude/CLAUDE.md)", Path.home() / ".claude" / "CLAUDE.md"),
            ("project root (CLAUDE.md)", self.workdir / "CLAUDE.md"),
        ]

        parts = []
        for label, path in sources:
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            parts.append(f"## From {label}\n{content}")

        if not parts:
            return ""

        return "# Standing instructions\n\n" + "\n\n".join(parts)

    # ------------------------------------------------------------
    # Dynamic sections
    # ------------------------------------------------------------

    def _build_dynamic_context(self) -> str:
        lines = [
            f"Current date: {datetime.date.today().isoformat()}",
            f"Working directory: {self.workdir}",
            f"Model: {self.model_name}",
            f"Platform: {platform.system()}",
        ]
        return "# Dynamic context\n" + "\n".join(lines)


def build_system_reminder(extra: str | None = None) -> dict | None:
    """
    Build a per-turn reminder outside the stable system prompt.
    """
    parts = []
    if extra:
        parts.append(extra.strip())
    if not parts:
        return None

    content = "<system-reminder>\n" + "\n".join(parts) + "\n</system-reminder>"
    return {"role": "user", "content": content}