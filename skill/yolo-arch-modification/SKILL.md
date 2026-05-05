---
name: yolo-arch-modification
description: Locate and modify YOLO model architecture in Ultralytics-based repos. Use when changing model YAML layouts, inserting or removing layers or heads, updating block definitions, touching model/task registration imports, or debugging channel or shape mismatches after architecture edits.
---

# YOLO Architecture Modification

Use this skill to change YOLO model structure with minimal, testable edits.

## Planning

- Create a todo before deep inspection.
- Make the todo reflect the actual investigation path and the decision points that may change the edit scope.

## Workflow

1. Inspect the target architecture YAML first, usually `ultralytics/cfg/models/11/*.yaml`.
2. Trace each referenced block or head into `ultralytics/nn/modules/*.py`.
3. Check `ultralytics/nn/tasks.py` for model construction, parsing, or registry logic.
4. Check `ultralytics/models/yolo/model.py` only when task entry points or model instantiation paths change.
5. Prefer the smallest change that keeps the graph valid.

## Edit Rules

- Keep layer indices, `from` references, and repeat counts aligned after inserts or deletes.
- Preserve channel dimensions across every connection.
- Reuse an existing module before creating a new one.
- Update `__init__.py` exports or `tasks.py` imports only when the new block cannot already be resolved by name.
- Avoid editing task entry files if the change is YAML-only or a pure block implementation update.

## Output Guidance

- Organize edit guidance by modification necessity instead of returning a flat file list.
- Adapt the grouping to the actual change scope, such as YAML-only edits, new module work, parser updates, or registration changes.

## Validation

- Run a smoke test that instantiates the model from the edited YAML.
- Run a small forward pass if the change affects tensor flow.
- Recheck the first shape or import failure before broadening the fix.
- If the repository layout differs, locate the nearest equivalent YAML, module, parser, and entry-point files before editing.

## Common Risks

- Wrong channel dimensions after an inserted or removed layer.
- Missing imports or exports for a new module.
- YAML layer reference mismatch after reordering.
- Unnecessary edits to task entry files when only YAML or module code needed to change.
