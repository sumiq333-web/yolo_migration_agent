import hashlib
import json
import re
import subprocess
from pathlib import Path

from agent.state import TODO_STATE, WORKDIR, STATE, WORKSPACE_ROOT, safe_path
from agent.workspace import MODEL, client

PROJECT_ROOT = WORKSPACE_ROOT
PERSIST_THRESHOLD = 4000
TOOL_OUTPUT_DIR = PROJECT_ROOT / ".task_outputs" / "tool_results"
TOOL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TRANSCRIPT_DIR = PROJECT_ROOT / ".transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

COMPACT_STATE = {
    "has_compacted": False,
    "last_summary": "",
    "recent_files": [],
}

def remember_recent_file(path: str, limit: int = 8) -> None:
    files = COMPACT_STATE["recent_files"]
    if path in files:
        files.remove(path)
    files.append(path)
    if len(files) > limit:
        del files[:-limit]
def save_large_output(tool_name: str, output: str) -> Path:
    digest = hashlib.md5(output.encode("utf-8")).hexdigest()[:8]
    path = TOOL_OUTPUT_DIR / f"{tool_name}_{digest}.txt"
    path.write_text(output, encoding="utf-8")
    return path


def persist_large_output(tool_name: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output

    stored_path = save_large_output(tool_name, output)
    preview = output[:1200]

    rel_path = stored_path.relative_to(PROJECT_ROOT)
    remember_recent_file(str(rel_path))
    return (
        "<persisted-output>\n"
        f"Full output saved to: {rel_path}\n"
        "Preview:\n"
        f"{preview}\n"
        "</persisted-output>"
    )


def estimate_context_size(messages: list) -> int:
    return len(str(messages))


def write_transcript(messages: list) -> Path:
    path = TRANSCRIPT_DIR / "latest_transcript.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for message in messages:
            f.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    return path


def summarize_history(messages: list, focus: str | None = None) -> str:
    conversation = json.dumps(messages, ensure_ascii=False, default=str)[:80000]

    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve:\n"
        "1. The current goal\n"
        "2. Important findings and decisions\n"
        "3. Files read or changed\n"
        "4. Remaining work\n"
        "5. User constraints and preferences\n"
    )

    if focus:
        prompt += f"6. Pay special attention to this focus: {focus}\n"

    prompt += "\nBe compact but concrete.\n\n" + conversation

    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )

    text = []
    for block in response.content:
        block_text = getattr(block, "text", None)
        if block_text:
            text.append(block_text)

    return "".join(text).strip()


def compact_history(messages: list, focus: str | None = None) -> list:
    transcript_path = write_transcript(messages)

    try:
        summary = summarize_history(messages, focus=focus)
    except Exception as e:
        summary = f"Error: failed to summarize history ({e})"

    if COMPACT_STATE["recent_files"]:
        recent_lines = "\n".join(f"- {path}" for path in COMPACT_STATE["recent_files"])
        summary += f"\n\nRecent files to reopen if needed:\n{recent_lines}"

    COMPACT_STATE["has_compacted"] = True
    COMPACT_STATE["last_summary"] = summary

    return [{
        "role": "user",
        "content": (
            "This conversation was compacted so the agent can continue working.\n\n"
            f"Transcript saved to: {transcript_path}\n\n"
            f"{summary}"
        ),
    }]


def todo_note_round_without_update() -> None:
    TODO_STATE["rounds_since_update"] += 1

PLAN_REMINDER_INTERVAL = 3

def todo_reminder() -> str | None:
    if not TODO_STATE["items"]:
        return None
    if TODO_STATE["rounds_since_update"] < PLAN_REMINDER_INTERVAL:
        return None
    return "<reminder>Refresh your current plan before continuing.</reminder>"

