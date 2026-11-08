import logging
import os
import time
from datetime import datetime, timezone

from openai import OpenAI, RateLimitError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = os.environ.get("BOTPY8_SOUL_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
RATE_LIMIT_WAITS = (15, 30, 60, 60, 60)


def get_client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is not set")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


class QuotaExhausted(RuntimeError):
    """The provider's daily request quota is used up; retrying before `resets_at` is pointless."""

    def __init__(self, model: str, resets_at: datetime | None):
        when = resets_at.strftime("%Y-%m-%d %H:%M UTC") if resets_at else "unknown"
        super().__init__(f"daily request quota for {model} exhausted (resets {when})")
        self.resets_at = resets_at


def _reset_time(exc: RateLimitError) -> datetime | None:
    try:
        reset_ms = int(exc.body["metadata"]["headers"]["X-RateLimit-Reset"])
    except (TypeError, KeyError, ValueError):
        return None
    return datetime.fromtimestamp(reset_ms / 1000, timezone.utc)


def _create(client, model: str, **kwargs):
    """chat.completions.create with rate-limit handling.

    Free OpenRouter models are limited to ~20 requests/minute; a fast agent
    loop hits that, so per-minute limits wait for the window to reset rather
    than killing a long build. A daily quota won't clear in a minute, so that
    raises QuotaExhausted for the orchestrator to pause on.
    """
    for attempt in range(len(RATE_LIMIT_WAITS) + 1):
        try:
            return client.chat.completions.create(model=model, **kwargs)
        except RateLimitError as exc:
            if "per-day" in str(exc):
                raise QuotaExhausted(model, _reset_time(exc)) from exc
            if attempt == len(RATE_LIMIT_WAITS):
                raise
            wait = RATE_LIMIT_WAITS[attempt]
            logging.warning("rate limited by %s; retrying in %ds", model, wait)
            time.sleep(wait)


def generate(client, system: str, user: str, model: str = MODEL) -> str:
    response = _create(
        client,
        model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned an empty response")
    return content


def chat(client, messages: list[dict], tools: list[dict], model: str = MODEL):
    """One tool-calling turn. Returns the assistant message object."""
    response = _create(client, model, messages=messages, tools=tools, tool_choice="auto")
    if not response.choices:
        raise RuntimeError("LLM returned no choices")
    return response.choices[0].message


def assistant_message_dict(message) -> dict:
    entry = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ]
    return entry
