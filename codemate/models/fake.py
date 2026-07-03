# 测试模型后端：按预设输出返回 ModelResponse，用于单元测试和 benchmark harness。

from .common import _as_model_response, _messages_to_text, _normalize_messages


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.supports_tools = True
        self.last_completion_metadata = {}

    def complete(self, messages, max_new_tokens, tools=None, system=None, **kwargs):
        del max_new_tokens, tools, kwargs
        self.prompts.append(_messages_to_text(_normalize_messages(messages), system=system))
        self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        response = _as_model_response(self.outputs.pop(0))
        self.last_completion_metadata = dict(response.metadata or {})
        return response
