"""Smoke tests for phone-sized telemetry cards."""
from __future__ import annotations

import io
import unittest

from PIL import Image

from .image_gen import render_race_results_card, render_race_summary_card


class ImageCardTests(unittest.TestCase):
    def test_results_card_is_phone_sized_png(self) -> None:
        data = render_race_results_card(
            {"session_name": "Race", "meeting_name": "Test GP", "date_start": "2026"},
            [{"Position": 1, "Abbreviation": "VER", "Time": None}],
            {},
        )
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual("PNG", image.format)
            self.assertEqual((1080, 1350), image.size)

    def test_live_summary_card_is_phone_sized_png(self) -> None:
        data = render_race_summary_card(
            22,
            57,
            [{
                "position": 1,
                "acronym": "VER",
                "gap": "LEADER",
                "compound": "MEDIUM",
                "tyre_age": 8,
                "pit_count": 1,
            }],
            "Test GP",
        )
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual("PNG", image.format)
            self.assertEqual((1080, 1350), image.size)


if __name__ == "__main__":
    unittest.main()
