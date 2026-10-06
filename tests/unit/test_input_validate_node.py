# SVC-C2-005 — Unit Tests: InputValidateNode (inner domain node 1)
#
# Invocation canon: node(state) via BaseNode.__call__ with an ANONYMOUS caller
# (inner domain node). Payloads are lowercase / identifier-free so the framework's
# input mask leaves them untouched.
#
# This node normalises the query and republishes the caller's request, which
# crosses the outer -> inner boundary on the caller-data channel. It re-checks the
# SHAPE of what arrives — a transport is not a contract — but never widens a bound.
#
# Mirrors docs/03_test_spec.md §2.2 (VAL-01..VAL-09).
# Deterministic — no network. framework.* / src.* imports only.

from framework.schemas.trust_level import TrustLevel

from src.nodes.input_validate_node import InputValidateNode
from src.schemas.state import from_json, to_json


def _make_state(payload, request=None, **extra) -> dict:
    state = {
        "validated_input": payload,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
        "input_context": {"caller_request": to_json(request or {})},
    }
    state.update(extra)
    return state


_EMPTY_FILTERS = {
    "category": None,
    "top_k": None,
    "min_score": None,
    "focus_labels": [],
}


class TestQueryNormalisation:
    def test_val_01_plain_text_becomes_query(self):
        result = InputValidateNode()(_make_state("engagement record retention duties"))
        assert result["search_query"] == "engagement record retention duties"
        assert from_json(result["query_filters"]) == _EMPTY_FILTERS

    def test_val_02_whitespace_is_collapsed(self):
        result = InputValidateNode()(_make_state("  engagement   record\n retention "))
        assert result["search_query"] == "engagement record retention"

    def test_query_filters_is_json_string(self):
        # Structured State fields travel as JSON strings, never bare dicts.
        result = InputValidateNode()(_make_state("engagement record retention"))
        assert isinstance(result["query_filters"], str)
        assert isinstance(from_json(result["query_filters"]), dict)

    def test_val_03_empty_query_is_noted_not_raised(self):
        result = InputValidateNode()(_make_state("   "))
        assert result["search_query"] == ""
        notes = from_json(result["intake_notes"], [])
        assert any("empty request" in note for note in notes)

    def test_val_04_over_long_query_is_truncated_with_a_note(self):
        result = InputValidateNode()(_make_state("methodology " * 400))
        assert len(result["search_query"]) == 2000
        notes = from_json(result["intake_notes"], [])
        assert any("truncated" in note for note in notes)


class TestTransportedRequest:
    def test_val_05_validated_request_reaches_the_filters(self):
        result = InputValidateNode()(
            _make_state(
                "benchmark utilization rate",
                {
                    "category": "benchmark",
                    "top_k": 2,
                    "min_score": 0.4,
                    "focus_labels": ["Cloud Assessment"],
                },
            )
        )
        assert from_json(result["query_filters"]) == {
            "category": "benchmark",
            "top_k": 2,
            "min_score": 0.4,
            "focus_labels": ["Cloud Assessment"],
        }

    def test_val_06_absent_channel_degrades_to_the_baseline(self):
        state = _make_state("engagement record retention")
        state.pop("input_context")
        result = InputValidateNode()(state)
        assert from_json(result["query_filters"]) == _EMPTY_FILTERS

    def test_val_07_a_field_of_the_wrong_shape_is_dropped_not_trusted(self):
        # These shapes cannot come from PreProcessNode; if one arrives anyway the
        # node degrades to the declared default rather than feed it to retrieval.
        for request in (
            {"top_k": 999},
            {"top_k": "4"},
            {"top_k": True},
            {"min_score": float("nan")},
            {"min_score": 5},
            {"category": "Benchmark Data"},
            {"category": "a" * 40},
            {"focus_labels": "Cloud Assessment"},
            {"focus_labels": ["carriage\nreturn"]},
        ):
            filters = from_json(InputValidateNode()(_make_state("methodology", request))["query_filters"])
            for key, value in _EMPTY_FILTERS.items():
                assert filters[key] == value, f"{request!r} leaked {key}={filters[key]!r}"

    def test_val_08_a_corrupt_channel_degrades_rather_than_raises(self):
        result = InputValidateNode()(_make_state("methodology", **{"input_context": {"caller_request": "not json"}}))
        assert from_json(result["query_filters"]) == _EMPTY_FILTERS

    def test_val_09_non_mapping_channel_degrades(self):
        result = InputValidateNode()(_make_state("methodology", **{"input_context": "not a mapping"}))
        assert from_json(result["query_filters"]) == _EMPTY_FILTERS