def normalize_messages(messages: list) -> list:
    """Clean up messages before sending to the API.

    Three jobs:
    1. Strip internal metadata fields the API doesn't understand
    2. Ensure every tool_use has a matching tool_result (insert placeholder if missing)
    3. Merge consecutive same-role messages (API requires strict alternation)
    """
    cleaned = []
    for msg in messages:
        clean = {"role": msg["role"]}
        if isinstance(msg.get("content"), str):
            clean["content"] = msg["content"]
        elif isinstance(msg.get("content"), list):
            clean["content"] = [
                {k: v for k, v in block.items()
                 if not k.startswith("_")}
                for block in msg["content"]
                if isinstance(block, dict)
            ]
        else:
            clean["content"] = msg.get("content", "")
        cleaned.append(clean)

    # Collect existing tool_result IDs
    existing_results = set()
    for msg in cleaned:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    existing_results.add(block.get("tool_use_id"))

    # Find orphaned tool_use blocks and insert placeholder results
    for msg in cleaned:
        if msg["role"] != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") not in existing_results:
                cleaned.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block["id"],
                     "content": "(cancelled)"}
                ]})

    # Merge consecutive same-role messages
    if not cleaned:
        return cleaned
    merged = [cleaned[0]]
    for msg in cleaned[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            prev_c = prev["content"] if isinstance(prev["content"], list) \
                else [{"type": "text", "text": str(prev["content"])}]
            curr_c = msg["content"] if isinstance(msg["content"], list) \
                else [{"type": "text", "text": str(msg["content"])}]
            prev["content"] = prev_c + curr_c
        else:
            merged.append(msg)
    return merged
def make_safe_filename(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).strip("._")
    return safe or "subagent_summary"
def render_todo() -> str:
    if not TODO_STATE["items"]:
        return "No session plan yet."

    lines = []
    for item in TODO_STATE["items"]:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }[item["status"]]

        line = f"{marker} {item['content']}"
        if item["status"] == "in_progress" and item["activeForm"]:
            line += f" ({item['activeForm']})"
        lines.append(line)

    completed = sum(1 for item in TODO_STATE["items"] if item["status"] == "completed")
    lines.append(f"\n({completed}/{len(TODO_STATE['items'])} completed)")
    return "\n".join(lines)


def todo_update(items: list) -> str:
    if len(items) > 12:
        return "Error: Keep the session plan short (max 12 items)"

    normalized = []
    in_progress_count = 0

    for index, raw_item in enumerate(items):
        content = str(raw_item.get("content", "")).strip()
        status = str(raw_item.get("status", "pending")).lower()
        active_form = str(raw_item.get("activeForm", "")).strip()

        if not content:
            return f"Error: Item {index}: content required"
        if status not in {"pending", "in_progress", "completed"}:
            return f"Error: Item {index}: invalid status '{status}'"
        if status == "in_progress":
            in_progress_count += 1

        normalized.append({
            "content": content,
            "status": status,
            "activeForm": active_form,
        })

    if in_progress_count > 1:
        return "Error: Only one plan item can be in_progress"

    TODO_STATE["items"] = normalized
    TODO_STATE["rounds_since_update"] = 0
    return render_todo()

