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

# ---------------------------------------------------------------------
# Validation / smoke-test configuration
# ---------------------------------------------------------------------
# 说明：
# 这里集中管理 validation 阶段需要的固定环境配置。
# 当前阶段可以写死，后续如果要通用化，再迁移到 json/yaml 配置文件。
# 注意：这里仅放配置，不放执行逻辑。

STATE.setdefault(
    "validation_env",
    {
        # YOLO 项目根目录。
        # 如果运行时通过 set_yolo_workspace 设置了 STATE["yolo_path"]，
        # 后续 helper 应优先使用 STATE["yolo_path"]。
        "workspace": r"F:\python\yolov12-main",

        # 已经配置好 ultralytics / torch / yolo CLI 的 Python 环境。
        # 不建议让 experiment_runner 自己 activate / pip install。
        "python": r"F:\agent\learn-claude-code\venv\Scripts\python.exe",
        "yolo_cli": r"F:\agent\learn-claude-code\venv\Scripts\yolo.exe",

        # 冒烟测试默认使用 CPU，稳定、低依赖。
        "device": "cpu",
    },
)

STATE.setdefault(
    "validation_smoke_dataset",
    {
        # harness 内部生成和复用的极小 YOLO detection 数据集。
        "root": str(WORKSPACE_ROOT / ".runtime-tasks" / "yolo_smoke_data"),
        "data_yaml": str(WORKSPACE_ROOT / ".runtime-tasks" / "yolo_smoke_data" / "data.yaml"),
    },
)

STATE.setdefault(
    "validation_runs",
    {
        # yolo train smoke 的输出目录。
        "root": str(WORKSPACE_ROOT / ".runtime-tasks" / "yolo_smoke_runs"),
        "default_name": "yolov8_v10backbone_smoke",
    },
)

STATE.setdefault(
    "validation_profiles",
    {
        "yolo_train_smoke_v1": {
            "description": "Run one-epoch YOLO CLI train smoke test for detection model YAML.",
            "task": "detect",
            "mode": "train",
            "epochs": 1,
            "imgsz": 64,
            "batch": 1,
            "workers": 0,
            "device": "cpu",
            "exist_ok": True,
            "save": False,
            "plots": False,
            "verbose": False,
            "pass_criteria": [
                "process exits successfully",
                "model builds successfully",
                "dataloader loads smoke dataset",
                "one training epoch completes",
                "no forward/loss/backward error",
            ],
        }
    },
)

STATE.setdefault(
    "validation_command_allowlist_prefixes",
    [
        # 只允许 validator 执行受控的 yolo train smoke 命令前缀。
        r"F:\agent\learn-claude-code\venv\Scripts\yolo.exe detect train",
    ],
)

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
def get_validation_env() -> dict:
    """
    获取 validation 环境配置。

    优先返回 STATE 中的 validation_env。
    如果运行时设置了 STATE["yolo_path"]，则覆盖 validation_env["workspace"]。
    """
    env = dict(STATE.get("validation_env", {}))

    yolo_path = STATE.get("yolo_path")
    if yolo_path:
        env["workspace"] = str(Path(yolo_path).resolve())

    return env


def get_yolo_train_smoke_command(model_yaml: str, run_name: str | None = None) -> str:
    """
    构造 yolo_train_smoke_v1 命令。

    这里只负责拼接命令，不负责执行命令。
    experiment_runner 后续应该通过 background_run 执行这个命令。
    """
    env = get_validation_env()
    dataset = STATE["validation_smoke_dataset"]
    runs = STATE["validation_runs"]
    profile = STATE["validation_profiles"]["yolo_train_smoke_v1"]

    run_name = run_name or runs["default_name"]

    return (
        f'"{env["yolo_cli"]}" detect train '
        f'model="{model_yaml}" '
        f'data="{dataset["data_yaml"]}" '
        f'epochs={profile["epochs"]} '
        f'imgsz={profile["imgsz"]} '
        f'batch={profile["batch"]} '
        f'workers={profile["workers"]} '
        f'device={profile["device"]} '
        f'project="{runs["root"]}" '
        f'name="{run_name}" '
        f'exist_ok={profile["exist_ok"]} '
        f'save={profile["save"]} '
        f'plots={profile["plots"]} '
        f'verbose={profile["verbose"]}'
    )


def build_yolo_train_smoke_spec(model_yaml: str, run_name: str | None = None) -> dict:
    """
    构造传给 experiment_runner 的 validation_spec。

    lead dispatch validation task 时，可以把这个 dict 序列化后塞进
    <validation-spec>...</validation-spec>。
    """
    env = get_validation_env()
    dataset = STATE["validation_smoke_dataset"]
    runs = STATE["validation_runs"]
    profile = STATE["validation_profiles"]["yolo_train_smoke_v1"]
    command = get_yolo_train_smoke_command(model_yaml, run_name=run_name)

    return {
        "type": "yolo_train_smoke_v1",
        "workspace": env["workspace"],
        "model_yaml": model_yaml,
        "data_yaml": dataset["data_yaml"],
        "env": {
            "python": env["python"],
            "yolo_cli": env["yolo_cli"],
            "device": env["device"],
        },
        "command": command,
        "runs_dir": runs["root"],
        "run_name": run_name or runs["default_name"],
        "pass_criteria": profile["pass_criteria"],
        "failure_policy": {
            "do_not_modify_command": True,
            "do_not_install_packages": True,
            "on_denied": "submit_blocked",
            "on_nonzero_exit": "submit_fail",
        },
    }