"""Helpers for validating LiteLLM response shapes."""

from typing import Any


class EmptyLLMChoicesError(RuntimeError):
    """Raised when a non-streaming provider response has no choices."""


def first_choice(
    response: Any,
    *,
    model_name: str | None = None,
    operation: str = "LLM response",
) -> Any:
    choices = getattr(response, "choices", None)
    if choices:
        return choices[0]

    model = model_name or "unknown model"
    raise EmptyLLMChoicesError(f"{operation} returned empty choices from {model}")
