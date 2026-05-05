from pathlib import Path
from agent.message_bus import MessageBus


bus = MessageBus(Path(".team/inbox"))

print(bus.send(
    sender="lead",
    to="engineer",
    content="Inspect YOLO architecture entry points.",
    msg_type="task_request",
))

print(bus.read_inbox("engineer"))

print(bus.read_inbox("engineer"))