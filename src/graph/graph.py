"""AgentCore Platform v1.0"""

# SVC-C2-005 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Professional Services Knowledge Base Search Agent (Cat 2 RAG domain workflow).
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to a single-slot agent; do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (retry)
#                                             -> pre_process
#
#   The `main` slot is a GraphNode subclass (KnowledgeSearchGraphNode) that delegates
#   the full knowledge-base search workflow to DomainWorkflowGraph (inner BaseGraph:
#   input_validate -> retrieve -> rerank_filter -> generate_answer -> output_format).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer backbone
#   is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- caller-request hand-off (outer -> inner)
#
# Class-name contract:
#   graph.py class:           ProfessionalServicesKnowledgeAgent (this file)
#   config/agent.yaml class:  "src.graph.graph.ProfessionalServicesKnowledgeAgent"
#   src/api/server.py import: from src.graph.graph import ProfessionalServicesKnowledgeAgent
#
# Runtime configuration:
#   config/agent.yaml is the static registration manifest — identity only, no tuning.
#   config/config.yaml holds every runtime parameter. The registry loads that file and
#   passes it as Graph(config=...); the standalone server does the same through
#   _runtime_config(), so a registry-loaded agent and a deployed one see identical
#   settings. Every declared value is validated once in declared_settings() and then
#   travels to its consumer — there is no second copy of the defaults on disk.

import math
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json, to_json

# Runtime parameters: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Built-in retrieval tuning, used only for keys config/config.yaml does not declare
# or declares out of contract. These are the pipeline's floor, not a mirror of the
# file: a value present in the file always wins, which is what makes the declaration
# observable end to end.
_BUILTIN_RETRIEVAL: Dict[str, Any] = {
    "top_k": 4,
    "score_threshold": 0.25,
    "kb_path": "config/kb/professional_services_kb.json",
}

# Bounds every declared retrieval setting is checked against.
_TOP_K_MIN, _TOP_K_MAX = 1, 20
_SCORE_MIN, _SCORE_MAX = 0.0, 1.0

# Entry cap on the caller's focus labels when config/config.yaml declares none.
_BUILTIN_MAX_FOCUS_LABELS = 10
_FOCUS_LABEL_CAP_MIN, _FOCUS_LABEL_CAP_MAX = 0, 50


