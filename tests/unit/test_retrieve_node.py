# SVC-C2-005 — Unit Tests: RetrieveNode (inner domain node 2)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller.
# RetrieveNode carries NO `config` parameter on execute() (the node
# contract) — every config-plumbing test below goes through node(state) and
# the State `retrieval_config` field (no direct execute(state, config=...)
# carve-out is needed for this template).
#
# Mirrors docs/03_test_spec.md §2.3 (RET-01..RET-08).
# Deterministic — keyword scoring over the seeded
# config/kb/professional_services_kb.json; no LLM, no network.
# framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import from_json, to_json

_DISCOVERY_QUERY = "What is our standard discovery-phase methodology for an SAP migration?"


def _make_state(query=_DISCOVERY_QUERY, **extra) -> dict:
    state = {
        "search_query": query,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestRetrieveHappyPath:
    def test_ret_01_top_hit_is_discovery_methodology_entry(self):
        result = RetrieveNode()(_make_state())
        docs = from_json(result["retrieved_documents"])
        assert docs, "expected candidates for the discovery-methodology query"
        assert docs[0]["id"] == "kb-001"

    def test_ret_02_scores_sorted_descending(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_documents"])
        scores = [d["score"] for d in docs]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0.0 for s in scores)

    def test_ret_03_entry_shape_and_excerpt_cap(self):
        docs = from_json(RetrieveNode()(_make_state())["retrieved_documents"])
        for doc in docs:
            assert set(doc.keys()) == {"id", "title", "category", "source", "score", "excerpt"}
            assert len(doc["excerpt"]) <= 400

    def test_retrieved_documents_is_json_string(self):
        # List-shaped State fields travel as JSON strings.
        result = RetrieveNode()(_make_state())
        assert isinstance(result["retrieved_documents"], str)


class TestRetrieveFilters:
    def test_ret_04_category_filter_restricts_pool(self):
        state = _make_state(
            query="benchmark utilization rate for cloud assessment engagements",
            query_filters=to_json({"category": "benchmark", "top_k": None}),
        )
        docs = from_json(RetrieveNode()(state)["retrieved_documents"])
        assert docs, "benchmark category has a seeded entry"
        assert {d["category"] for d in docs} == {"benchmark"}
        assert docs[0]["id"] == "kb-006"

    def test_ret_05_empty_query_yields_no_candidates(self):
        docs = from_json(RetrieveNode()(_make_state(query=""))["retrieved_documents"])
        assert docs == []


class TestRetrieveConfigFromState:
    """Config plumbing: State `retrieval_config` (republished by
    DomainWorkflowGraph._extra_initial_state()) over module defaults. No
    `config` execute() parameter exists — every call below goes through
    node(state)."""

    def test_ret_06_state_kb_path_override_missing_file(self):
        state = _make_state(retrieval_config=to_json({"kb_path": "config/kb/does_not_exist.json"}))
        result = RetrieveNode()(state)
        assert from_json(result["retrieved_documents"]) == []
        notes = from_json(result.get("intake_notes"), [])
        assert any("not readable" in n for n in notes)

    def test_ret_07_state_retrieval_config_kb_path_used_when_valid(self):
        state = _make_state(retrieval_config=to_json({"kb_path": "config/kb/professional_services_kb.json"}))
        result = RetrieveNode()(state)
        assert from_json(result["retrieved_documents"]), "the state-forwarded kb_path must be honoured"


class TestRetrieveNotesAccumulation:
    def test_ret_08_notes_append_never_clobber(self):
        state = _make_state(
            intake_notes=to_json(["earlier note from input validation"]),
            retrieval_config=to_json({"kb_path": "config/kb/bogus.json"}),
        )
        result = RetrieveNode()(state)
        notes = from_json(result["intake_notes"])
        assert notes[0] == "earlier note from input validation"
        assert len(notes) == 2
