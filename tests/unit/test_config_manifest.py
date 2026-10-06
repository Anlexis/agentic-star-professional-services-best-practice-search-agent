# SVC-C2-005 — Unit Tests: manifest / runtime-config consistency
#
# Two files, two jobs, and both are live rather than documentation:
#   config/agent.yaml   the static registration manifest — identity only. The
#                       registry reads every key at ROOT level.
#   config/config.yaml  every runtime parameter. The registry loads it and passes
#                       it as Graph(config=...); src/api/server.py does the same.
#
# These tests pin the manifest against the code it names, and pin the runtime
# declaration against the settings the graph actually forwards — a drift in either
# direction fails here rather than degrading silently at runtime.
#
# Mirrors docs/03_test_spec.md §2.8 (CFG-01..CFG-08). Deterministic — no network.

import json
import pathlib

import yaml

from framework.schemas.trust_level import TrustLevel

from src.graph.graph import (
    KnowledgeSearchGraphNode,
    ProfessionalServicesKnowledgeAgent,
    _runtime_config,
    declared_settings,
)
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST = yaml.safe_load((_ROOT / "config" / "agent.yaml").read_text(encoding="utf-8"))
_CONFIG = yaml.safe_load((_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


class TestManifestIdentity:
    def test_cfg_01_manifest_is_flat(self):
        # The registry reads root-level keys; a nested `agent:` block would leave
        # every one of them unread.
        assert "agent" not in _MANIFEST
        assert _MANIFEST["id"] == "SVC-C2-005"
        assert _MANIFEST["namespace"] == "svc"

    def test_cfg_02_declared_class_is_the_graph_class(self):
        assert _MANIFEST["class"] == ("src.graph.graph.ProfessionalServicesKnowledgeAgent")
        assert _MANIFEST["name"] == ProfessionalServicesKnowledgeAgent().name

    def test_cfg_03_category_and_industry(self):
        assert _MANIFEST["category"] == "Cat 2"
        assert _MANIFEST["industry"] == "SVC"
        assert _MANIFEST["base_type"] == "RAGAgent"

    def test_cfg_declares_no_secrets_or_extras(self):
        # Declaring a secret or extra that is never required makes the agent fail
        # at compile time; this template calls no secrets provider and constructs
        # no model client, so both lists stay empty.
        assert _MANIFEST["requires"]["secrets"] == []
        assert _MANIFEST["requires"]["extras"] == []
        assert _MANIFEST["generation_mode"] == "deterministic"


class TestManifestSecurity:
    def test_cfg_04_required_trust_level_matches_outer_gate_nodes(self):
        declared = TrustLevel(_MANIFEST["required_trust_level"])
        assert declared is TrustLevel.VERIFIED_EXTERNAL
        assert PreProcessNode.required_trust_level is declared
        assert PostProcessNode.required_trust_level is declared

    def test_cfg_05_max_retry_within_framework_ceiling(self):
        max_retry = _CONFIG["max_retry"]
        assert isinstance(max_retry, int)
        assert 0 <= max_retry < 10  # the framework's retry ceiling

    def test_hitl_is_not_enabled(self):
        # The interrupt-propagation waiver depends on this: no human-in-the-loop.
        assert (_CONFIG.get("hitl") or {}).get("enabled", False) is False


class TestRuntimeConfigIsLive:
    def test_cfg_06_runtime_config_reads_the_shipped_file(self):
        assert _runtime_config() == _CONFIG

    def test_cfg_07_declared_retrieval_settings_reach_the_inner_graph(self):
        # The declaration is the source of truth: what config/config.yaml says is
        # what the main slot forwards. A reader still pointing at the manifest
        # would return the module floor here instead.
        forwarded = KnowledgeSearchGraphNode(settings=declared_settings(_CONFIG))._parent_config()["configurable"][
            "retrieval"
        ]
        assert forwarded["top_k"] == _CONFIG["retrieval"]["top_k"]
        assert forwarded["score_threshold"] == _CONFIG["retrieval"]["score_threshold"]
        assert forwarded["kb_path"] == _CONFIG["retrieval"]["kb_path"]

    def test_cfg_08_declared_value_wins_over_the_module_floor(self):
        # Declare something different from the shipped file and prove it travels.
        forwarded = KnowledgeSearchGraphNode(
            settings=declared_settings({"retrieval": {"top_k": 9, "score_threshold": 0.9}})
        )._parent_config()["configurable"]["retrieval"]
        assert forwarded["top_k"] == 9
        assert forwarded["score_threshold"] == 0.9

    def test_out_of_contract_declaration_falls_back_to_the_floor(self):
        # A non-finite or out-of-range declaration is not forwarded: NaN compares
        # False against every bound, so forwarding it would disable the filter it
        # configures rather than fail.
        for bad in (float("nan"), float("inf"), -1, 999, "4", True, None):
            forwarded = KnowledgeSearchGraphNode(
                settings=declared_settings({"retrieval": {"top_k": bad}})
            )._parent_config()["configurable"]["retrieval"]
            assert forwarded["top_k"] == 4, f"unexpected forward for {bad!r}"


class TestSeededKnowledgeBase:
    def _entries(self):
        return json.loads((_ROOT / _CONFIG["retrieval"]["kb_path"]).read_text(encoding="utf-8"))

    def test_kb_path_declaration_resolves(self):
        assert (_ROOT / _CONFIG["retrieval"]["kb_path"]).is_file()

    def test_kb_is_a_well_formed_entry_list(self):
        entries = self._entries()
        assert isinstance(entries, list)
        assert len(entries) >= 5, "seeded knowledge base must carry a usable corpus"
        for entry in entries:
            assert set(entry.keys()) == {"id", "title", "category", "source", "tags", "content"}
            assert entry["id"] and entry["title"] and entry["content"]

    def test_kb_ids_are_unique(self):
        ids = [e["id"] for e in self._entries()]
        assert len(ids) == len(set(ids))
