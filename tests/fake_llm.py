"""A scripted stand-in for the OpenAI client.

Each scripted reply is either a str (plain text content) or a list of
(tool_name, args_dict) tuples (tool calls).
"""

import json
from types import SimpleNamespace


def _message(reply, index):
    if isinstance(reply, str):
        return SimpleNamespace(content=reply, tool_calls=None)
    calls = [
        SimpleNamespace(
            id=f"call_{index}_{i}",
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(reply)
    ]
    return SimpleNamespace(content=None, tool_calls=calls)


class _Completions:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        # Snapshot messages: the builder mutates its history after each call.
        kwargs = dict(kwargs, messages=json.loads(json.dumps(kwargs.get("messages", []))))
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("fake LLM ran out of scripted replies")
        message = _message(self._replies.pop(0), len(self.calls))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedClient:
    def __init__(self, replies):
        self.chat = SimpleNamespace(completions=_Completions(replies))

    @property
    def calls(self):
        return self.chat.completions.calls


README = "# wylt\n\n## Overview\nx\n\n## How it was built\nI built it.\n"
PASSING_TEST = "def test_ok():\n    assert True\n"
FAILING_TEST = "def test_fails():\n    assert False\n"


def build_python_project(test_body=PASSING_TEST):
    """Tool calls that produce a minimal, verifiable Python project."""
    return [
        ("write_file", {"path": "test_smoke.py", "content": test_body}),
        ("write_file", {"path": "README.md", "content": README}),
    ]
