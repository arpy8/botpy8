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


def generate(client, system: str, user: str, model: str = MODEL) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned an empty response")
    return content


def _reset_time(exc: RateLimitError) -> str:
    try:
        reset_ms = int(exc.body["metadata"]["headers"]["X-RateLimit-Reset"])
    except (TypeError, KeyError, ValueError):
        return "reset time unknown"
    return "resets " + datetime.fromtimestamp(reset_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def chat(client, messages: list[dict], tools: list[dict], model: str = MODEL):
    """One tool-calling turn. Returns the assistant message object.

    Free OpenRouter models are limited to ~20 requests/minute; a fast agent
    loop hits that, so rate-limit errors wait for the window to reset rather
    than killing a long build.
    """
    for attempt in range(len(RATE_LIMIT_WAITS) + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            break
        except RateLimitError as exc:
            if "per-day" in str(exc):
                # A daily quota won't clear in a minute; fail now with the reset time.
                raise RuntimeError(f"daily request quota for {model} exhausted ({_reset_time(exc)})") from exc
            if attempt == len(RATE_LIMIT_WAITS):
                raise
            wait = RATE_LIMIT_WAITS[attempt]
            logging.warning("rate limited by %s; retrying in %ds", model, wait)
            time.sleep(wait)
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
