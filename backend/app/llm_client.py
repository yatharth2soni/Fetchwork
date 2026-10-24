"""Minimal OpenAI-compatible chat completion client for ASI:One."""

from __future__ import annotations

import logging
from typing import Any

from openai import OpenAI

from app.config import load_settings

logger = logging.getLogger(__name__)

# 30.0s allows sufficient generation time for a 350-word completion over remote
# LLM endpoints while protecting the agent event loop from hanging.
DEFAULT_LLM_TIMEOUT_SECONDS: float = 30.0


def call_chat_completion(
    messages: list[dict[str, str]],
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_LLM_TIMEOUT_SECONDS,
) -> str:
    """Synchronous chat completion call to OpenAI-compatible provider (ASI:One).

    Executed in a worker thread via asyncio.to_thread by callers.
    """
    if base_url is None or api_key is None or model is None:
        settings = load_settings()
        base_url = base_url or settings.llm_base_url
        api_key = api_key if api_key is not None else settings.llm_api_key
        model = model or settings.llm_model

    client = OpenAI(
        base_url=base_url,
        api_key=api_key or "missing-key",
        timeout=timeout,
    )

    response = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.7,
        timeout=timeout,
    )

    if not response.choices:
        return ""

    choice = response.choices[0]
    content = choice.message.content
    if content is not None:
        return content

    # Check for refusal if content was empty/None
    if choice.message.refusal:
        return ""

    return ""
