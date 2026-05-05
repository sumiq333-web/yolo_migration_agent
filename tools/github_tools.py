"""GitHub project tools module."""

from __future__ import annotations

from tools.project_context import GithubProjectContext


def set_github_workspace(path: str) -> str:
    ctx = GithubProjectContext(path)
    return ctx.activate()


def scan_github_project(path: str = ".") -> str:
    from agent.state import STATE

    active = STATE.get("active_project")
    if not active or active.get("type") != "github":
        return "Error: GitHub workspace not set"

    root = active.get("root")
    ctx = GithubProjectContext(root)
    return ctx.scan(path)