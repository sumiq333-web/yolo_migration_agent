import re
from pathlib import Path


class SkillRegistry:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    def _parse_frontmatter(self, text: str):
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text

        meta = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)

    def _load_all(self):
        if not self.skills_dir.exists():
            return

        for path in self.skills_dir.rglob("SKILL.md"):
            meta, body = self._parse_frontmatter(path.read_text(encoding="utf-8"))
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "")
            self.skills[name] = {
                "name": name,
                "description": description,
                "body": body.strip(),
            }

    def describe_available(self) -> str:
        if not self.skills:
            return "(no skills available)"
        return "\n".join(
            f"- {skill['name']}: {skill['description']}"
            for skill in self.skills.values()
        )

    def load_full_text(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'"
        return f"<skill name=\"{skill['name']}\">\n{skill['body']}\n</skill>"