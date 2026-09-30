"""Tests for model-specific OpenAI request compatibility."""
from __future__ import annotations

import unittest

from openai_utils import chat_completion_options


class ChatCompletionOptionsTests(unittest.TestCase):
    def test_luna_uses_none_and_keeps_temperature(self) -> None:
        options = chat_completion_options("gpt-6-luna", temperature=0.3)
        self.assertEqual("none", options["reasoning_effort"])
        self.assertEqual(0.3, options["temperature"])

    def test_sol_uses_low_and_drops_temperature(self) -> None:
        options = chat_completion_options(
            "gpt-6.1-sol", temperature=0.7, max_tokens=400
        )
        self.assertEqual("low", options["reasoning_effort"])
        self.assertNotIn("temperature", options)
        self.assertEqual(400, options["max_completion_tokens"])

    def test_legacy_model_keeps_legacy_parameters(self) -> None:
        options = chat_completion_options(
            "gpt-4o-mini", temperature=0.2, max_tokens=100
        )
        self.assertEqual({"temperature": 0.2, "max_tokens": 100}, options)


if __name__ == "__main__":
    unittest.main()
