# SVC-C2-005 — Unit Tests: PreProcessNode (outer pre_process slot)
#
# Invocation canon: every test here invokes the node via node(state) —
# BaseNode.__call__ → trust gate → framework input gate → execute() → framework
# output gate — never a bare node.execute(state). PreProcessNode
# requires VERIFIED_EXTERNAL, so its behavioural tests build the state at that
# level (the ANONYMOUS rejection lives in test_trust_gate.py).
#
# Layering note: the FRAMEWORK input gate masks user_input /
# validated_input before execute() runs — e-mails and digit-group runs surface
# as [MASKED]. The NODE's own surface strip then catches engagement/client
# reference-code and long-reference-number shapes the framework patterns do
# not, and replaces them with [REDACTED]. Intentional-PII tests therefore
# assert the raw identifier is GONE and the corresponding masked marker is
# present.
#
# Mirrors docs/03_test_spec.md §2.1 (PRE-01..PRE-08).
# Deterministic — no LLM, no network. framework.* / src.* imports only.

from unittest.mock import MagicMock

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

import src.nodes.pre_process_node
from src.nodes.pre_process_node import PreProcessNode

# Lowercase phrasing on purpose: PII-free (no Title-Case bigram, no @, no
# digit run), so the framework mask leaves the payload untouched.
_VALID_QUERY = "what is our standard discovery-phase methodology for an sap migration?"


def _make_state(user_input=_VALID_QUERY, **extra) -> dict:
    state = {
        "user_input": user_input,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPreProcessSuccess:
    def test_pre_01_valid_query_accepted(self):
        result = PreProcessNode()(_make_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: State carries the plain string, never the bare enum.
        assert isinstance(result["status"], str)
        assert not isinstance(result["status"], AgentStatus)
        assert result["validated_input"] == _VALID_QUERY

    def test_enriched_context_carries_channel(self):
        result = PreProcessNode()(_make_state(input_context={"channel": "web"}))
        assert result["enriched_context"]["channel"] == "web"
        assert result["enriched_context"]["source"] == "ProfessionalServicesKnowledgeAgent"

    def test_missing_channel_defaults_to_unknown(self):
        result = PreProcessNode()(_make_state())
        assert result["enriched_context"]["channel"] == "unknown"


class TestPreProcessRejection:
    def test_pre_02_empty_input_is_error(self):
        result = PreProcessNode()(_make_state(user_input=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")
        assert result["error_log"]
        # No validated_input is produced on the reject path.
        assert "validated_input" not in result

    def test_whitespace_only_is_error(self):
        result = PreProcessNode()(_make_state(user_input="   \n\t "))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")

    def test_pre_03_missing_user_input_is_error(self):
        state = _make_state()
        del state["user_input"]
        result = PreProcessNode()(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")

    def test_non_string_input_is_error(self):
        result = PreProcessNode()(_make_state(user_input={"malicious": "dict"}))
        assert result["status"] == AgentStatus.SUCCESS.value
        # Completes carrying the reason, so the caller can correct the value and send the request again.
        assert result.get("error_code")


class TestPreProcessIdentifierScreen:
    """PRE-04: raw client/engagement identifiers never survive into validated_input."""

    def test_engagement_code_redacted_by_node_screen(self):
        # Engagement/client reference codes are NOT in the framework
        # patterns — the node's own surface strip must catch them
        # ([REDACTED] path).
        raw = "confirm the discovery scope for engagement code ENG-045821 before kickoff"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "ENG-045821" not in vi
        assert "[REDACTED]" in vi

    def test_email_masked_by_framework_s2_gate(self):
        # The framework input gate masks e-mail before execute() sees it.
        raw = "escalate the engagement question to delivery.lead@example.com today"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "delivery.lead@example.com" not in vi
        assert "[MASKED]" in vi

    def test_grouped_reference_digits_masked(self):
        # 4-4-4 digit groups match the framework's number patterns.
        raw = "client reference 1234 5678 9012 needs the benchmark figures"
        result = PreProcessNode()(_make_state(user_input=raw))
        vi = result["validated_input"]
        assert "1234 5678 9012" not in vi
        assert "[MASKED]" in vi


class TestPreProcessAudit:
    def test_pre_08_domain_audit_payload(self, monkeypatch):
        """The accepted request emits pre_process_complete; the assertion
        targets call.args[1] — the event payload — never the whole call repr."""
        spy = MagicMock()
        monkeypatch.setattr(src.nodes.pre_process_node, "emit_trace_event", spy)
        PreProcessNode()(_make_state())
        events = [call.args[0] for call in spy.call_args_list]
        assert "pre_process_complete" in events
        payload = spy.call_args_list[events.index("pre_process_complete")].args[1]
        assert payload["input_chars"] == len(_VALID_QUERY)
