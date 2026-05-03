from types import SimpleNamespace

import pytest

from agent.core.llm_response import EmptyLLMChoicesError, first_choice


def test_first_choice_returns_first_provider_choice():
    first = SimpleNamespace(message=SimpleNamespace(content="ok"))
    response = SimpleNamespace(choices=[first, SimpleNamespace()])

    assert first_choice(response, model_name="openai/test-model") is first


def test_first_choice_raises_clear_error_for_empty_provider_choices():
    response = SimpleNamespace(choices=[])

    with pytest.raises(EmptyLLMChoicesError, match="empty choices"):
        first_choice(
            response,
            model_name="openai/test-model",
            operation="test operation",
        )
