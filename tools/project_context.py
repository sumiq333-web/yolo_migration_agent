from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agent.state import STATE, safe_path


DEFAULT_MAX_ITEMS = 30


class BaseProjectContext:
    """
    Generic project context.

    Responsibilities:
    - hold the project root
    - register workspace roots
    - resolve absolute / project-relative paths
    - scan one directory into a compact summary
    - expose project metadata for tool wrappers

    This base class is intentionally domain-light.
    Domain-specific state should be added in subclasses.
    """

    project_type = "generic"

    def __init__(
        self,
        root: str,
        *,
        priority_names: Optional[list[str]] = None,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.priority_names = priority_names or []
        self.max_items = max_items

    # ---------- lifecycle ----------
    def validate_root(self) -> Optional[str]:
        if not self.root.exists() or not self.root.is_dir():
            return f"Error: invalid {self.project_type} workspace: {self.root}"
        return None

    def register_workspace_root(self) -> None:
        """
        Register this project root into global workspace roots so safe_path can validate it.
        """
        root_str = str(self.root)
        if root_str not in STATE["workspace_roots"]:
            STATE["workspace_roots"].append(root_str)

    def to_active_project(self) -> dict:
        """
        Public metadata stored in STATE["active_project"].
        Keep this compact and generic.
        """
        return {
            "type": self.project_type,
            "root": str(self.root),
        }

    def activate(self) -> str:
        """
        Validate and activate this project context.
        """
        error = self.validate_root()
        if error:
            return error

        self.register_workspace_root()
        STATE["active_project"] = self.to_active_project()
        return f"{self.project_type.capitalize()} workspace set to: {self.root}"

    # ---------- path resolution ----------
    def resolve_path(self, path: str = ".") -> Path:
        """
        Resolve a path relative to this project root unless already absolute.
        Then validate against allowed workspace roots.
        """
        target = Path(path).expanduser()

        if target.is_absolute():
            return safe_path(str(target), STATE["workspace_roots"])

        return safe_path(str(self.root / target), STATE["workspace_roots"])

    # ---------- scanning ----------
    def scan(self, path: str = ".") -> str:
        """
        Scan one directory and return a compact summary of its immediate
        subdirectories and files.
        """
        try:
            root = self.resolve_path(path)
        except Exception as e:
            return json.dumps(
                {
                    "root": str(path),
                    "error": str(e),
                    "dirs": [],
                    "files": [],
                    "priority_dirs": [],
                },
                ensure_ascii=False,
                indent=2,
            )

        if not root.exists() or not root.is_dir():
            return json.dumps(
                {
                    "root": str(root),
                    "error": "path_not_found",
                    "dirs": [],
                    "files": [],
                    "priority_dirs": [],
                },
                ensure_ascii=False,
                indent=2,
            )

        dirs = sorted([entry.name for entry in root.iterdir() if entry.is_dir()], key=str.lower)
        files = sorted([entry.name for entry in root.iterdir() if entry.is_file()], key=str.lower)

        result_dirs: list[str] = []
        result_files: list[str] = []

        while len(result_dirs) + len(result_files) < self.max_items:
            progressed = False

            if dirs:
                result_dirs.append(dirs.pop(0))
                progressed = True

            if len(result_dirs) + len(result_files) >= self.max_items:
                break

            if files:
                result_files.append(files.pop(0))
                progressed = True

            if not progressed:
                break

        priority_dirs = [name for name in self.priority_names if name in result_dirs]

        return json.dumps(
            {
                "root": str(root),
                "dirs": result_dirs,
                "files": result_files,
                "priority_dirs": priority_dirs,
            },
            ensure_ascii=False,
            indent=2,
        )


class YoloProjectContext(BaseProjectContext):
    """
    YOLO-specific project context.

    This subclass is allowed to carry YOLO-specific metadata because it is a
    domain tool layer, not a generic tool layer.
    """

    project_type = "yolo"

    def __init__(self, root: str) -> None:
        super().__init__(
            root=root,
            priority_names=["ultralytics", "nn", "models", "cfg", "engine"],
            max_items=30,
        )

    def to_active_project(self) -> dict:
        data = super().to_active_project()
        data["yolo_root"] = str(self.root)
        return data

    def activate(self) -> str:
        error = self.validate_root()
        if error:
            return error

        self.register_workspace_root()

        # Keep compatibility with existing code that still reads these keys.
        STATE["yolo_path"] = str(self.root)
        if "yolo_roots" not in STATE:
            STATE["yolo_roots"] = []
        if str(self.root) not in STATE["yolo_roots"]:
            STATE["yolo_roots"].append(str(self.root))

        STATE["active_project"] = self.to_active_project()
        return f"YOLO workspace set to: {self.root}"


class GithubProjectContext(BaseProjectContext):
    """
    GitHub/local-code style project context.

    This is a placeholder for the next step.
    You can refine priority_names later depending on the repos you care about.
    """

    project_type = "github"

    def __init__(self, root: str) -> None:
        super().__init__(
            root=root,
            priority_names=["src", "app", "lib", "packages", ".github"],
            max_items=30,
        )

    def to_active_project(self) -> dict:
        data = super().to_active_project()
        data["github_root"] = str(self.root)
        return data

    def activate(self) -> str:
        error = self.validate_root()
        if error:
            return error

        self.register_workspace_root()
        STATE["active_project"] = self.to_active_project()
        return f"GitHub workspace set to: {self.root}"


# ---------- registry helpers ----------
def get_active_project_context() -> BaseProjectContext | None:
    """
    Reconstruct the current active project context from STATE.
    """
    active = STATE.get("active_project")
    if not active:
        return None

    project_type = active.get("type")
    root = active.get("root")
    if not root:
        return None

    if project_type == "yolo":
        return YoloProjectContext(root)
    if project_type == "github":
        return GithubProjectContext(root)

    return BaseProjectContext(root)


def scan_active_project(path: str = ".") -> str:
    ctx = get_active_project_context()
    if ctx is None:
        return "Error: no active project context is set"
    return ctx.scan(path)