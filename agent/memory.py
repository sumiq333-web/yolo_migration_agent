from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict, Literal


MemoryType = Literal["user", "feedback", "project", "reference"]


class MemoryItem(TypedDict):
    name: str
    type: MemoryType
    description: str
    content: str


class MemoryStore:
    ALLOWED_TYPES = ("user", "feedback", "project", "reference")
    MAX_ITEMS = 30
    MAX_NAME_LEN = 64
    MAX_DESC_LEN = 120
    MAX_CONTENT_LEN = 500
    MAX_SUMMARY_ITEMS = 8

    BANNED_PATTERNS = [
        r"api[_\-\s]?key",
        r"access[_\-\s]?token",
        r"secret",
        r"password",
        r"passwd",
        r"private[_\-\s]?key",
        r"current branch",
        r"open pr",
        r"pull request #?\d+",
        r"\btodo\b",
        r"temporary task",
    ]

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.memory_dir = self.workspace / ".memory"
        self.memory_file = self.memory_dir / "MEMORY.json"
        self.memories: list[MemoryItem] = []
        self.version = 0
        self._loaded = False

    def load_memories(self, force: bool = False) -> list[MemoryItem]:
        if self._loaded and not force:
            return self.memories

        self.memory_dir.mkdir(parents=True, exist_ok=True)

        if not self.memory_file.exists():
            self.memories = []
            self._loaded = True
            return self.memories

        try:
            raw = json.loads(self.memory_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.memories = []
            self._loaded = True
            return self.memories

        cleaned: list[MemoryItem] = []
        if isinstance(raw, list):
            for item in raw:
                normalized = self._normalize_loaded_item(item)
                if normalized is not None:
                    cleaned.append(normalized)

        self.memories = cleaned[: self.MAX_ITEMS]
        self._loaded = True
        return self.memories

    def save_memory(self, name: str, mem_type: str, description: str, content: str) -> str:
        self.load_memories()
        normalized_name = self._normalize_name(name)

        self._validate(
            name=normalized_name,
            mem_type=mem_type,
            description=description,
            content=content,
        )

        item: MemoryItem = {
            "name": normalized_name,
            "type": mem_type,  # type: ignore[assignment]
            "description": description.strip(),
            "content": content.strip(),
        }

        replaced = False
        changed = False

        for idx, existing in enumerate(self.memories):
            if existing["name"] == normalized_name:
                replaced = True
                if existing != item:
                    self.memories[idx] = item
                    changed = True
                break

        if not replaced:
            if len(self.memories) >= self.MAX_ITEMS:
                return f"Error: memory limit reached ({self.MAX_ITEMS})"
            self.memories.append(item)
            changed = True

        if changed:
            self._write_file()
            self.version += 1

        return f"Saved memory '{normalized_name}' [{mem_type}]"

    def get_memory(self, name: str) -> MemoryItem | None:
        self.load_memories()
        normalized_name = self._normalize_name(name)
        for item in self.memories:
            if item["name"] == normalized_name:
                return item
        return None

    def list_memories(self) -> list[MemoryItem]:
        self.load_memories()
        return list(self.memories)

    def delete_memory(self, name: str) -> str:
        self.load_memories()
        normalized_name = self._normalize_name(name)
        original_count = len(self.memories)
        self.memories = [m for m in self.memories if m["name"] != normalized_name]

        if len(self.memories) == original_count:
            return f"Error: memory '{normalized_name}' not found"

        self._write_file()
        self.version += 1
        return f"Deleted memory '{normalized_name}'"

    def render_memory_summary(self, max_items: int | None = None) -> str:
        if not self.memories:
            return ""

        max_items = max_items or self.MAX_SUMMARY_ITEMS

        grouped: dict[str, list[MemoryItem]] = {
            "user": [],
            "feedback": [],
            "project": [],
            "reference": [],
        }

        for item in self.memories[:max_items]:
            grouped[item["type"]].append(item)

        lines: list[str] = ["[Persistent Notes]"]
        for mem_type in self.ALLOWED_TYPES:
            items = grouped[mem_type]
            if not items:
                continue
            lines.append(f"- {mem_type}:")
            for item in items:
                lines.append(f"  - {item['name']}: {item['description']}")

        return "\n".join(lines)

    def _write_file(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.memories, ensure_ascii=False, indent=2)
        self.memory_file.write_text(text + "\n", encoding="utf-8")

    def _normalize_name(self, name: str) -> str:
        name = name.strip().lower()
        name = re.sub(r"[^a-z0-9_\-]+", "_", name)
        name = re.sub(r"_+", "_", name).strip("_")
        return name

    def _normalize_loaded_item(self, item: object) -> MemoryItem | None:
        if not isinstance(item, dict):
            return None

        name = self._normalize_name(str(item.get("name", "")))
        mem_type = str(item.get("type", "")).strip()
        description = str(item.get("description", "")).strip()
        content = str(item.get("content", "")).strip()

        if not name or mem_type not in self.ALLOWED_TYPES:
            return None
        if not description or not content:
            return None

        return {
            "name": name,
            "type": mem_type,  # type: ignore[assignment]
            "description": description[: self.MAX_DESC_LEN],
            "content": content[: self.MAX_CONTENT_LEN],
        }

    def _validate(self, name: str, mem_type: str, description: str, content: str) -> None:
        if not name:
            raise ValueError("memory name cannot be empty")
        if len(name) > self.MAX_NAME_LEN:
            raise ValueError(f"memory name too long (>{self.MAX_NAME_LEN})")

        if mem_type not in self.ALLOWED_TYPES:
            raise ValueError(f"type must be one of {self.ALLOWED_TYPES}")

        if not description.strip():
            raise ValueError("description cannot be empty")
        if len(description.strip()) > self.MAX_DESC_LEN:
            raise ValueError(f"description too long (>{self.MAX_DESC_LEN})")

        if not content.strip():
            raise ValueError("content cannot be empty")
        if len(content.strip()) > self.MAX_CONTENT_LEN:
            raise ValueError(f"content too long (>{self.MAX_CONTENT_LEN})")

        lowered = f"{description}\n{content}".lower()
        for pattern in self.BANNED_PATTERNS:
            if re.search(pattern, lowered):
                raise ValueError(
                    "memory looks like a secret or temporary task state; refused"
                )


MEMORY_GUIDANCE = """
Use persistent memory sparingly.

Good fits for memory:
- user preferences
- repeated user feedback
- stable project constraints
- external resource pointers

Do NOT save:
- repo structure or code facts that can be re-read
- temporary task state
- secrets
""".strip()


class PromptCache:
    def __init__(self, base_prompt: str, memory_store: MemoryStore):
        self.base_prompt = base_prompt.strip()
        self.memory_store = memory_store
        self._cached_prompt: str | None = None
        self._cached_memory_version = -1

    def get_system_prompt(self) -> str:
        if (
            self._cached_prompt is not None
            and self._cached_memory_version == self.memory_store.version
        ):
            return self._cached_prompt

        parts = [self.base_prompt]
        memory_summary = self.memory_store.render_memory_summary()
        if memory_summary:
            parts.append(memory_summary)
        parts.append(MEMORY_GUIDANCE)

        self._cached_prompt = "\n\n".join(parts)
        self._cached_memory_version = self.memory_store.version
        return self._cached_prompt

WORKDIR = Path.cwd()
memory_store = MemoryStore(WORKDIR)
memory_store.load_memories()


def run_save_memory(name: str, type: str, description: str, content: str) -> str:
    try:
        return memory_store.save_memory(
            name=name,
            mem_type=type,
            description=description,
            content=content,
        )
    except Exception as e:
        return f"Error: {e}"


def run_list_memories() -> str:
    items = memory_store.list_memories()
    if not items:
        return "(no memories)"
    return "\n".join(
        f"[{item['type']}] {item['name']}: {item['description']}"
        for item in items
    )


def run_read_memory(name: str) -> str:
    item = memory_store.get_memory(name)
    if not item:
        return f"Error: memory '{name}' not found"
    return (
        f"name: {item['name']}\n"
        f"type: {item['type']}\n"
        f"description: {item['description']}\n\n"
        f"{item['content']}"
    )


def run_delete_memory(name: str) -> str:
    try:
        return memory_store.delete_memory(name)
    except Exception as e:
        return f"Error: {e}"