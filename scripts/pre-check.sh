#!/usr/bin/env bash
# Compatibility wrapper for the former single-node runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export SCENARIO_DIR="${SCENARIO_DIR:-$HOME/aiops/scenarios/sock-shop}"
export API_BASE="${API_BASE:-http://localhost:8000}"
export EXPECTED_NODE_COUNT="${EXPECTED_NODE_COUNT:-1}"
export REQUIRE_CHAOS_MESH="${REQUIRE_CHAOS_MESH:-true}"

exec bash "$REPO_ROOT/cluster-port/scripts/03-pre-check-cluster.sh" "$@"
