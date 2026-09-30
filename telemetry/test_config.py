"""Regression tests for the current 2026 driver/team metadata."""
from __future__ import annotations

import unittest

from .config import DRIVERS, RACING_NUMBER_TO_ACR, TEAM_NAMES
from .image_gen import TEAM_COLORS


class DriverConfigTests(unittest.TestCase):
    def test_current_2026_lineups(self) -> None:
        expected = {
            "NOR": "mclaren",
            "PIA": "mclaren",
            "LEC": "ferrari",
            "HAM": "ferrari",
            "RUS": "mercedes",
            "ANT": "mercedes",
            "VER": "red_bull",
            "HAD": "red_bull",
            "LAW": "racing_bulls",
            "LIN": "racing_bulls",
            "GAS": "alpine",
            "COL": "alpine",
            "OCO": "haas",
            "BEA": "haas",
            "HUL": "audi",
            "BOR": "audi",
            "SAI": "williams",
            "ALB": "williams",
            "ALO": "aston_martin",
            "STR": "aston_martin",
            "PER": "cadillac",
            "BOT": "cadillac",
        }
        for acronym, team in expected.items():
            self.assertEqual(team, DRIVERS[acronym]["team"])

    def test_2026_champion_numbers_and_new_teams(self) -> None:
        self.assertEqual("NOR", RACING_NUMBER_TO_ACR[1])
        self.assertEqual("VER", RACING_NUMBER_TO_ACR[3])
        self.assertEqual("LIN", RACING_NUMBER_TO_ACR[41])
        self.assertEqual("Audi", TEAM_NAMES["audi"])
        self.assertEqual("Cadillac", TEAM_NAMES["cadillac"])
        self.assertIn("audi", TEAM_COLORS)


if __name__ == "__main__":
    unittest.main()
