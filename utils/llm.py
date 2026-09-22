import os
from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"


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
    return response.choices[0].message.content