def resolve_workspace_path(path: str) -> Path:
    p = Path(path).expanduser()

    # 1) Absolute path: validate directly against allowed roots.
    if p.is_absolute():
        return safe_path(str(p), STATE["workspace_roots"])

    # 2) Relative path: prefer current YOLO workspace if set.
    yolo_root = STATE.get("yolo_path")
    if yolo_root:
        candidate = Path(yolo_root) / p
        try:
            return safe_path(str(candidate), STATE["workspace_roots"])
        except Exception:
            pass

    # 3) Fallback to old behavior.
    return safe_path(path, STATE["workspace_roots"])

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        cwd = STATE.get("yolo_path") or WORKDIR
        r = subprocess.run(command, shell=True, cwd=cwd,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_python(code: str) -> str:
    cwd = STATE.get("yolo_path") or WORKDIR
    try:
        r = subprocess.run(
            ["python", "-c", code],
            capture_output=True, text=True, timeout=30, cwd=cwd,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (30s)"


def run_read(path: str, limit: int = None) -> str:
    remember_recent_file(path)
    try:
        text = resolve_workspace_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, create: bool = False) -> str:
    try:
        fp = resolve_workspace_path(path)
        if not fp.exists() and not create:
            return f"Error: file does not exist: {path}. Set create=True only if the task explicitly asks to create a new file."
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = resolve_workspace_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


import ast
from pathlib import Path


def _slice_lines(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    start_idx = max(0, start_line - 1)
    end_idx = min(len(lines), end_line)
    return "\n".join(lines[start_idx:end_idx])


def _safe_get_source_segment(text: str, node: ast.AST) -> str:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None:
        return ""
    if end is None:
        end = min(start + 80, len(text.splitlines()))
    return _slice_lines(text, start, end)


def _rank_symbols(functions: list[dict], classes: list[dict], goal: str, symbols: list[str]) -> list[str]:
    explicit = symbols or []
    if explicit:
        return explicit

    goal_lower = (goal or "").lower()

    candidates = []

    def score_name(name: str) -> int:
        score = 0
        lower = name.lower()

        if "parse" in lower:
            score += 5
        if "yaml" in lower:
            score += 5
        if lower.endswith("model"):
            score += 4
        if "task" in lower:
            score += 2

        if "architecture" in goal_lower or "build" in goal_lower or "construction" in goal_lower:
            if "parse" in lower or "yaml" in lower or lower.endswith("model"):
                score += 3

        return score

    for item in functions:
        candidates.append((score_name(item["name"]), item["name"]))
    for item in classes:
        candidates.append((score_name(item["name"]), item["name"]))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    ranked = [name for score, name in candidates if score > 0]

    # fallback: give something useful even if heuristics are weak
    if not ranked:
        ranked = [item["name"] for item in functions[:5]] + [item["name"] for item in classes[:5]]

    return ranked[:8]


def run_read_code(path: str, mode: str, symbols: list[str] | None = None, goal: str | None = None) -> str:
    """
    Minimal code-navigation tool.

    Modes
    -----
    index:
        Return structural information for a Python file:
        - module_doc
        - classes
        - functions
        - methods_by_class

    focus:
        Return local code for explicitly requested symbols only.
        Supported symbol forms:
        - top-level function: "parse_model"
        - top-level class: "DetectionModel"
        - class method: "DetectionModel.forward"

    Notes
    -----
    - This tool is intentionally low-coupling.
    - It does not guess business importance.
    - It does not infer next steps.
    - It does not embed domain knowledge.
    """
    try:
        fp = resolve_workspace_path(path)
        text = fp.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: {e}"

    if fp.suffix != ".py":
        return f"Error: read_code currently supports Python files only: {path}"

    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return f"Error: failed to parse Python AST in {path}: {e}"

    module_doc = ast.get_docstring(tree) or ""

    functions = []
    classes = []
    methods_by_class = {}

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line_start": getattr(node, "lineno", None),
                "line_end": getattr(node, "end_lineno", None),
                "docstring": ast.get_docstring(node) or "",
            })

        elif isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "line_start": getattr(node, "lineno", None),
                "line_end": getattr(node, "end_lineno", None),
                "docstring": ast.get_docstring(node) or "",
            })

            methods = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": child.name,
                        "line_start": getattr(child, "lineno", None),
                        "line_end": getattr(child, "end_lineno", None),
                        "docstring": ast.get_docstring(child) or "",
                    })
            methods_by_class[node.name] = methods

    if mode == "index":
        payload = {
            # Most important first
            "module_doc": module_doc,
            "classes": [
                {
                    "name": item["name"],
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                }
                for item in classes[:80]
            ],
            "functions": [
                {
                    "name": item["name"],
                    "line_start": item["line_start"],
                    "line_end": item["line_end"],
                }
                for item in functions[:120]
            ],
            "methods_by_class": {
                cls: [
                    {
                        "name": m["name"],
                        "line_start": m["line_start"],
                        "line_end": m["line_end"],
                    }
                    for m in methods[:40]
                ]
                for cls, methods in list(methods_by_class.items())[:40]
            },
            # Metadata later
            "path": path,
            "language": "python",
            "mode": "index",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    if mode == "focus":
        if not symbols:
            return "Error: focus mode requires explicit symbols"

        symbol_map = {}

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbol_map[node.name] = node

                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            symbol_map[f"{node.name}.{child.name}"] = child

        found_symbols = []
        missing_symbols = []

        for name in symbols:
            node = symbol_map.get(name)
            if node is None:
                missing_symbols.append(name)
                continue

            snippet = _safe_get_source_segment(text, node)

            found_symbols.append({
                # Most important first
                "symbol": name,
                "kind": type(node).__name__,
                "line_start": getattr(node, "lineno", None),
                "line_end": getattr(node, "end_lineno", None),
                "docstring": ast.get_docstring(node) or "",
                "snippet": snippet[:8000],
            })

        payload = {
            # Most important first
            "requested_symbols": symbols,
            "found_symbols": found_symbols,
            "missing_symbols": missing_symbols,
            # Metadata later
            "path": path,
            "language": "python",
            "mode": "focus",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    return f"Error: unsupported mode '{mode}'"

# ---------- s06: typed tool-output compression ----------

CONTROL_TOOLS = {
    "load_skill",
    "todo",
    "set_yolo_workspace",
    "set_project_workspace",

    "task_create_graph",
    "task_list",
    "task_get",
    "task_ready",
    "task_set_status",
    "background_run",
    "background_check",
    "background_list",
    "cron_create",
    "cron_delete",
    "cron_list",
    "schedule_after",
    "team_init",
    "list_teammates",
    "wake_teammate",
    "shutdown_teammate",
    "send_message",
    "read_inbox",
    "broadcast",
    "dispatch_to_teammate",
}

STRUCTURED_TOOLS = {
    "read_code",
}

RAW_PERSIST_CANDIDATES = {
    "read_file",
    "bash",
    "scan_paper",
    "scan_yolo_project",
    "scan_project",
}

RAW_KEEP_LAST = 3


def classify_tool_output(tool_name: str) -> str:
    """
    Return one of:
    - control
    - structured
    - raw
    """
    if tool_name in CONTROL_TOOLS:
        return "control"
    if tool_name in STRUCTURED_TOOLS:
        return "structured"
    return "raw"


def _guess_result_kind(tool_name: str, tool_input: dict, output: str) -> str:
    path = str(tool_input.get("path", ""))

    if tool_name == "read_file":
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            return "code_file"
        if suffix in {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}:
            return "config_file"
        if suffix in {".log", ".out"}:
            return "log_file"
        return "text_file"

    if tool_name == "read_code":
        return "structured_code"

    if tool_name in {"scan_yolo_project", "scan_project"}:
        return "directory_scan"

    if tool_name == "bash":
        return "shell_output"

    if tool_name == "scan_paper":
        return "document_result"

    if output.lstrip().startswith("{") or output.lstrip().startswith("["):
        return "json_like"

    return "raw_text"


def _extract_code_anchors(text: str, limit: int = 8) -> list[str]:
    """
    Extract task-relevant top-level symbols from raw Python source.

    Strategy:
    1. Parse AST and collect top-level classes/functions
    2. Score symbols by relevance
    3. Return the highest-value anchors first
    4. Fall back to regex if AST parsing fails
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        anchors = []
        for line in text.splitlines():
            m = re.match(r"^\s*(class|def)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                anchors.append(f"{m.group(1)} {m.group(2)}")
            if len(anchors) >= limit:
                break
        return anchors

    symbols = []

    def score_symbol(kind: str, name: str) -> int:
        lower = name.lower()
        score = 0

        # Strong relevance
        if "parse" in lower:
            score += 10
        if "yaml" in lower:
            score += 10

        # Architecture / task-related
        if lower.endswith("model"):
            score += 8
        if "detect" in lower:
            score += 6
        if "segment" in lower or "pose" in lower or "obb" in lower or "classif" in lower:
            score += 5
        if "task" in lower:
            score += 4
        if "load" in lower:
            score += 3

        # Prefer top-level classes/functions over utility-looking names
        if kind == "class":
            score += 2

        # Small penalty for private/internal helpers
        if name.startswith("_"):
            score -= 2

        return score

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(("class", node.name, score_symbol("class", node.name), getattr(node, "lineno", 0)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(("def", node.name, score_symbol("def", node.name), getattr(node, "lineno", 0)))

    # Sort by relevance first, then by source order
    symbols.sort(key=lambda x: (-x[2], x[3]))

    anchors = [f"{kind} {name}" for kind, name, _score, _lineno in symbols[:limit]]

    # Fallback if AST had almost nothing useful
    if not anchors:
        for line in text.splitlines():
            m = re.match(r"^\s*(class|def)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
            if m:
                anchors.append(f"{m.group(1)} {m.group(2)}")
            if len(anchors) >= limit:
                break

    return anchors


def _extract_config_anchors(text: str, limit: int = 8) -> list[str]:
    anchors = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.lstrip() != line:
            continue  # top-level only
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if m:
            anchors.append(m.group(1))
        if len(anchors) >= limit:
            break
    return anchors


def _extract_log_anchors(text: str, limit: int = 6) -> list[str]:
    hits = []
    for line in text.splitlines():
        lower = line.lower()
        if "error" in lower or "warning" in lower or "exception" in lower:
            hits.append(line.strip())
        if len(hits) >= limit:
            break

    if not hits:
        tail = [line.strip() for line in text.splitlines()[-3:] if line.strip()]
        hits.extend(tail)

    return hits[:limit]


def _first_nonempty_lines(text: str, limit: int = 3) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[:limit]


def build_tool_digest(tool_name: str, tool_input: dict, output: str, saved_to: str) -> dict:
    kind = _guess_result_kind(tool_name, tool_input, output)
    source_path = str(tool_input.get("path", ""))

    summary = ""
    anchors: list[str] = []
    reopen_hint = ""
    task_relevance = ""

    if kind == "code_file":
        summary = "Large raw Python file content was externalized."
        anchors = _extract_code_anchors(output)
        # print("DEBUG digest anchors:", anchors)
        reopen_hint = (
            f"Prefer read_code(path={source_path!r}, mode='index') "
            f"or read_code(path={source_path!r}, mode='focus', symbols=[...])."
        )
        task_relevance = "Use this digest for navigation; reopen source before making edits."

    elif kind == "config_file":
        summary = "Large raw config/YAML content was externalized."
        anchors = _extract_config_anchors(output)
        reopen_hint = f"Use read_file(path={source_path!r}) to reopen the config when needed."
        task_relevance = "Useful for recovering top-level sections without keeping the full raw text in context."

    elif kind == "directory_scan":
        summary = "Large directory scan result was externalized."
        try:
            data = json.loads(output)
            dirs = data.get("dirs", [])
            files = data.get("files", [])
            priority = data.get("priority_dirs", [])
            anchors = [f"priority:{name}" for name in priority] + [f"dir:{name}" for name in dirs[:3]]
            summary = f"Directory scan externalized ({len(dirs)} dirs, {len(files)} files in result window)."
        except Exception:
            anchors = _first_nonempty_lines(output, limit=5)
        reopen_hint = "Re-run scan on a narrower path or use direct read_file/read_code if the target path is known."
        task_relevance = "Useful for relocation when the exact file path is unknown."

    elif kind == "shell_output" or kind == "log_file":
        summary = "Large shell/log output was externalized."
        anchors = _extract_log_anchors(output)
        reopen_hint = "Reopen the saved raw output if you need the full log tail or error context."
        task_relevance = "Use the anchors to identify failures before reopening the full raw output."

    else:
        summary = "Large raw tool output was externalized."
        anchors = _first_nonempty_lines(output, limit=5)
        reopen_hint = "Reopen the saved raw output if more detail is needed."
        task_relevance = "Externalized to preserve context while keeping a short working-memory digest."

    return {
        "kind": kind,
        "source_tool": tool_name,
        "source_path": source_path,
        "saved_to": saved_to,
        "summary": summary,
        "anchors": anchors,
        "task_relevance": task_relevance,
        "reopen_hint": reopen_hint,
    }


def prepare_tool_result(tool_name: str, tool_input: dict, raw_output) -> tuple[str, dict]:
    """
    Return:
      (content_for_context, metadata_for_local_compaction)
    """
    output = str(raw_output)
    role = classify_tool_output(tool_name)

    meta = {
        "role": role,
        "externalized": False,
        "summary": "",
        "saved_to": "",
    }

    # 1) Control-plane outputs should stay in active context.
    if role == "control":
        meta["summary"] = f"{tool_name} control result kept in context"
        return output, meta

    # 2) Structured navigation outputs are already compressed; keep them.
    if role == "structured":
        meta["summary"] = f"{tool_name} structured result kept in context"
        return output, meta

    # 3) Raw outputs: keep if small enough.
    if len(output) <= PERSIST_THRESHOLD:
        meta["summary"] = f"{tool_name} raw result kept in context"
        return output, meta

    # 4) Large raw outputs: externalize + structured digest.
    stored_path = save_large_output(tool_name, output)
    rel_path = str(stored_path.relative_to(PROJECT_ROOT))
    remember_recent_file(rel_path)

    digest = build_tool_digest(tool_name, tool_input, output, rel_path)

    meta["externalized"] = True
    meta["summary"] = digest["summary"]
    meta["saved_to"] = rel_path

    content = (
        "<externalized-result>\n"
        f"{json.dumps(digest, ensure_ascii=False, indent=2)}\n"
        "</externalized-result>"
    )
    return content, meta


def micro_compact_messages(messages: list, keep_last_raw: int = RAW_KEEP_LAST) -> tuple[list, bool]:
    """
    Compact only older raw tool results.
    Preserve:
    - control results
    - structured navigation results
    """
    raw_positions = []

    for i, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue

            role = block.get("_compression_role", "raw")
            if role == "raw":
                raw_positions.append((i, j))

    if len(raw_positions) <= keep_last_raw:
        return messages, False

    for i, j in raw_positions[:-keep_last_raw]:
        block = messages[i]["content"][j]
        summary = block.get("_digest_summary", "Earlier raw tool result compacted.")
        saved_to = block.get("_saved_to", "")

        marker = f"[Earlier raw tool result compacted] {summary}"
        if saved_to:
            marker += f" Saved to: {saved_to}"

        block["content"] = marker

    return messages, True