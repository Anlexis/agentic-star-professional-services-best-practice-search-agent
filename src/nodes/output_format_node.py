"""AgentCore Platform v1.0"""

# SVC-C2-005 - OutputFormatNode
# Domain node 5 (terminal): compose the final formatted answer - the grounded
# answer body, the Sources list, and the standing advisory disclaimer.
# The disclaimer is part of THIS node's domain output contract, not of the
# outer post_process slot (post_process only gates, it does not compose).
#
# Document invariant (enforced independently at the output boundary by
# PostProcessNode): the rendered document's structure is this template's own. It
# carries exactly one title heading and one Sources heading, every source line
# corresponds to a citation the pipeline produced, and the advisory disclaimer is
# always present. Caller text appears only as single-line quoted fragments — the
# query is whitespace-collapsed upstream and focus labels are alphabet-locked at
# the caller boundary — so no caller value can open a section or forge a source.
#
# Wired by the inner graph (DomainWorkflowGraph). get_output() of the inner
# graph surfaces formatted_answer + status to the outer merge_output().
# Returns only changed state keys (partial dict).
# Node contract: execute(self, state) -> dict - no extra parameter.

from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

# Standing advisory line - appended to EVERY answer this template emits.
_ADVISORY_DISCLAIMER = (
    "This answer is generated from the seeded professional-services knowledge "
    "base for informational purposes only and summarizes internal methodology, "
    "guidance, and benchmark materials. It does not replace engagement-specific "
    "analysis or client-specific legal, tax, or regulatory advice. Verify "
    "against the current methodology repository and consult the engagement "
    "lead before applying it to client work."
)


class OutputFormatNode(FunctionNode):
    """Compose the final answer: body + sources + advisory disclaimer.

    Input state keys:
        grounded_answer: answer body with [n] citation markers
        citations:       JSON list [{ref, id, title, source}]
        query_filters:   JSON dict carrying the caller's focus labels

    Output state keys (partial dict):
        formatted_answer: final rendered answer string
        status:           AgentStatus.SUCCESS.value (a plain string — the bare
                          enum is never written to State)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        grounded_answer = state.get("grounded_answer") or ("No answer is available for this request.")
        citations: List[Dict[str, Any]] = from_json(state.get("citations"), []) or []
        filters: Dict[str, Any] = from_json(state.get("query_filters"), {}) or {}
        focus_labels = [label for label in (filters.get("focus_labels") or []) if isinstance(label, str)]

        lines: List[str] = []
        lines.append("# Professional Services Knowledge Base Search Result")
        lines.append("")
        if focus_labels:
            # Caller free text, locked to the label alphabet at the caller boundary
            # and rendered on one line so it cannot alter the document structure.
            lines.append(f"Engagement focus applied: {'; '.join(focus_labels)}")
            lines.append("")
        lines.append(grounded_answer)
        lines.append("")
        lines.append("## Sources")
        if citations:
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                ref = citation.get("ref", "?")
                title = str(citation.get("title", "")).strip()
                source = str(citation.get("source", "")).strip()
                suffix = f" ({source})" if source else ""
                lines.append(f"- [{ref}] {title}{suffix}")
        else:
            lines.append("- none (no knowledge-base passage cleared the relevance threshold)")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"*{_ADVISORY_DISCLAIMER}*")

        formatted_answer = "\n".join(lines)

        # Domain audit: final answer composed (advisory line attached).
        emit_trace_event(
            "output_format_complete",
            {
                "answer_chars": len(formatted_answer),
                "citation_count": len(citations),
                "focus_labels": len(focus_labels),
            },
            state,
        )

        return {
            "formatted_answer": formatted_answer,
            "status": AgentStatus.SUCCESS.value,
        }
