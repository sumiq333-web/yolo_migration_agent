import os


def paper_tools():
    files = os.listdir(".")
    return files


def yolo_tools():
    pass


TOOL_HANDLERS = {
    "scan_paper":  lambda **kw: paper_tools(),
    "scan_yolo":  lambda **kw: yolo_tools(),
}

class Agent:
    def __init__(self):
        self.state={
            "history":[],
            "done":False,
        }

    def decide(self):
        if len(self.state["history"])== 0:
            return "scan_paper"
        elif len(self.state["history"])== 1:
            return "scan_yolo"
        else:
            return "finish"

    def act(self, action):
        if action=="finish":
            return{"ok":True}
        handler=TOOL_HANDLERS[action]
        output = handler()
        return{"ok":True,"data":output}

    def update(self, action, result):
        if action == "finish":
            self.state["done"] = True
        elif result["ok"]:
            self.state["history"].append(action)

    def run(self):
        while self.state["done"]==False:
            action = self.decide()
            result = self.act(action)
            update = self.update(action, result)

agent = Agent()
agent.run()
print(agent.state)