def _runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the file the registry loads and passes as Graph(config=...); the
    standalone server (src/api/server.py) reads it here so both deployments run on
    the same declaration. Returns an empty dict — never raises — when the file is
    absent, unreadable, not valid YAML, or not a mapping; the graph then runs on its
    built-in floor. PyYAML is imported lazily because it is a framework runtime
    dependency rather than a module-load coupling of this template.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(Dict[str, Any], loaded)


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate one declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, other non-numerics, NaN/Infinity and out-of-range values all
    return None, and the consumer keeps its built-in floor. Rejecting non-finite
    values matters even for a declared setting: NaN compares False against every
    bound, so a NaN relevance threshold would silently disable the filter it
    configures rather than fail.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def declared_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and flatten the declared runtime settings for the domain nodes.

    `config` is what the graph was constructed with — the contents of
    config/config.yaml. Each value is checked for type, finiteness and range; an
    absent or out-of-contract key falls back to the built-in floor rather than
    propagating a value no consumer could use.
    """
    declared = config.get("retrieval")
    declared = declared if isinstance(declared, dict) else {}

    settings: Dict[str, Any] = dict(_BUILTIN_RETRIEVAL)

    top_k = _config_number(declared.get("top_k"), _TOP_K_MIN, _TOP_K_MAX)
    if top_k is not None:
        settings["top_k"] = int(top_k)

    threshold = _config_number(declared.get("score_threshold"), _SCORE_MIN, _SCORE_MAX)
    if threshold is not None:
        settings["score_threshold"] = threshold

    kb_path = declared.get("kb_path")
    if isinstance(kb_path, str) and kb_path.strip():
        settings["kb_path"] = kb_path.strip()

    cap = _config_number(config.get("max_focus_labels"), _FOCUS_LABEL_CAP_MIN, _FOCUS_LABEL_CAP_MAX)
    settings["max_focus_labels"] = int(cap) if cap is not None else _BUILTIN_MAX_FOCUS_LABELS

    return settings


class KnowledgeSearchGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the outer agent.

    Wraps DomainWorkflowGraph (the inner knowledge-base search pipeline). Called by
    the backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — build the inner graph carrying the declared settings
      extract_input() — hand the inner graph the validated query, and bridge the
                        validated caller request across the boundary
      merge_output()  — map sub_result fields into the outer state delta (changed
                        keys only)
      error_strategy  — "propagate": re-raise inner errors as SubgraphError
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail fast).
    # "handle": call on_subgraph_error() instead — for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: human-in-the-loop interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        """Bind the validated runtime settings forwarded by the outer graph."""
        super().__init__()
        self._settings: Dict[str, Any] = dict(settings or _BUILTIN_RETRIEVAL)

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the validated retrieval settings to the inner graph.

        The values come from the outer graph's own config — config/config.yaml as
        loaded by the registry — validated once in declared_settings(). The inner
        graph republishes them into inner state (DomainWorkflowGraph.
        _extra_initial_state()) so RetrieveNode and RerankFilterNode read live
        top_k / score_threshold values rather than a dead declaration.
        """
        return {"configurable": {"retrieval": dict(self._settings)}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported inside the method to avoid a circular import
        at module load time and to match the nested composition pattern.

        The inner graph receives the validated settings through its BaseGraph ctor;
        its domain NODES still take no constructor arguments. Config reaches them
        exclusively through State — `execute(self, state) -> dict` carries no extra
        parameter.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke(), and bridge the
        caller request.

        GraphNode.extract_input() returns a STRING the framework writes into the
        inner state under `user_input`. That field is one the framework's input gate
        rewrites, and its personal-name heuristic matches any two consecutive
        capitalised words — precisely the shape of a service line or practice area
        ("Change Management", "Cloud Assessment"). Carrying the caller's focus
        labels there would replace them with a mask token and the ranking would key
        off the mask instead of the label. Only the query travels on `user_input`;
        the structured request crosses on the caller-data channel, which carries
        values through untouched.

        This is also the last hook that sees the outer state before the inner invoke,
        and the framework does not forward the caller-data channel into a subgraph
        (see src/graph/context_bridge.py), so the validated request is stashed here
        for the inner side to pick up.
        """
        set_caller_input_context({"caller_request": state.get("caller_request") or "{}"})
        return cast(str, state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output(). Returns
        ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "formatted_answer", "citations", "status", ...
          This merge_output() reads -> sub_result.get("formatted_answer"),
                                       sub_result.get("citations"),
                                       sub_result.get("status")

        `result` is the field PostProcessNode (the output boundary) reads, so the
        rendered answer is mapped there as well as to knowledge_base_answer;
        otherwise the finalized output would always be empty.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "knowledge_base_answer": sub_result.get("formatted_answer"),
            "result": sub_result.get("formatted_answer"),
            "citations": sub_result.get("citations"),
            "status": sub_result.get("status"),
        }


class ProfessionalServicesKnowledgeAgent(AgentBaseGraph):
    """Outer graph for SVC-C2-005 (Cat 2 knowledge-base search).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    KnowledgeSearchGraphNode (main slot), which delegates to DomainWorkflowGraph.

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() and get_output() are the ONLY overrides:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (caller contract + confidentiality screen)
      - main:         KnowledgeSearchGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output boundary)
      - get_output(): extends the base envelope with the structured citations field,
        on SUCCESS only, fail-closed

    Runtime configuration: the registry loads config/config.yaml and passes it as
    Graph(config=...); the standalone server does the same via _runtime_config().
    AgentBaseGraph consumes max_retry from that config for retry routing, the
    validated retrieval settings reach the inner graph through
    KnowledgeSearchGraphNode, and the caller-contract bounds reach the pre_process
    node through the initial state — so every declared value is live in both
    deployments.

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "ProfessionalServicesKnowledgeAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the framework's
        default initialize node (schema_version, session_id, trust_level) and
        finalize node (response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        # The declared settings are validated once here and handed to the main slot,
        # so config/config.yaml is the single source for both the caller-contract
        # bounds and the inner pipeline.
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = KnowledgeSearchGraphNode(settings=declared_settings(self.config))
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated runtime settings into the outer initial state.

        The node contract takes no config argument and every node is constructed
        without one, so the bound PreProcessNode needs — the focus-label entry cap —
        travel through State.
        """
        return {"runtime_settings": to_json(declared_settings(self.config))}

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Extend the base envelope with the citations field.

        The grounded answer's citations back to the source knowledge-base entry
        (id / title / source) ARE the product, not a side note, so they are surfaced
        as a real (decoded) Python list on top of the base `output` / `status` /
        `trace_id` / `correlation_id` / `node_history` envelope from
        AgentBaseGraph.get_output().

        Fail-closed, three times over:
          1. Citations are attached only when the terminal state.status is SUCCESS —
             a non-SUCCESS run (trust denial, blocked output, subgraph error, ...)
             returns only the base envelope.
          1a. SUCCESS alone is not enough: a run that completed WITHOUT carrying
             out the request (a value the caller can correct, marked by
             `error_code`) is also a SUCCESS, and produced no citations to
             attach. It returns only the base envelope too.
          2. Even on an answered SUCCESS, citations are re-scanned through the same
             `_security_gate_output()` PostProcessNode applies. That node gates
             `result`; this second pass guards the field get_output() adds on top of
             it. A violation flips status to ERROR and withholds citations, never a
             partial payload.
        """
        base = cast(Dict[str, Any], super().get_output(state))
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a product: none of the
        # structured fields below were produced, so none is released.
        if state.get("error_code"):
            return base

        if state.get("status") != AgentStatus.SUCCESS.value:
            return base

        citations = cast(List[Any], from_json(state.get("citations"), []) or [])

        violation = _security_gate_output({"citations": citations})
        if violation:
            base["status"] = AgentStatus.ERROR.value
            return base

        base["citations"] = citations
        return base


# Back-compat alias — config/agent.yaml declares the dotted path to the class above,
# and src/api/server.py imports it directly. Keep both names pointing at the agent.
Graph = ProfessionalServicesKnowledgeAgent
