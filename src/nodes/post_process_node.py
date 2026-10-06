"""AgentCore Platform v1.0"""

# SVC-C2-005 - PostProcessNode (the output boundary)
#
# Reads the final knowledge-base answer from state["result"], populated by
# KnowledgeSearchGraphNode.merge_output() from the inner graph's formatted_answer,
# and surfaces it as the finalized output only AFTER both boundary layers pass.
#
# TWO INDEPENDENT LAYERS, each with its own audit event, neither relying on the
# other:
#
#   1. Credential scan - the module-level `_security_gate_output()` below,
#      called from execute(). It RECURSES into dict / list / tuple / set
#      containers rather than inspecting only a top-level string, so a
#      violation nested inside a structured payload is never missed even if a
#      future caller passes one. API keys, JWT and bearer tokens and raw
#      credential assignments are matched.
#
#   2. Document invariant - `_enforce_document_invariant()` below. This
#      template's stated output contract is that the rendered document's
#      STRUCTURE is its own: exactly one title line, exactly one Sources
#      heading, every source line backed by a citation the pipeline produced,
#      and the advisory line always present. Caller text reaches the document
#      only as single-line quoted fragments, so a document that no longer
#      satisfies the invariant is not a formatting nit - it is caller-controlled
#      structure that escaped the render path, and it is refused.
#
# This template renders no monetary aggregate, so the round-to-the-nearest-1,000
# grid other agents enforce at this boundary does NOT apply here; see
# docs/02_design.md "Output boundary". A numeric snap would in fact be wrong for
# a knowledge-base agent: passages are quoted from the corpus, and rewriting a
# figure inside a quoted passage would falsify the guidance the answer is
# grounded in. Because nothing here rewrites the text, both layers are pure
# scans and their order carries no risk of one destroying evidence the other
# needs.
#
# On a violation of either layer, the surfaced formatted_output and result are
# replaced with a sanitised stub and ERROR status is returned. No
# _extra_security_gate_input/_output instance methods are defined on this node
# (the framework auto-wraps such hooks).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Disallowed content patterns for the credential layer.
# Each tuple: (name, compiled regex) - order matters (most specific first).
_DISALLOWED_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    # API key patterns: sk-..., pk-..., ak-...
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # JWT: three base64url segments separated by dots
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Bearer token in Authorization-like context
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/]{20,}", re.IGNORECASE)),
    # Credential assignment patterns
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
]

_SANITISED_STUB = (
    "[OUTPUT BLOCKED at the output boundary - the answer did not satisfy the "
    "published output contract. Review the generated knowledge-base answer and "
    "retry without credential-like strings.]"
)

# The document chrome this template renders. The invariant is stated in terms of
# these literals so a drift in the renderer is caught here rather than shipped.
_TITLE_HEADING = "# Professional Services Knowledge Base Search Result"
_SOURCES_HEADING = "## Sources"
_NO_SOURCES_LINE = "- none (no knowledge-base passage cleared the relevance threshold)"
_DISCLAIMER_MARKER = "does not replace engagement-specific"

# Every clause is LINE-ANCHORED. Structure is a property of lines, and a caller
# may legitimately mention a heading inline — a question quoting "## Sources" is
# an ordinary thing to ask about a document, and a substring check would refuse to
# answer it. What must never happen is caller text BECOMING a line of structure.
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)
_TITLE_LINE_RE = re.compile(r"^" + re.escape(_TITLE_HEADING) + r"$", re.MULTILINE)
_SOURCES_LINE_RE = re.compile(r"^" + re.escape(_SOURCES_HEADING) + r"$", re.MULTILINE)
_SOURCE_LINE_RE = re.compile(r"^- \[(\d+)\] ", re.MULTILINE)


