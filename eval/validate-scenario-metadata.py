#!/usr/bin/env python3
"""Validate and resolve canonical per-scenario runner metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SCHEMA_VERSION = 1
SETTLE_KEY = "observation_settle_seconds"
RECOVERY_SETTLE_KEY = "recovery_settle_seconds"
SETTLE_KEYS = (SETTLE_KEY, RECOVERY_SETTLE_KEY)
MIN_SETTLE_SECONDS = 0
MAX_SETTLE_SECONDS = 20
TOP_LEVEL_KEYS = {"schema_version", "defaults", "scenarios"}
SCENARIO_ID_PATTERN = re.compile(
    r"^[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*$"
)


class MetadataError(ValueError):
    """Raised when scenario metadata violates the runner contract."""


def load_scenarios(path: Path) -> list[str]:
    scenarios = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not scenarios:
        raise MetadataError("scenario manifest is empty")
    if len(scenarios) != len(set(scenarios)):
        raise MetadataError("scenario manifest contains duplicate IDs")
    invalid = [
        scenario
        for scenario in scenarios
        if SCENARIO_ID_PATTERN.fullmatch(scenario) is None
    ]
    if invalid:
        raise MetadataError(
            f"scenario manifest contains invalid IDs: {','.join(invalid)}"
        )
    return scenarios


def require_settle_seconds(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MetadataError(f"{context} must be an integer")
    if not MIN_SETTLE_SECONDS <= value <= MAX_SETTLE_SECONDS:
        raise MetadataError(
            f"{context} must be between "
            f"{MIN_SETTLE_SECONDS} and {MAX_SETTLE_SECONDS}"
        )
    return value


def resolve_metadata(
    metadata_path: Path,
    scenarios: list[str],
) -> dict[str, dict[str, int]]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                raise MetadataError(f"duplicate JSON key: {key}")
            parsed[key] = value
        return parsed

    try:
        document = json.loads(
            metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError(f"cannot read metadata: {exc}") from exc

    if not isinstance(document, dict):
        raise MetadataError("top-level metadata must be an object")
    unknown_top = sorted(set(document) - TOP_LEVEL_KEYS)
    missing_top = sorted(TOP_LEVEL_KEYS - set(document))
    if unknown_top or missing_top:
        raise MetadataError(
            f"top-level keys mismatch: missing={missing_top}, "
            f"unknown={unknown_top}"
        )
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise MetadataError(
            f"schema_version must be {SCHEMA_VERSION}"
        )

    defaults = document["defaults"]
    if not isinstance(defaults, dict) or set(defaults) != set(SETTLE_KEYS):
        raise MetadataError(
            f"defaults must contain exactly {','.join(SETTLE_KEYS)}"
        )
    resolved_defaults = {
        key: require_settle_seconds(
            defaults[key],
            f"defaults.{key}",
        )
        for key in SETTLE_KEYS
    }

    overrides = document["scenarios"]
    if not isinstance(overrides, dict):
        raise MetadataError("scenarios must be an object")
    scenario_set = set(scenarios)
    unknown_scenarios = sorted(set(overrides) - scenario_set)
    if unknown_scenarios:
        raise MetadataError(
            f"unknown scenario IDs: {','.join(unknown_scenarios)}"
        )

    resolved: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        config = overrides.get(scenario, {})
        if not isinstance(config, dict):
            raise MetadataError(f"scenarios.{scenario} must be an object")
        unknown_keys = sorted(set(config) - set(SETTLE_KEYS))
        if unknown_keys:
            raise MetadataError(
                f"scenarios.{scenario} has unknown keys: "
                f"{','.join(unknown_keys)}"
            )
        resolved[scenario] = {
            key: require_settle_seconds(
                config.get(key, resolved_defaults[key]),
                f"scenarios.{scenario}.{key}",
            )
            for key in SETTLE_KEYS
        }
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        scenarios = load_scenarios(args.manifest)
        resolved = resolve_metadata(args.metadata, scenarios)
    except MetadataError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    for scenario in scenarios:
        values = resolved[scenario]
        print(
            f"{scenario}\t{values[SETTLE_KEY]}"
            f"\t{values[RECOVERY_SETTLE_KEY]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
