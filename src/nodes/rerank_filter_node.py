"""AgentCore Platform v1.0"""

# SVC-C2-005 - RerankFilterNode
# Domain node 3: rerank the retrieval candidates and enforce the relevance
# floor. Deterministic: a small category-match boost on top of the retrieval
# score, drop everything below `score_threshold`, cap the survivors at
# `top_k`.
#
# Config: reads `top_k` / `score_threshold` exclusively from the State field
# `retrieval_config` (see retrieve_node.py for the plumbing note), falling back to
# a module floor for a key the declaration omits. A caller-supplied top_k or
# min_score override (query_filters, already validated at the boundary) wins when
# it is STRICTER than the declared value — a caller may narrow the result set,
# never widen it. There is no `config` parameter on execute().
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import re
from typing import Any, ClassVar, Dict, List, Set

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

# Built-in floor, used only for a key config/config.yaml does not declare.
_DEFAULT_RETRIEVAL: Dict[str, Any] = {
    "top_k": 4,
    "score_threshold": 0.25,
}

# Boost applied when a candidate's category matches the caller's filter.
_CATEGORY_BOOST = 0.1

# Boost applied when a candidate's title or tags carry a caller focus label's terms.
# Kept below the category boost so an explicit category filter still dominates.
_FOCUS_BOOST = 0.05

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _label_tokens(labels: List[str]) -> Set[str]:
    """Lowercase alphanumeric tokens of the caller's focus labels, noise removed."""
    tokens: Set[str] = set()
    for label in labels:
        if not isinstance(label, str):
            continue
        tokens.update(t for t in _TOKEN_RE.findall(label.lower()) if len(t) > 2)
    return tokens


def _resolve_retrieval_config(state: AgentState) -> Dict[str, Any]:
    """Effective retrieval config: State `retrieval_config` (if set) over defaults."""
    effective = dict(_DEFAULT_RETRIEVAL)  # local copy - never mutate the module default
    from_state = from_json(state.get("retrieval_config"), None)
    if isinstance(from_state, dict):
        effective.update(from_state)
    return effective


class RerankFilterNode(FunctionNode):
    """Rerank candidates, apply the score threshold, cap at top_k.

    Input state keys:
        retrieved_documents: JSON list of scored candidates (from RetrieveNode)
        query_filters:       JSON dict with the category filter, the top_k /
                             min_score overrides and the focus labels
        retrieval_config:    declared retrieval settings (JSON)

    Output state keys (partial dict):
        ranked_documents: JSON list of surviving passages (score desc, <= top_k)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        candidates: List[Dict[str, Any]] = from_json(state.get("retrieved_documents"), []) or []
        filters = from_json(state.get("query_filters"), {}) or {}
        retrieval_cfg = _resolve_retrieval_config(state)

        try:
            top_k = int(retrieval_cfg.get("top_k", _DEFAULT_RETRIEVAL["top_k"]))
        except (TypeError, ValueError):
            top_k = int(_DEFAULT_RETRIEVAL["top_k"])
        top_k = max(1, min(20, top_k))
        # A stricter caller override (validated by InputValidateNode) wins.
        caller_top_k = filters.get("top_k")
        if isinstance(caller_top_k, int) and not isinstance(caller_top_k, bool) and 1 <= caller_top_k < top_k:
            top_k = caller_top_k

        try:
            score_threshold = float(retrieval_cfg.get("score_threshold", _DEFAULT_RETRIEVAL["score_threshold"]))
        except (TypeError, ValueError):
            score_threshold = float(_DEFAULT_RETRIEVAL["score_threshold"])
        score_threshold = max(0.0, min(1.0, score_threshold))
        # A caller may only tighten the relevance floor, never lower it.
        caller_min_score = filters.get("min_score")
        if (
            isinstance(caller_min_score, (int, float))
            and not isinstance(caller_min_score, bool)
            and score_threshold < float(caller_min_score) <= 1.0
        ):
            score_threshold = float(caller_min_score)

        category = filters.get("category")
        focus_tokens = _label_tokens(filters.get("focus_labels") or [])

        reranked: List[Dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            entry = dict(candidate)  # local copy - inputs stay immutable
            try:
                score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if category and str(entry.get("category", "")).lower() == str(category).lower():
                score = min(1.0, score + _CATEGORY_BOOST)
            if focus_tokens:
                haystack = f"{entry.get('title', '')} {entry.get('category', '')}".lower()
                if focus_tokens & set(_TOKEN_RE.findall(haystack)):
                    score = min(1.0, score + _FOCUS_BOOST)
            entry["score"] = round(score, 4)
            reranked.append(entry)

        # Deterministic ordering: score desc, then id asc for stable ties.
        reranked.sort(key=lambda c: (-c.get("score", 0.0), str(c.get("id", ""))))

        kept = [c for c in reranked if c.get("score", 0.0) >= score_threshold][:top_k]
        dropped = len(reranked) - len(kept)

        # Domain audit: rerank + relevance floor applied.
        emit_trace_event(
            "rerank_filter_complete",
            {
                "kept": len(kept),
                "dropped": dropped,
                "score_threshold": score_threshold,
                "top_k": top_k,
                "focus_labels": len(filters.get("focus_labels") or []),
            },
            state,
        )

        return {"ranked_documents": to_json(kept)}
