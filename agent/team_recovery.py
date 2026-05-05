from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TeamRecoveryReport:
    """
    Result of team startup recovery.

    stale_working_members:
        Members that were recorded as working, but the process was restarted,
        so their runtime threads no longer exist.

    members_with_pending_inbox:
        Members that have non-empty inbox files.

    config_path:
        Path to the team config file.

    inbox_dir:
        Path to the team inbox directory.
    """

    stale_working_members: list[str]
    members_with_pending_inbox: list[str]
    config_path: str
    inbox_dir: str

    def has_changes(self) -> bool:
        return bool(self.stale_working_members or self.members_with_pending_inbox)

    def render(self) -> str:
        lines = [
            "Team recovery report:",
            f"- config_path: {self.config_path}",
            f"- inbox_dir: {self.inbox_dir}",
        ]

        if self.stale_working_members:
            lines.append(
                "- Reset stale working members: "
                + ", ".join(self.stale_working_members)
            )
        else:
            lines.append("- No stale working members.")

        if self.members_with_pending_inbox:
            lines.append(
                "- Members with pending inbox messages: "
                + ", ".join(self.members_with_pending_inbox)
            )
        else:
            lines.append("- No pending teammate inbox messages.")

        return "\n".join(lines)


class TeamRecovery:
    """
    Startup recovery for persistent teammate state.

    Team identity is durable:
        .team/config.json

    Team inboxes are durable:
        .team/inbox/*.jsonl

    Teammate runtime threads are not durable:
        Python threads disappear when the process exits.

    Therefore, after process restart:
    - any member recorded as "working" is stale and should be reset to "idle"
    - inbox files may still contain pending messages
    - lead should decide which teammate to wake next
    """

    def __init__(self, *, team_dir: Path):
        self.team_dir = Path(team_dir)
        self.config_path = self.team_dir / "config.json"
        self.inbox_dir = self.team_dir / "inbox"

    def recover(self) -> TeamRecoveryReport:
        """
        Repair stale runtime state and report pending inboxes.

        This method is intentionally conservative:
        - It resets stale "working" statuses to "idle".
        - It does not wake teammates.
        - It does not drain inboxes.
        - It does not create missing default members.

        Team creation should remain the responsibility of TeammateManager.
        Teammate execution should remain the responsibility of lead.
        """
        config = self._load_config()

        stale_working_members = self._reset_stale_working_members(config)

        if stale_working_members:
            self._save_config(config)

        members_with_pending_inbox = self._find_members_with_pending_inbox(config)

        return TeamRecoveryReport(
            stale_working_members=stale_working_members,
            members_with_pending_inbox=members_with_pending_inbox,
            config_path=str(self.config_path),
            inbox_dir=str(self.inbox_dir),
        )

    def _reset_stale_working_members(self, config: dict[str, Any]) -> list[str]:
        reset_members: list[str] = []

        for member in config.get("members", []):
            if member.get("status") != "working":
                continue

            name = str(member.get("name", "unknown"))

            member["status"] = "idle"
            member["previous_status"] = "working"
            member["recovered_at"] = time.time()
            member["updated_at"] = time.time()

            reset_members.append(name)

        return reset_members

    def _find_members_with_pending_inbox(self, config: dict[str, Any]) -> list[str]:
        pending: list[str] = []

        for member in config.get("members", []):
            name = str(member.get("name", "")).strip()
            if not name:
                continue

            inbox_path = self.inbox_dir / f"{self._safe_name(name)}.jsonl"

            if not inbox_path.exists():
                continue

            try:
                text = inbox_path.read_text(encoding="utf-8").strip()
            except OSError:
                continue

            if text:
                pending.append(name)

        return pending

    def _load_config(self) -> dict[str, Any]:
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

        if not isinstance(data, dict):
            return {
                "team_name": "migration_team",
                "members": [],
            }

        data.setdefault("team_name", "migration_team")
        data.setdefault("members", [])

        if not isinstance(data["members"], list):
            data["members"] = []

        return data

    def _save_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _safe_name(self, name: str) -> str:
        cleaned = "".join(
            ch for ch in name.strip()
            if ch.isalnum() or ch in {"_", "-"}
        )
        return cleaned or "unknown"