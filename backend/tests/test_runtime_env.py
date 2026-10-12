"""Tests for server-side runtime environment loading."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from infra.config import load_runtime_env


class RuntimeEnvTests(unittest.TestCase):
    def write_env(self, directory: str) -> Path:
        env_file = Path(directory) / ".env.cluster"
        env_file.write_text(
            "# test configuration\n"
            "LLM_BASE_URL=https://example.test/v1/\n"
            "LLM_MODEL=example-model\n"
            "LLM_API_KEY='example-key'\n",
            encoding="utf-8",
        )
        return env_file

    def test_loads_runtime_values_from_explicit_file(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = self.write_env(directory)

            self.assertEqual(env_file, load_runtime_env(env_file))
            self.assertEqual("https://example.test/v1/", os.environ["LLM_BASE_URL"])
            self.assertEqual("example-model", os.environ["LLM_MODEL"])
            self.assertEqual("example-key", os.environ["LLM_API_KEY"])

    def test_explicit_process_values_take_precedence(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LLM_MODEL": "process-model"}, clear=True
        ):
            env_file = self.write_env(directory)

            load_runtime_env(env_file)

            self.assertEqual("process-model", os.environ["LLM_MODEL"])

    def test_missing_file_is_ignored(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(load_runtime_env("not-present.env"))


if __name__ == "__main__":
    unittest.main()
