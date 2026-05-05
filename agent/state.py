"""Shared agent state and workspace path helpers."""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1].resolve()
WORKDIR = Path.cwd()

STATE = {
    "workspace_roots": [str(WORKSPACE_ROOT)],
    "active_project": None,
}
STATE.setdefault("yolo_path", None)
STATE.setdefault("yolo_roots", [])
TODO_STATE = {
    "items": [],
    "rounds_since_update": 0,
}


def safe_path(path_str: str, allowed_roots: list[str]) -> Path:
    path = Path(path_str).expanduser()

    if not path.is_absolute():
        path = (Path(allowed_roots[0]) / path).resolve()
    else:
        path = path.resolve()

    for root_str in allowed_roots:
        root = Path(root_str).resolve()
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue

    raise ValueError(f"Path escapes workspace: {path_str}")
