---
name: analyze-yolo-architecture
description: analyze yolo-family architecture in a repository. use when the task is to identify the main architecture files, explain backbone, neck, and head organization, map model yaml definitions to python model-building code, and stay focused on core model files instead of experiments, logs, helper scripts, or unrelated utilities.
---

# Analyze YOLO Architecture

Analyze architecture only.

Do not:
- modify source code
- generate patches
- run experiments
- tune hyperparameters
- optimize performance
- review unrelated utility scripts
## Workspace Requirement

Before reading any repository file, call `set_yolo_workspace` with the target repository root.

After the workspace is set, use repository-relative paths such as `ultralytics/nn/tasks.py` and `ultralytics/cfg/models/...`.

For large Python source files such as `ultralytics/nn/tasks.py`, prefer `read_code` over `read_file`.

Do not read repository files before the workspace is set.
## Primary Trace

Use the target model YAML as the architecture source of truth.

Follow this path in order:

1. target model YAML
2. `ultralytics/nn/tasks.py`
3. only the module definition files required by the target YAML or `parse_model`
4. `ultralytics/models/yolo/model.py`
5. summary

Do not switch into open-ended repository exploration.

Do not compare multiple model families unless the user explicitly asks for comparison.

## Read Boundary

In the first pass, only read from these locations:

- target YAML
- `ultralytics/nn/tasks.py`
- `ultralytics/nn/modules/conv.py`
- `ultralytics/nn/modules/block.py`
- `ultralytics/nn/modules/head.py`
- `ultralytics/models/yolo/model.py`

Do not read any other file unless:
- the expected file path does not exist
- a required symbol is missing
- the repo uses a different layout

## Target YAML

If the user names a specific model family or task, start from that YAML.

Examples:
- `ultralytics/cfg/models/11/yolo11.yaml`
- `ultralytics/cfg/models/v8/yolov8.yaml`
- `ultralytics/cfg/models/v10/yolov10n.yaml`
- matching `-seg`, `-pose`, `-obb`, or `-cls` variants

If the expected YAML path is missing, report:

`repo layout differs from expected ultralytics structure`

Then perform one relocation search for the target YAML and continue the same trace.

## Phase Rules

### Phase 1: YAML

Open the target YAML first.

Extract:
- task variant
- `scales`
- `backbone`
- `head`
- final prediction module such as `Detect`, `Segment`, `Pose`, `OBB`, or `Classify`

### Phase 2: Build Logic

Open `ultralytics/nn/tasks.py`.

Use this sequence for large source files:

1. call `read_code(..., mode="index")` on `ultralytics/nn/tasks.py`
2. identify `yaml_model_load`, `parse_model`, and the task model class
3. call `read_code(..., mode="focus")` on those symbols
4. only fall back to raw `read_file` if symbol-focused reading is insufficient

Find:
- `yaml_model_load`
- the task model class for the target task
- `parse_model`

Explain how the build path goes from YAML load to model assembly.

Focus on how `parse_model`:
- walks `d["backbone"] + d["head"]`
- resolves module names
- applies repeats, channels, and scaling
- builds the layer list and save list

If the expected build file or symbols are missing, report:

`repo layout differs from expected ultralytics structure`

Then perform one relocation search for the missing build entry and continue.

### Phase 3: Module Definitions

Open only the files needed to explain modules actually used by the target YAML.

Typical mapping:
- `Conv`, `Concat` -> `ultralytics/nn/modules/conv.py`
- higher-level blocks -> `ultralytics/nn/modules/block.py`
- `Detect`, `Segment`, `Pose`, `OBB`, `Classify` -> `ultralytics/nn/modules/head.py`

Do not scan every class in these files.

For each missing module symbol, perform at most one relocation search for that symbol.

### Phase 4: User-Facing Entry

Open `ultralytics/models/yolo/model.py`.

Use it to explain:
- how `YOLO(...)` selects the task model
- how the entrypoint maps a task to the correct model class
- where YAML-based construction is reached from the user-facing API

If the expected entry file is missing, report:

`repo layout differs from expected ultralytics structure`

Then perform one relocation search for the user-facing YOLO model entry and continue.

## Fallback Budget

Use at most one relocation attempt per phase:

1. yaml relocation: 1 search
2. build-logic relocation: 1 search
3. module relocation: 1 search per missing symbol
4. entry relocation: 1 search

Do not perform repeated broad searches within the same phase.

## Summary Format

Return the analysis with these sections in this order:

1. `relevant folders`
2. `key files`
3. `file responsibilities`
4. `architecture`
5. `yaml to python mapping`
6. `ignored scope`

## Minimum Content Requirements

In `architecture`, explicitly identify:
- backbone
- neck or feature-fusion path
- final prediction head

If the YAML has no explicit `neck` section, identify the neck as the feature-fusion path between backbone outputs and the final prediction head.

In `yaml to python mapping`, explicitly include:
- target YAML
- `yaml_model_load`
- task model class
- `parse_model`
- module definition files used by the target architecture
- user-facing `YOLO(...)` entry path

## Scope Exclusions

Ignore these unless the user explicitly asks:
- helper scripts
- fft or image-processing scripts
- `runs/`, `logs/`, `outputs/`
- notebooks
- temporary experiment files
- dataset utilities
- one-off custom scripts unrelated to model assembly

Do not let side files change the analysis direction.

Stop once the target YAML, build logic, required module definition files, and user-facing entry are covered.

## Todo Discipline

If using a todo or plan tool, update it only when the main phase changes:

1. target YAML
2. build logic
3. module definitions
4. task entry
5. summary

Do not churn the todo because of side discoveries.

## Final Check

Before finishing, verify that every must-answer item in `references/analysis-checklist.md` is explicitly covered in the final response.
