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


def _rate_limit_error(message, reset_ms=None):
    from openai import RateLimitError

    class _RateLimit(RateLimitError):
        def __init__(self):  # skip the HTTP response plumbing; chat only reads str() and .body
            Exception.__init__(self, message)
            self.message = message
            self.body = {"message": message, "code": 429}
            if reset_ms is not None:
                self.body["metadata"] = {"headers": {"X-RateLimit-Reset": str(reset_ms)}}

    return _RateLimit()


class _FlakyCompletions:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        from types import SimpleNamespace
        return SimpleNamespace(choices=[SimpleNamespace(message="ok")])


def _client_with(completions):
    from types import SimpleNamespace
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_chat_waits_out_per_minute_rate_limits(monkeypatch):
    from utils import llm
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    completions = _FlakyCompletions([_rate_limit_error("Rate limit exceeded: free-models-per-min.")])
    assert llm.chat(_client_with(completions), [], []) == "ok"
    assert slept == [llm.RATE_LIMIT_WAITS[0]]


def test_chat_fails_fast_on_daily_quota_with_reset_time(monkeypatch):
    import pytest
    from utils import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("should not wait for a daily quota"))
    completions = _FlakyCompletions([_rate_limit_error("Rate limit exceeded: free-models-per-day.", 1794096000000)])
    with pytest.raises(RuntimeError, match=r"daily request quota .*(resets 2026-11-08 00:00 UTC)"):
        llm.chat(_client_with(completions), [], [])
    assert completions.calls == 1


def test_quota_error_carries_reset_time_and_covers_generate(monkeypatch):
    import pytest
    from datetime import datetime, timezone
    from utils import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("should not wait for a daily quota"))
    completions = _FlakyCompletions([_rate_limit_error("Rate limit exceeded: free-models-per-day.", 1794096000000)])
    with pytest.raises(llm.QuotaExhausted) as info:
        llm.generate(_client_with(completions), "system", "user")
    assert info.value.resets_at == datetime(2026, 11, 8, tzinfo=timezone.utc)
