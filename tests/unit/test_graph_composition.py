# SVC-C2-005 — Unit Tests: nested Cat-2 graph composition (outer + end-to-end)
#
# Drives the REAL outer agent (ProfessionalServicesKnowledgeAgent / Graph)
# end-to-end via AgentBaseGraph.invoke(). The e2e context is
# InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) — the
# manifest's declared caller level; for_internal() is NEVER used (it would
# over-privilege the run and hide trust-gate regressions).
#
# Mirrors docs/03_test_spec.md §3 (INT-05..INT-12).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

import pathlib

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

import src.graph.graph
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import (
    ProfessionalServicesKnowledgeAgent,
    Graph,
    KnowledgeSearchGraphNode,
)
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json, to_json

_DISCOVERY_QUERY = "What is our standard discovery-phase methodology for an SAP migration?"


def _run(user_input: str, trust: TrustLevel = TrustLevel.VERIFIED_EXTERNAL) -> dict:
    ctx = InvocationContext(caller_trust_level=trust, caller_id="unit-suite")
    return Graph().invoke(user_input, ctx=ctx)


class TestOuterGraphConstruction:
    def test_int_05_inherits_agent_base_graph_directly(self):
        assert issubclass(ProfessionalServicesKnowledgeAgent, AgentBaseGraph)

    def test_int_05_graph_alias(self):
        assert Graph is ProfessionalServicesKnowledgeAgent

    def test_state_schema_is_state(self):
        assert ProfessionalServicesKnowledgeAgent().state_schema is State

    def test_int_06_compile_fills_all_backbone_slots(self):
        agent = ProfessionalServicesKnowledgeAgent()
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"
        assert isinstance(agent._nodes["pre_process"], PreProcessNode)
        assert isinstance(agent._nodes["main"], KnowledgeSearchGraphNode)
        assert isinstance(agent._nodes["post_process"], PostProcessNode)

    def test_add_edges_is_not_overridden(self):
        # Backbone wiring belongs to the framework — the template must not
        # redefine it.
        assert "add_edges" not in ProfessionalServicesKnowledgeAgent.__dict__


class TestMainSlotGraphNode:
    def test_int_07_get_subgraph_returns_the_inner_graph(self):
        subgraph = KnowledgeSearchGraphNode().get_subgraph()
        assert isinstance(subgraph, DomainWorkflowGraph)
        assert subgraph.config["configurable"]["retrieval"], "inner config must carry the retrieval block"

    def test_int_08_extract_input_prefers_validated_input(self):
        node = KnowledgeSearchGraphNode()
        assert node.extract_input({"validated_input": "VI", "user_input": "UI"}) == "VI"
        assert node.extract_input({"user_input": "UI"}) == "UI"

    def test_int_09_merge_output_maps_the_inner_contract(self):
        node = KnowledgeSearchGraphNode()
        citations = to_json([{"ref": 1, "id": "kb-001", "title": "t", "source": "s"}])
        delta = node.merge_output(
            {},
            {"formatted_answer": "ANSWER", "citations": citations, "status": AgentStatus.SUCCESS.value},
        )
        # The inner formatted_answer surfaces as BOTH knowledge_base_answer and
        # result (the output boundary reads state["result"]).
        assert delta == {
            # Carried across the boundary so the outer graph can report a reason
            # settled inside the inner run. Empty here: this run was not declined.
            "error_code": "",
            "knowledge_base_answer": "ANSWER",
            "result": "ANSWER",
            "citations": citations,
            "status": AgentStatus.SUCCESS.value,
        }

    def test_error_strategy_is_propagate_and_hitl_is_contained(self):
        assert KnowledgeSearchGraphNode.error_strategy == "propagate"
        assert KnowledgeSearchGraphNode.propagate_hitl is False

    def test_int_10_parent_config_forwards_a_usable_block_without_a_config_file(self, monkeypatch):
        # With no runtime config on disk the graph runs on its built-in floor
        # rather than forwarding {} — the pipeline stays usable, and the values it
        # falls back to are the module's own, not a second copy of the file.
        monkeypatch.setattr(src.graph.graph, "_RUNTIME_CONFIG_PATH", pathlib.Path("/nonexistent/config.yaml"))
        assert src.graph.graph._runtime_config() == {}
        cfg = KnowledgeSearchGraphNode(
            settings=src.graph.graph.declared_settings(src.graph.graph._runtime_config())
        )._parent_config()
        retrieval = cfg["configurable"]["retrieval"]
        assert retrieval["kb_path"] == "config/kb/professional_services_kb.json"
        assert retrieval["top_k"] == 4

    def test_extract_input_stashes_the_validated_request_for_the_inner_graph(self):
        from src.graph.context_bridge import get_caller_input_context, set_caller_input_context

        set_caller_input_context(None)
        try:
            handed = KnowledgeSearchGraphNode().extract_input(
                {"validated_input": "methodology guidance", "caller_request": '{"top_k": 2}'}
            )
            # Only the query travels on the string channel the framework masks.
            assert handed == "methodology guidance"
            assert get_caller_input_context() == {"caller_request": '{"top_k": 2}'}
        finally:
            set_caller_input_context(None)


