import pytest
from utils import llm


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class _FakeClient:
    def __init__(self, content="hello"):
        self.chat = _FakeChat(content)


def test_get_client_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.get_client()


def test_get_client_builds_client_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    client = llm.get_client()
    assert client.base_url is not None


def test_generate_returns_message_content():
    fake = _FakeClient(content="generated text")
    result = llm.generate(fake, system="sys", user="usr")
    assert result == "generated text"


def test_generate_sends_system_and_user_messages():
    fake = _FakeClient(content="ignored")
    llm.generate(fake, system="be terse", user="write code")
    sent_messages = fake.chat.completions.last_kwargs["messages"]
    assert sent_messages == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "write code"},
    ]


def test_generate_raises_on_empty_content():
    fake = _FakeClient(content=None)
    with pytest.raises(RuntimeError, match="empty response"):
        llm.generate(fake, system="s", user="u")


def test_generate_uses_default_model_when_not_specified():
    fake = _FakeClient()
    llm.generate(fake, system="s", user="u")
    assert fake.chat.completions.last_kwargs["model"] == llm.MODEL
