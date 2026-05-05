# analysis checklist

## folder roles to verify
- `ultralytics/cfg/models/`: architecture declaration layer
- `ultralytics/nn/`: yaml-to-model build path and task model classes
- `ultralytics/nn/modules/`: concrete module implementations used by yaml rows and `parse_model`
- `ultralytics/models/yolo/`: user-facing task entry and wrapper path into model construction

## must-answer questions
- what does each main folder do in the architecture trace?
- which yaml file is the architecture source of truth?
- which function turns yaml rows into python modules?
- which model class loads the yaml for this task?
- which files define the main modules used by this yaml?
- which layers belong to the backbone?
- which layers act as the neck or feature-fusion path?
- which layer is the final prediction head?
- how does the user-facing `YOLO(...)` entry reach this model build path?
- did the repo follow the expected ultralytics layout, and if not, what equivalent files were used instead?
- which unrelated files were intentionally ignored?

## section checks

### relevant folders
- explain each visited folder in architecture terms
- do not dump full trees
- state why each folder matters to the trace

### key files
- include the exact yaml file
- include the build logic file
- include the module definition files actually used
- include the user-facing entry file

### file responsibilities
- give one architecture-focused responsibility per key file
- do not describe files vaguely

### architecture
- separate backbone, neck, and head
- if no explicit neck section exists, identify the feature-fusion path as neck
- name the final prediction head module

### yaml to python mapping
- include yaml load
- include task model class
- include `parse_model`
- include the module resolution path
- include how `YOLO(...)` reaches this path

### ignored scope
- explicitly state which unrelated files or folders were ignored

## stop conditions
stop exploring once these are clear:
- target yaml is identified
- `parse_model` path is explained
- main modules used by the yaml are located
- backbone, neck, and head are summarized
- entrypoint mapping is explained
