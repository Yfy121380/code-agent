# 工具 schema 转换：把 codemate 内部工具描述转换为不同模型后端需要的格式。

def _tool_specs_to_openai(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _tool_specs_to_openai_chat(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


def _tool_specs_to_anthropic(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "input_schema": spec.get("input_schema", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _tool_specs_to_ollama(tools):
    converted = []
    for tool in tools or []:
        spec = dict(tool)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted
