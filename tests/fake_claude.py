"""A scripted stand-in for the Anthropic client: returns queued responses and records every request."""
import copy
import json
from types import SimpleNamespace


def text(body: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)], stop_reason=stop_reason)


def tool_calls(*calls):
    """calls: (name, input) pairs; returned in one assistant turn like parallel tool use."""
    blocks = [SimpleNamespace(type="tool_use", id=f"tu_{n}", name=name, input=args) for n, (name, args) in enumerate(calls)]
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))  # the caller keeps appending to its message list
        return self.responses.pop(0)

    def sent(self) -> str:
        """Everything sent to the API, as text, for privacy assertions."""
        return json.dumps(self.calls, default=lambda o: getattr(o, "__dict__", str(o)))