class TestGetOutputCitationsSecurityGate:
    """get_output() re-scans `citations` through the credential gate as a
    SECOND, independent pass on top of PostProcessNode's own `result` scan.
    PostProcessNode gates `result`; this pass guards the field get_output()
    adds on top of it, which nothing else inspects. A credential-shaped value
    nested inside a single citation's `title`/`source` field must flip status
    to ERROR and withhold `citations` entirely, even when the top-level
    answer (`formatted_output` / `result`, surfaced as `output` on the base
    envelope) is itself completely clean — proving the second pass is not a
    no-op that just re-checks what PostProcessNode already gated."""

    _CLEAN_ANSWER = (
        "# Professional Services Knowledge Base Search Result\n\n"
        "[1] discovery-phase methodology confirmed for the SAP migration engagement.\n"
    )

    def _make_state(self, citations_json: str) -> dict:
        return {
            "status": AgentStatus.SUCCESS.value,
            "formatted_output": self._CLEAN_ANSWER,
            "result": self._CLEAN_ANSWER,
            "citations": citations_json,
            "trace_id": "trace-1",
            "correlation_id": "corr-1",
            "node_history": [
                "InitializeNode",
                "PreProcessNode",
                "KnowledgeSearchGraphNode",
                "PostProcessNode",
                "FinalizeNode",
            ],
        }

    def test_clean_citations_are_surfaced_on_success(self):
        clean = to_json(
            [
                {
                    "ref": 1,
                    "id": "kb-001",
                    "title": "Discovery-phase methodology for technology transformation engagements",
                    "source": "Delivery Methodology Handbook, discovery phase",
                }
            ]
        )
        state = self._make_state(clean)
        result = ProfessionalServicesKnowledgeAgent().get_output(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["citations"] == from_json(clean)
        assert result["output"] == self._CLEAN_ANSWER

    def test_credential_nested_in_citation_blocks_despite_clean_top_level_answer(self):
        """The top-level answer/result carries no credential — the violation
        is nested two containers down, inside citations[0]['source']."""
        secret = "sk-ABCDEF0123456789abcdef"
        dirty = to_json(
            [
                {
                    "ref": 1,
                    "id": "kb-001",
                    "title": "Discovery-phase methodology for technology transformation engagements",
                    "source": f"internal note api_key={secret}",
                },
                {
                    "ref": 2,
                    "id": "kb-002",
                    "title": "Requirements-elicitation workshop format for engagement kickoff",
                    "source": "Delivery Methodology Handbook, requirements phase",
                },
            ]
        )
        state = self._make_state(dirty)
        # Sanity: the top-level answer itself is clean — only the nested
        # citation field carries the credential-shaped string.
        assert secret not in state["formatted_output"]
        assert secret not in state["result"]

        result = ProfessionalServicesKnowledgeAgent().get_output(state)

        assert result["status"] == AgentStatus.ERROR.value, (
            "a credential nested inside citations[0]['source'] must flip status "
            "to ERROR even though the top-level answer is clean"
        )
        assert "citations" not in result, (
            "citations must be withheld ENTIRELY on any violation — never a "
            "partial/best-effort list with only the bad entry dropped"
        )
        # The base envelope's own output field is untouched — proves the
        # violation was detected in the SECOND (citations) pass, not the
        # first (result) pass PostProcessNode already ran.
        assert result["output"] == self._CLEAN_ANSWER
        assert secret not in str(result)

    def test_non_success_state_never_attaches_citations(self):
        state = self._make_state(to_json([{"ref": 1, "id": "kb-001", "title": "t", "source": "s"}]))
        state["status"] = AgentStatus.ERROR.value
        result = ProfessionalServicesKnowledgeAgent().get_output(state)
        assert "citations" not in result
        assert result["status"] == AgentStatus.ERROR.value


class TestEndToEndInvoke:
    """Full agent run: outer backbone + inner domain workflow, no LLM."""

    def test_int_11_invoke_returns_success(self):
        result = _run(_DISCOVERY_QUERY)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_int_11_output_is_the_gated_formatted_answer(self):
        output = _run(_DISCOVERY_QUERY).get("output")
        assert isinstance(output, str) and output.strip()
        assert output.startswith("# Professional Services Knowledge Base Search Result")
        assert "[1]" in output
        assert "does not replace engagement-specific analysis" in output

    def test_int_11_e2e_traverses_the_post_process_gate(self):
        history = _run(_DISCOVERY_QUERY).get("node_history", [])
        for cls_name in ("PreProcessNode", "KnowledgeSearchGraphNode", "PostProcessNode"):
            assert cls_name in history, f"node_history missing {cls_name}: {history}"

    def test_no_coverage_query_still_terminates_success(self):
        result = _run("quantum telepathy sandwich recipes")
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "does not contain sufficient coverage" in result.get("output", "")

    def test_int_12_anonymous_caller_is_denied_at_the_outer_boundary(self):
        """Trust gate at graph level: an ANONYMOUS invoke is refused by the
        VERIFIED_EXTERNAL pre_process slot. The error state short-circuits the
        main slot (its input gate sees status=error and skips the inner graph)
        and routes past post_process to finalize — no domain answer is ever
        produced."""
        result = _run(_DISCOVERY_QUERY, trust=TrustLevel.ANONYMOUS)
        assert result.get("status") == AgentStatus.ERROR.value
        assert not result.get("output")
        history = result.get("node_history", [])
        assert "PostProcessNode" not in history
        assert history[:2] == ["InitializeNode", "PreProcessNode"]


class TestStateRoundTrip:
    """State helpers: producers to_json() on write, consumers from_json()."""

    def test_to_from_json_list_round_trip(self):
        original = [{"id": "kb-001", "score": 0.69, "title": "discovery-phase methodology"}]
        assert from_json(to_json(original)) == original

    def test_to_from_json_dict_round_trip(self):
        original = {"category": "methodology", "top_k": 3}
        assert from_json(to_json(original)) == original

    def test_to_json_none_passes_through(self):
        assert to_json(None) is None

    def test_from_json_malformed_returns_default(self):
        assert from_json("{not valid json", default=[]) == []
        assert from_json(None, default={}) == {}
        assert from_json("", default=[]) == []
