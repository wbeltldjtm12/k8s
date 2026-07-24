# Experiment script layout

- `../cluster-port/scripts/03-05`: canonical pre-check, one-cycle, and repeated evaluation runners.
- `pre-check.sh`, `ablation-eval.sh`, `ablation-5cycle.sh`: compatibility wrappers using the former single-node defaults.
- `legacy/ablation-eval-4mode.sh`: preserved 21-scenario/four-mode research artifact; do not connect it to the current 5-cycle runner.

The canonical runner supports both profiles through `SCENARIO_DIR`, `API_BASE`,
`EXPECTED_NODE_COUNT`, and `REQUIRE_CHAOS_MESH`, and all stages share
`../eval/scenarios.txt` as the canonical scenario manifest. The original pre-cleanup
implementations are preserved in the timestamped workspace snapshot.
