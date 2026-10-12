"""Unit contracts for the dependency-free probe exporter."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_PATH = Path(__file__).with_name("app.py")
SPEC = importlib.util.spec_from_file_location("kubein_network_probe_app", APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load network probe app")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class ProbeExporterTests(unittest.TestCase):
    def test_render_includes_probe_observation_timestamp(self) -> None:
        store = APP.ProbeStore()
        store.record(
            APP.ProbeResult(
                probe="dns",
                target="catalogue",
                success=False,
                latency_seconds=0.25,
                observed_at=1234.5,
            )
        )

        metrics = store.render_prometheus()

        self.assertIn("kubein_network_probe_success", metrics)
        self.assertIn(
            "kubein_network_probe_observed_timestamp_seconds",
            metrics,
        )
        self.assertIn("1234.500000", metrics)
        self.assertIn("kubein_network_probe_failures_total", metrics)

    def test_dns_probe_records_wall_clock_completion_time(self) -> None:
        target = APP.ProbeTarget("catalogue", "catalogue.example")
        with (
            patch.object(APP.socket, "getaddrinfo", return_value=[]),
            patch.object(APP.time, "time", return_value=4321.25),
        ):
            result = APP.probe_dns(target, timeout=0.1)

        self.assertTrue(result.success)
        self.assertEqual(4321.25, result.observed_at)


if __name__ == "__main__":
    unittest.main()
