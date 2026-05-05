"""YOLO tools module."""

from __future__ import annotations

from tools.project_context import YoloProjectContext


def set_yolo_workspace(path: str) -> str:
    ctx = YoloProjectContext(path)
    return ctx.activate()


def scan_yolo_project(path: str = ".") -> str:
    """
    Scan one directory inside the current YOLO project context.
    """
    # We reconstruct the current context from STATE-compatible YOLO root.
    # The active project is normally set by set_yolo_workspace().
    from agent.state import STATE

    yolo_root = STATE.get("yolo_path")
    if not yolo_root:
        return "Error: YOLO workspace not set"

    ctx = YoloProjectContext(yolo_root)
    return ctx.scan(path)