def _enforce_document_invariant(document: str, citations: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """Check the rendered document against this template's stated output contract.

    Returns the name of the first violated clause, or None when the document is
    the shape this template publishes. Purely a scan - nothing is rewritten, so
    the check is order-independent with respect to the credential layer.

    Clauses:
      title_heading      exactly one line that is this template's title
      sources_heading    exactly one line that is the Sources heading
      stray_heading      no other heading line of any level - caller text that
                         became a section would appear here
      source_ref         every "- [n] " line carries a ref the pipeline emitted
                         (checked only when the citation list is supplied)
      disclaimer         the standing advisory line is present
    """
    if len(_TITLE_LINE_RE.findall(document)) != 1:
        return "title_heading"
    if len(_SOURCES_LINE_RE.findall(document)) != 1:
        return "sources_heading"
    headings = _HEADING_RE.findall(document)
    if len(headings) != 2:
        return "stray_heading"
    if _DISCLAIMER_MARKER not in document:
        return "disclaimer"
    if citations is not None:
        refs = {str(citation.get("ref")) for citation in citations if isinstance(citation, dict)}
        rendered = set(_SOURCE_LINE_RE.findall(document))
        if rendered - refs:
            return "source_ref"
        if not refs and _NO_SOURCES_LINE not in document:
            return "source_ref"
    return None


def _security_gate_output(content: Any) -> Optional[str]:
    """Run the credential scan over the outbound content.

    Recurses into dict / list / tuple / set containers so a violation nested
    inside a structured payload is not missed. This template's own output is a
    flat string today, but a scan that inspects only top-level strings is blind
    to a value carried one level down, so the scan does not assume the shape.

    Returns the name of the first matched violation, or None if clean.
    """
    if content is None:
        return None
    if isinstance(content, str):
        for name, pattern in _DISALLOWED_PATTERNS:
            if pattern.search(content):
                return str(name)
        return None
    if isinstance(content, dict):
        for value in content.values():
            violation = _security_gate_output(value)
            if violation:
                return violation
        return None
    if isinstance(content, (list, tuple, set)):
        for item in content:
            violation = _security_gate_output(item)
            if violation:
                return violation
        return None
    # Any other scalar (int, float, bool) carries no credential-shaped content.
    return None


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """The output boundary: credential scan + document invariant, fail closed.

    Outer backbone post_process slot. Reads state["result"] (the merged
    formatted_answer from KnowledgeSearchGraphNode.merge_output()) and applies both
    boundary layers from execute() before the response reaches the caller.

    Input state keys:
        result:    final formatted knowledge-base answer (from merge_output)
        citations: JSON list the pipeline emitted, used to check that every
                   rendered source line is backed by a real citation

    Output state keys (partial dict):
        formatted_output: sanitised output (unchanged answer if clean; blocked
                          stub on violation)
        result:           gated alongside formatted_output (blocked stub on
                          violation; unchanged on the clean/empty paths)
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
                          (plain strings — the bare enum is never written to State)
        error_log:        (on error) list of error messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
                "result": message,
            }
        result = state.get("result") or ""

        if not result or not str(result).strip():
            # No answer was generated - forward as-is (non-fatal).
            return {
                "formatted_output": result,
                "status": AgentStatus.SUCCESS.value,
            }

        document = str(result)

        violation = _security_gate_output(result)
        if violation:
            logger.error(
                "PostProcessNode: OUTPUT BLOCKED - credential violation type: %s",
                violation,
            )
            emit_trace_event(
                "output_blocked_credential",
                {"violation": violation},
                state,
            )
            return {
                "formatted_output": _SANITISED_STUB,
                "result": _SANITISED_STUB,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked - disallowed content " f"detected ({violation})"],
            }

        clause = _enforce_document_invariant(document, from_json(state.get("citations"), []) or [])
        if clause:
            logger.error(
                "PostProcessNode: OUTPUT BLOCKED - document invariant clause: %s",
                clause,
            )
            emit_trace_event(
                "output_blocked_document_invariant",
                {"clause": clause},
                state,
            )
            return {
                "formatted_output": _SANITISED_STUB,
                "result": _SANITISED_STUB,
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked - document invariant not " f"satisfied ({clause})"],
            }

        # Clean - domain audit: record that a finalized answer was emitted.
        emit_trace_event(
            "post_process_complete",
            {"output_chars": len(document)},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
