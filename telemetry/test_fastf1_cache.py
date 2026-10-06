"""Regression test for FastF1 fallback cache setup."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import fastf1_client


class _Cache:
    enabled_path: str | None = None

    @classmethod
    def enable_cache(cls, path: str) -> None:
        cls.enabled_path = path


class _FastF1:
    Cache = _Cache


class FastF1CacheTests(unittest.TestCase):
    def test_cache_directory_is_created_before_enable(self) -> None:
        old_path = fastf1_client._CACHE_DIR
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                cache_dir = Path(temp_dir) / "missing" / "fastf1"
                fastf1_client._CACHE_DIR = cache_dir
                fastf1_client._enable_cache(_FastF1)
                self.assertTrue(cache_dir.is_dir())
                self.assertEqual(str(cache_dir), _Cache.enabled_path)
        finally:
            fastf1_client._CACHE_DIR = old_path


if __name__ == "__main__":
    unittest.main()
