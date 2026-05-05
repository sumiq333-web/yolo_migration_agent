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
        skill_summary = ""
        if self.skill_registry is not None:
            skill_summary = self.skill_registry.describe_available().strip()

        body = f"""
# Core instructions
You are a coding agent operating in {self.workdir}.

You MUST use tools to solve the task. Do NOT answer with free-form text.

Rules:
- You MUST first find available skill and read skill if it exists then call the todo tool.
- Do NOT directly answer the question without using tools.
- If you have not used a tool yet, your next message MUST be a tool call.
- Only update todo when progress changes significantly.
""".strip()

        if skill_summary:
            body += f"\n\nSkills available:\n{skill_summary}"

        return body

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