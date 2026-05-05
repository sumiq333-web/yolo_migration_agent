from tools.common_tool import _extract_code_anchors

text = open(r"F:\python\YOLO-NEW\ultralytics-main\ultralytics\nn\tasks.py", "r", encoding="utf-8").read()
print(_extract_code_anchors(text))