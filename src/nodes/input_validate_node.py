"""AgentCore Platform v1.0"""

# SVC-C2-005 — InputValidateNode
# Domain node 1: normalise the incoming search request for the pipeline.
#
# Two things arrive from the outer graph, both already validated by PreProcessNode:
#
#   user_input / validated_input   the identifier-stripped query text
#   input_context["caller_request"]  the validated request record — category,
#                                    top_k, min_score, focus_labels
#
# The record crosses the outer -> inner boundary on the caller-data channel
# (src/graph/context_bridge.py), not inside the query string: the framework's input
# gate masks any two consecutive capitalised words on the query field, which is the
# shape of every service line and practice area a focus label names.
#
# This node re-checks the record's SHAPE before use — a transport is not a contract,
# and a field that does not arrive in the shape PreProcessNode published is dropped
# rather than trusted. It never widens a bound; the bounds live in PreProcessNode.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).
# Node contract: execute(self, state) -> dict — no extra parameter.

import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Hard cap on the normalised query length (defence in depth on input size).
_MAX_QUERY_CHARS = 2000

# Bounds the transported record is re-checked against. These MIRROR the caller
# contract in PreProcessNode; they never widen it.
_TOP_K_MIN, _TOP_K_MAX = 1, 20
_MIN_SCORE_MIN, _MIN_SCORE_MAX = 0.0, 1.0
_MAX_FOCUS_LABELS = 50

_WHITESPACE_RE = re.compile(r"\s+")
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_LABEL_RE = re.compile(r"^[\w &.,()/+-]{1,60}$")


def _transported_request(state: AgentState) -> Dict[str, Any]:
    """Read the validated caller request off the caller-data channel.

    Returns only fields that arrive in the shape PreProcessNode published. A field
    of any other shape is dropped, so a transport fault degrades to the declared
    defaults instead of feeding an unchecked value into retrieval.
    """
    channel = state.get("input_context") or {}
    if not isinstance(channel, dict):
        return {}
    record = from_json(channel.get("caller_request"), {}) or {}
    if not isinstance(record, dict):
        return {}

    clean: Dict[str, Any] = {}

    category = record.get("category")
    if isinstance(category, str) and _SLUG_RE.fullmatch(category):
        clean["category"] = category

    top_k = record.get("top_k")
    if isinstance(top_k, int) and not isinstance(top_k, bool) and _TOP_K_MIN <= top_k <= _TOP_K_MAX:
        clean["top_k"] = top_k

    min_score = record.get("min_score")
    if (
        isinstance(min_score, (int, float))
        and not isinstance(min_score, bool)
        and _MIN_SCORE_MIN <= float(min_score) <= _MIN_SCORE_MAX
    ):
        clean["min_score"] = float(min_score)

    labels = record.get("focus_labels")
    if isinstance(labels, list):
        kept = [label for label in labels[:_MAX_FOCUS_LABELS] if isinstance(label, str) and _LABEL_RE.fullmatch(label)]
        if kept:
            clean["focus_labels"] = kept

    return clean


class InputValidateNode(FunctionNode):
    """Normalise the query and publish the search filters for the pipeline.

    Input state keys:
        validated_input | user_input: identifier-stripped query text
        input_context:                validated caller request (bridged)

    Output state keys (partial dict):
        search_query:  normalised free-text search query
        query_filters: JSON dict {"category", "top_k", "min_score", "focus_labels"}
        intake_notes:  (when anomalies were seen) JSON list[str]
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")
        notes: List[str] = []

        query = raw if isinstance(raw, str) else ""
        query = _WHITESPACE_RE.sub(" ", query).strip()
        if not query:
            notes.append("InputValidateNode: empty request - no query to search.")
        if len(query) > _MAX_QUERY_CHARS:
            query = query[:_MAX_QUERY_CHARS]
            notes.append(f"InputValidateNode: query truncated to {_MAX_QUERY_CHARS} chars.")

        request = _transported_request(state)
        filters: Dict[str, Any] = {
            "category": request.get("category"),
            "top_k": request.get("top_k"),
            "min_score": request.get("min_score"),
            "focus_labels": request.get("focus_labels") or [],
        }

        # Domain audit: request normalised and filters published.
        emit_trace_event(
            "input_validate_complete",
            {
                "query_chars": len(query),
                "has_category_filter": filters["category"] is not None,
                "has_top_k_override": filters["top_k"] is not None,
                "focus_labels": len(filters["focus_labels"]),
            },
            state,
        )

        out: Dict[str, Any] = {
            "search_query": query,
            "query_filters": to_json(filters),
        }
        if notes:
            out["intake_notes"] = to_json(notes)
        return out
