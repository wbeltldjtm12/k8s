"""Unit tests for evaluation snapshot reservation semantics."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = BACKEND_DIR / "main.py"


class FakeJSONResponse:
    def __init__(self, *, status_code: int, content: dict):
        self.status_code = status_code
        self.content = content
        self.body = json.dumps(content).encode("utf-8")


class FakeFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        pass

    def _route(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator

    get = _route
    post = _route
    delete = _route


class DummyComponent:
    def __init__(self, *args, **kwargs):
        pass


class FakeCache:
    collection_failure_count = 0


class FakeGraph:
    def __init__(self, cache):
        self.cache = cache
        self.nodes = {}
        self.forward_edges = {}

    def find_root_causes(self):
        return []

    def find_root_causes_dfs_only(self):
        return []

    def get_error_summary_for_llm(self):
        return "정상 상태 (이상 노드 없음)"


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []
    return module


def _load_main_module():
    fastapi = _package("fastapi")
    fastapi.FastAPI = FakeFastAPI
    fastapi_middleware = _package("fastapi.middleware")
    fastapi_cors = ModuleType("fastapi.middleware.cors")
    fastapi_cors.CORSMiddleware = DummyComponent
    fastapi_responses = ModuleType("fastapi.responses")
    fastapi_responses.JSONResponse = FakeJSONResponse

    infra = _package("infra")
    infra_config = ModuleType("infra.config")
    infra_config.load_k8s_config = lambda: None
    infra_cache = ModuleType("infra.cache")
    infra_cache.ResourceCache = FakeCache
    infra_metrics = ModuleType("infra.metrics")
    infra_metrics.MetricsAnalyzer = DummyComponent
    infra_network = ModuleType("infra.network_evidence")
    infra_network.NetworkEvidenceCollectionError = type(
        "NetworkEvidenceCollectionError",
        (RuntimeError,),
        {},
    )
    infra_network.collect_network_evidence = lambda **kwargs: []

    analysis = _package("analysis")
    analysis_analyzers = ModuleType("analysis.analyzers")
    for class_name in (
        "PodAnalyzer",
        "DeploymentAnalyzer",
        "NodeAnalyzer",
        "ServiceAnalyzer",
        "IngressAnalyzer",
        "PVCAnalyzer",
        "HPAAnalyzer",
        "JobAnalyzer",
        "CronJobAnalyzer",
        "PVAnalyzer",
    ):
        setattr(analysis_analyzers, class_name, DummyComponent)
    analysis_graph = ModuleType("analysis.graph")
    analysis_graph.DependencyGraph = FakeGraph
    analysis_events = ModuleType("analysis.events")
    analysis_events.EventAnalyzer = DummyComponent

    intelligence = _package("intelligence")
    intelligence_llm = ModuleType("intelligence.llm")
    intelligence_llm.LLMClient = DummyComponent
    intelligence_llm.Retriever = DummyComponent
    intelligence_llm.build_rag_query = lambda *args, **kwargs: ""
    intelligence_llm.redact_provider_error = str

    stubs = {
        "fastapi": fastapi,
        "fastapi.middleware": fastapi_middleware,
        "fastapi.middleware.cors": fastapi_cors,
        "fastapi.responses": fastapi_responses,
        "infra": infra,
        "infra.config": infra_config,
        "infra.cache": infra_cache,
        "infra.metrics": infra_metrics,
        "infra.network_evidence": infra_network,
        "analysis": analysis,
        "analysis.analyzers": analysis_analyzers,
        "analysis.graph": analysis_graph,
        "analysis.events": analysis_events,
        "intelligence": intelligence,
        "intelligence.llm": intelligence_llm,
    }
    module_name = "kubein_main_snapshot_contract"
    spec = importlib.util.spec_from_file_location(module_name, MAIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load backend/main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
    return module


class EvaluationSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = _load_main_module()

    def setUp(self) -> None:
        with self.main._eval_snapshot_lock:
            self.main._eval_snapshots.clear()
            self.main._expired_eval_snapshot_ids.clear()
            self.main._cancelled_eval_snapshot_ids.clear()

    def tearDown(self) -> None:
        with self.main._eval_snapshot_lock:
            self.main._eval_snapshots.clear()
            self.main._expired_eval_snapshot_ids.clear()
            self.main._cancelled_eval_snapshot_ids.clear()

    def test_supplied_client_snapshot_id_is_reserved_and_returned(self) -> None:
        snapshot_id = "0123456789abcdef0123456789abcdef"

        response = self.main.create_evaluation_snapshot(snapshot_id)

        self.assertIsInstance(response, dict)
        self.assertEqual("success", response["status"])
        self.assertEqual(snapshot_id, response["snapshot_id"])
        self.assertGreater(response["expires_in_sec"], 0)
        self.assertEqual(
            "ready",
            self.main._eval_snapshots[snapshot_id]["state"],
        )

    def test_empty_id_generates_uuid_and_invalid_client_ids_are_rejected(self) -> None:
        generated = self.main.create_evaluation_snapshot("")
        self.assertRegex(generated["snapshot_id"], r"^[0-9a-f]{32}$")

        for snapshot_id in (
            "abc",
            "G" * 32,
            "0" * 31,
            "0" * 33,
            " " + "0" * 32,
            "../" + "0" * 32,
        ):
            with self.subTest(snapshot_id=snapshot_id):
                response = self.main.create_evaluation_snapshot(snapshot_id)
                self.assertEqual(400, response.status_code)
                self.assertEqual("error", response.content["status"])

    def test_duplicate_client_snapshot_id_returns_conflict(self) -> None:
        snapshot_id = "a" * 32
        first = self.main.create_evaluation_snapshot(snapshot_id)
        original_record = self.main._eval_snapshots[snapshot_id]

        duplicate = self.main.create_evaluation_snapshot(snapshot_id)

        self.assertEqual("success", first["status"])
        self.assertEqual(409, duplicate.status_code)
        self.assertIs(
            original_record,
            self.main._eval_snapshots[snapshot_id],
        )

    def test_normal_release_does_not_consume_cancellation_capacity(
        self,
    ) -> None:
        snapshot_id = "f" * 32
        created = self.main.create_evaluation_snapshot(snapshot_id)

        released = self.main.release_evaluation_snapshot(snapshot_id)

        self.assertEqual("success", created["status"])
        self.assertEqual({"status": "ok", "released": True}, released)
        self.assertNotIn(snapshot_id, self.main._eval_snapshots)
        self.assertNotIn(
            snapshot_id,
            self.main._cancelled_eval_snapshot_ids,
        )

    def test_general_health_redacts_configured_llm_base_url(self) -> None:
        private_url = (
            "https://user:password@example.invalid/v1"
            "?api_key=private-value"
        )
        with patch.dict(
            os.environ,
            {"LLM_BASE_URL": private_url},
            clear=False,
        ), patch.object(
            self.main,
            "redact_provider_error",
            return_value="https://[redacted]@example.invalid/v1"
            "?api_key=[redacted]",
        ) as redactor, patch.object(
            self.main,
            "retriever",
            type("UnavailableRetriever", (), {"_ready": False})(),
        ):
            response = self.main.health_check()

        redactor.assert_called_once_with(private_url)
        rendered_url = response["llm"]["base_url"]
        self.assertNotIn("password", rendered_url)
        self.assertNotIn("private-value", rendered_url)

    def test_zero_error_snapshot_modes_never_call_provider(self) -> None:
        snapshot_id = "1" * 32

        class CleanGraph:
            def __init__(self, cache):
                self.cache = cache
                self.nodes = {}
                self.forward_edges = {}

            def find_root_causes(self):
                return []

            def find_root_causes_dfs_only(self):
                return []

            def get_error_summary_for_llm(self):
                return "정상 상태 (이상 노드 없음)"

        llm = Mock()
        retriever = Mock()
        metrics_analyzer = Mock(
            side_effect=AssertionError(
                "zero-error hybrid must not collect LLM metrics"
            )
        )
        rag_query = Mock(
            side_effect=AssertionError(
                "zero-error modes must not build a RAG query"
            )
        )

        with patch.dict(
            os.environ,
            {"KUBEIN_NETWORK_EVIDENCE_ENABLED": "false"},
            clear=False,
        ), patch.object(
            self.main,
            "DependencyGraph",
            CleanGraph,
        ), patch.object(
            self.main,
            "llm",
            llm,
        ), patch.object(
            self.main,
            "retriever",
            retriever,
        ), patch.object(
            self.main,
            "MetricsAnalyzer",
            metrics_analyzer,
        ), patch.object(
            self.main,
            "_build_rag_query",
            rag_query,
        ):
            created = self.main.create_evaluation_snapshot(snapshot_id)
            responses = {
                mode: self.main.evaluate(mode=mode, snapshot_id=snapshot_id)
                for mode in ("hybrid", "dfs_only", "llm_only")
            }

        self.assertEqual(0, created["error_node_count"])
        for mode, response in responses.items():
            with self.subTest(mode=mode):
                self.assertEqual("success", response["status"])
                self.assertEqual(mode, response["mode"])
                self.assertEqual(snapshot_id, response["snapshot_id"])
                self.assertEqual(
                    created["captured_at"],
                    response["snapshot_captured_at"],
                )
                self.assertEqual(0, response["error_node_count"])
        for mode in ("hybrid", "llm_only"):
            self.assertEqual(
                "not_applicable",
                responses[mode]["data"]["ai_status"],
            )
            self.assertEqual("", responses[mode]["data"]["ai_analysis"])
        llm.analyze.assert_not_called()
        retriever.search_texts_only.assert_not_called()
        metrics_analyzer.assert_not_called()
        rag_query.assert_not_called()

    def test_network_collection_failure_rejects_snapshot_and_releases_slot(
        self,
    ) -> None:
        snapshot_id = "2" * 32
        failure = self.main.NetworkEvidenceCollectionError(
            "network evidence unavailable"
        )

        with patch.object(
            self.main,
            "_augment_graph_with_network_evidence",
            side_effect=failure,
        ):
            response = self.main.create_evaluation_snapshot(snapshot_id)

        self.assertEqual(503, response.status_code)
        self.assertEqual("error", response.content["status"])
        self.assertNotIn(snapshot_id, self.main._eval_snapshots)

    def test_live_evaluation_reports_network_unavailability_as_503(
        self,
    ) -> None:
        failure = self.main.NetworkEvidenceCollectionError(
            "network probe scrape unavailable"
        )

        with patch.dict(
            self.main._cache_store,
            {"cache": None, "graph": None, "results": None, "expires_at": 0},
            clear=True,
        ), patch.object(
            self.main,
            "_augment_graph_with_network_evidence",
            side_effect=failure,
        ):
            response = self.main.evaluate(mode="dfs_only")

        self.assertEqual(503, response.status_code)
        self.assertEqual("error", response.content["status"])

    def test_analyze_and_causality_report_network_unavailability_as_503(
        self,
    ) -> None:
        failure = self.main.NetworkEvidenceCollectionError(
            "network probe scrape unavailable"
        )

        for endpoint in (
            self.main.analyze_cluster,
            self.main.causality_fast,
        ):
            with self.subTest(endpoint=endpoint.__name__), patch.object(
                self.main,
                "_get_or_build_graph",
                side_effect=failure,
            ):
                response = endpoint()

            self.assertEqual(503, response.status_code)
            self.assertEqual("error", response.content["status"])

    def test_required_network_evidence_reuses_resources_but_refreshes_live_graph(
        self,
    ) -> None:
        cached_cache = object()
        stale_graph = FakeGraph(cached_cache)
        fresh_graph = FakeGraph(cached_cache)
        failure = self.main.NetworkEvidenceCollectionError(
            "network probe scrape unavailable"
        )
        resource_cache = Mock(side_effect=AssertionError(
            "unexpired ResourceCache should be reused"
        ))
        dependency_graph = Mock(return_value=fresh_graph)
        augment = Mock(side_effect=failure)

        with patch.dict(
            os.environ,
            {
                "KUBEIN_NETWORK_EVIDENCE_ENABLED": "true",
                "KUBEIN_NETWORK_EVIDENCE_REQUIRED": "true",
            },
        ), patch.dict(
            self.main._cache_store,
            {
                "cache": cached_cache,
                "graph": stale_graph,
                "results": [],
                "expires_at": self.main.time.time() + 60,
            },
            clear=True,
        ), patch.object(
            self.main,
            "ResourceCache",
            resource_cache,
        ), patch.object(
            self.main,
            "DependencyGraph",
            dependency_graph,
        ), patch.object(
            self.main,
            "_augment_graph_with_network_evidence",
            augment,
        ):
            response = self.main.evaluate(mode="dfs_only")

        self.assertEqual(503, response.status_code)
        resource_cache.assert_not_called()
        dependency_graph.assert_called_once_with(cached_cache)
        augment.assert_called_once_with(fresh_graph)

    def test_snapshot_collects_network_evidence_once_for_all_modes(self) -> None:
        snapshot_id = "3" * 32
        augment = Mock(return_value=0)

        with patch.object(
            self.main,
            "_augment_graph_with_network_evidence",
            augment,
        ):
            created = self.main.create_evaluation_snapshot(snapshot_id)
            responses = {
                mode: self.main.evaluate(mode=mode, snapshot_id=snapshot_id)
                for mode in ("hybrid", "dfs_only", "llm_only")
            }

        self.assertEqual("success", created["status"])
        augment.assert_called_once()
        _, kwargs = augment.call_args
        self.assertTrue(kwargs["strict"])
        self.assertIsInstance(kwargs["reference_time"], float)
        for response in responses.values():
            self.assertEqual(snapshot_id, response["snapshot_id"])
            self.assertEqual(
                created["captured_at"],
                response["snapshot_captured_at"],
            )

    def test_delete_unknown_id_cancels_delayed_create_without_collecting(
        self,
    ) -> None:
        snapshot_id = "d" * 32

        class UnexpectedCache:
            calls = 0

            def __init__(self):
                type(self).calls += 1
                raise AssertionError("cancelled snapshot must not collect")

        released = self.main.release_evaluation_snapshot(snapshot_id)
        with patch.object(self.main, "ResourceCache", UnexpectedCache):
            create_response = self.main.create_evaluation_snapshot(
                snapshot_id
            )

        self.assertEqual(
            {"status": "ok", "released": False},
            released,
        )
        self.assertEqual(409, create_response.status_code)
        self.assertEqual(0, UnexpectedCache.calls)
        self.assertIn(
            snapshot_id,
            self.main._cancelled_eval_snapshot_ids,
        )

    def test_cancellation_tombstones_are_bounded_to_configured_cap(
        self,
    ) -> None:
        count = self.main.MAX_EVAL_SNAPSHOT_TOMBSTONES + 1
        snapshot_ids = [f"{index:032x}" for index in range(count)]

        with patch.object(
            self.main.time,
            "monotonic",
            side_effect=range(100, 100 + count),
        ):
            for snapshot_id in snapshot_ids:
                self.main.release_evaluation_snapshot(snapshot_id)

        self.assertLessEqual(
            len(self.main._cancelled_eval_snapshot_ids),
            self.main.MAX_EVAL_SNAPSHOT_TOMBSTONES,
        )
        self.assertNotIn(
            snapshot_ids[0],
            self.main._cancelled_eval_snapshot_ids,
        )
        self.assertIn(
            snapshot_ids[-1],
            self.main._cancelled_eval_snapshot_ids,
        )

    def test_expired_cancellation_tombstone_is_pruned(self) -> None:
        snapshot_id = "e" * 32
        created_at = 100.0

        with patch.object(
            self.main.time,
            "monotonic",
            return_value=created_at,
        ):
            self.main.release_evaluation_snapshot(snapshot_id)
        self.assertIn(
            snapshot_id,
            self.main._cancelled_eval_snapshot_ids,
        )

        with patch.object(
            self.main.time,
            "monotonic",
            return_value=created_at + self.main.EVAL_SNAPSHOT_TTL,
        ):
            response = self.main.create_evaluation_snapshot(snapshot_id)

        self.assertEqual("success", response["status"])
        self.assertNotIn(
            snapshot_id,
            self.main._cancelled_eval_snapshot_ids,
        )

    def test_delete_building_reservation_makes_creator_conflict_and_cleans_registry(
        self,
    ) -> None:
        snapshot_id = "b" * 32
        build_started = Event()
        allow_build_to_finish = Event()
        responses = []

        class BlockingCache:
            collection_failure_count = 0

            def __init__(self):
                build_started.set()
                if not allow_build_to_finish.wait(timeout=3):
                    raise RuntimeError("test did not release snapshot builder")

        def create_snapshot():
            responses.append(
                self.main.create_evaluation_snapshot(snapshot_id)
            )

        with patch.object(self.main, "ResourceCache", BlockingCache):
            thread = Thread(target=create_snapshot, daemon=True)
            thread.start()
            try:
                self.assertTrue(build_started.wait(timeout=2))
                self.assertEqual(
                    "building",
                    self.main._eval_snapshots[snapshot_id]["state"],
                )

                released = self.main.release_evaluation_snapshot(snapshot_id)
                self.assertEqual(
                    {"status": "ok", "released": True},
                    released,
                )
                self.assertNotIn(snapshot_id, self.main._eval_snapshots)
                self.assertNotIn(
                    snapshot_id,
                    self.main._cancelled_eval_snapshot_ids,
                )
            finally:
                allow_build_to_finish.set()
                thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(responses))
        self.assertEqual(409, responses[0].status_code)
        self.assertNotIn(snapshot_id, self.main._eval_snapshots)

    def test_cancelled_failing_builder_does_not_pop_reused_client_id(self) -> None:
        snapshot_id = "c" * 32
        first_build_started = Event()
        allow_first_build_to_fail = Event()
        first_responses = []

        class FirstBlockingThenHealthyCache:
            collection_failure_count = 0
            calls = 0

            def __init__(self):
                type(self).calls += 1
                if type(self).calls == 1:
                    first_build_started.set()
                    if not allow_first_build_to_fail.wait(timeout=3):
                        raise RuntimeError("test did not release first builder")
                    raise RuntimeError("simulated first build failure")

        def create_first_snapshot():
            first_responses.append(
                self.main.create_evaluation_snapshot(snapshot_id)
            )

        with patch.object(
            self.main,
            "ResourceCache",
            FirstBlockingThenHealthyCache,
        ):
            first_thread = Thread(
                target=create_first_snapshot,
                daemon=True,
            )
            first_thread.start()
            try:
                self.assertTrue(first_build_started.wait(timeout=2))
                self.main.release_evaluation_snapshot(snapshot_id)

                replacement_response = (
                    self.main.create_evaluation_snapshot(snapshot_id)
                )
                replacement_record = self.main._eval_snapshots[snapshot_id]
                self.assertEqual("success", replacement_response["status"])
            finally:
                allow_first_build_to_fail.set()
                first_thread.join(timeout=3)

        self.assertFalse(first_thread.is_alive())
        self.assertEqual(409, first_responses[0].status_code)
        self.assertIs(
            replacement_record,
            self.main._eval_snapshots[snapshot_id],
        )
        self.assertEqual("ready", replacement_record["state"])


if __name__ == "__main__":
    unittest.main()
