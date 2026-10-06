"""AgentCore Platform v1.0"""

# State must be a flat TypedDict - never a Pydantic BaseModel. Checkpoints use
# msgpack serialization and Pydantic objects corrupt silently there. Extend
# AgentState with agent-specific fields only. Do NOT add credentials, secrets, or
# Pydantic models.
#
# Msgpack safety: structured fields (dict / list[dict]) are stored as JSON STRINGS,
# not bare Python containers - a bare dict/list in a checkpointed State field is a
# state-safety violation. Producers serialize with to_json() on write; consumers
# deserialize with from_json() on read.
#
# SVC-C2-005 - Professional Services Knowledge Base Search Agent (Cat 2 RAG).
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). Fields below cover both layers.
#
# Confidentiality note: direct client/engagement identifiers (engagement reference
# codes, long client reference numbers, e-mail) in the query text are
# surface-stripped by PreProcessNode before any field is written to State, and are
# refused outright on the caller-data channel. Only the normalised search query,
# knowledge-base passage summaries, validated labels, and the final grounded answer
# are persisted - never a raw client or engagement identifier.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input -> the supplied ``default`` so a missing or
    corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for SVC-C2-005.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    Domain fields are NotRequired so the TypedDict is valid at graph
    initialisation, before any node has written a value.
    """

    # ------------------------------------------------------------------
    # Outer layer - set by PreProcessNode / KnowledgeSearchGraphNode.merge_output
    # ------------------------------------------------------------------

    # Identifier-stripped, validated query text produced by PreProcessNode. Raw
    # input is NOT persisted beyond PreProcessNode.
    validated_input: NotRequired[str]

    # JSON STRING (to_json) of the validated caller request published by
    # PreProcessNode. Deserialised shape: {"channel": str, "category": str,
    # "top_k": int, "min_score": float, "focus_labels": list[str]} - every key
    # optional, every present key already within its contract bound. Crosses into
    # the inner graph on the caller-data channel (src/graph/context_bridge.py).
    caller_request: NotRequired[Optional[str]]

    # JSON STRING (to_json) of the runtime settings the outer graph validated out
    # of config/config.yaml, seeded so PreProcessNode reads its focus-label entry
    # cap from the declaration rather than a second copy of it.
    runtime_settings: NotRequired[Optional[str]]

    # Final knowledge-base search answer, mapped from the inner graph's
    # formatted_answer output via merge_output.
    knowledge_base_answer: NotRequired[str]

    # ------------------------------------------------------------------
    # Inner layer - domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # InputValidateNode outputs
    # Normalised free-text search query (whitespace-collapsed, length-capped).
    search_query: NotRequired[str]

    # JSON STRING (to_json) of the search filters published for the pipeline.
    # Deserialised dict shape: {"category": str | None, "top_k": int | None,
    # "min_score": float | None, "focus_labels": list[str]}.
    # Consumers (RetrieveNode, RerankFilterNode, OutputFormatNode) read it back
    # via from_json().
    query_filters: NotRequired[Optional[str]]

    # Declared `retrieval` settings forwarded by KnowledgeSearchGraphNode.
    # _parent_config() -> DomainWorkflowGraph._extra_initial_state(). This is the
    # ONLY config-plumbing route into a domain node (execute() takes no config
    # parameter).
    # JSON STRING (to_json) of {"top_k": int, "score_threshold": float,
    # "kb_path": str}. Consumers (RetrieveNode, RerankFilterNode) read it
    # back via from_json().
    retrieval_config: NotRequired[Optional[str]]

    # RetrieveNode output
    # JSON STRING (to_json) of scored KB candidates. Deserialised shape:
    # list[dict], each entry {"id": str, "title": str, "category": str,
    # "source": str, "score": float, "excerpt": str}.
    # Consumers (RerankFilterNode) read it back via from_json().
    retrieved_documents: NotRequired[Optional[str]]

    # RerankFilterNode output
    # JSON STRING (to_json) of reranked + threshold-filtered passages, capped
    # at top_k. Same entry shape as retrieved_documents.
    # Consumers (GenerateAnswerNode) read it back via from_json().
    ranked_documents: NotRequired[Optional[str]]

    # GenerateAnswerNode outputs
    # Rule-assembled grounded answer body with numbered citation markers.
    grounded_answer: NotRequired[str]

    # JSON STRING (to_json) of citations. Deserialised shape: list[dict],
    # each entry {"ref": int, "id": str, "title": str, "source": str}.
    # Consumers (OutputFormatNode) read it back via from_json().
    citations: NotRequired[Optional[str]]

    # OutputFormatNode output
    # Final formatted answer (body + sources + advisory disclaimer). Written by
    # OutputFormatNode; surfaced to the outer graph via get_output() ->
    # merge_output().
    formatted_answer: NotRequired[str]

    # Validation / parse notes accumulated during intake (no PII).
    # JSON STRING (to_json) of list[str].
    intake_notes: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit - framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
