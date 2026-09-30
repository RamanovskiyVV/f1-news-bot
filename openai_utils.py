"""Compatibility helpers for OpenAI text-model request parameters."""
from __future__ import annotations

from typing import Any


def chat_completion_options(
    model: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Return parameters supported by both current reasoning and legacy models.

    GPT-6.1 Sol and Astra do not support ``reasoning_effort='none'`` and reject
    sampling parameters while reasoning is enabled. GPT-6 Sol/Luna can run with
    no reasoning for low-latency classification and translation workloads.
    """
    options: dict[str, Any] = {}
    normalized = model.lower()
    is_gpt6 = normalized.startswith("gpt-6")

    if normalized.startswith(("gpt-6.1-", "gpt-6-astra")):
        options["reasoning_effort"] = "low"
    elif is_gpt6:
        options["reasoning_effort"] = "none"
        if temperature is not None:
            options["temperature"] = temperature
    elif temperature is not None:
        options["temperature"] = temperature

    if max_tokens is not None:
        key = "max_completion_tokens" if is_gpt6 else "max_tokens"
        options[key] = max_tokens

    return options
