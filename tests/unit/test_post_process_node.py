# SVC-C2-005 — Unit Tests: PostProcessNode (the output boundary)
#
# Invocation canon: node(state) via BaseNode.__call__. PostProcessNode is the
# second outer gate slot and requires VERIFIED_EXTERNAL (like PreProcessNode), so
# its behavioural tests build the state at that level; the ANONYMOUS rejection
# lives in test_trust_gate.py.
#
# The boundary runs two independent layers inside execute() and replaces a
# violating answer with the sanitised stub (returned dict — no exception):
#   1. the credential scan, which RECURSES into dict/list/tuple, so these tests
#      cover a nested-payload violation as well as a flat string;
#   2. the document invariant, which checks the rendered structure against the
#      contract this template publishes.
# The framework's own output scan then sees only the clean stub.
# Intentional-credential tests assert the raw secret never survives into
# formatted_output OR result.
#
# Mirrors docs/03_test_spec.md §2.7 (POST-01..POST-08).
# Deterministic — no network. framework.* / src.* imports only.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    PostProcessNode,
    _enforce_document_invariant,
    _security_gate_output,
)
from src.schemas.state import to_json

# A document in the shape OutputFormatNode renders: one title, one Sources
# heading, a source line per citation, and the standing advisory line.
_CLEAN_ANSWER = (
    "# Professional Services Knowledge Base Search Result\n"
    "\n"
    "[1] discovery-phase methodology confirmed for the migration engagement.\n"
    "\n"
    "## Sources\n"
    "- [1] Discovery-phase methodology (Methodology Library)\n"
    "\n"
    "---\n"
    "\n"
    "*This answer is informational only. It does not replace engagement-specific "
    "analysis or client-specific advice.*"
)
_CLEAN_CITATIONS = [{"ref": 1, "id": "kb-001", "title": "Discovery-phase methodology", "source": "Methodology Library"}]

# JWT-shaped token built at runtime so no credential-shaped literal ever sits
# in the repository (credential-scan hygiene).
_FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12


def _make_state(result_text, citations=None, **extra) -> dict:
    state = {
        "result": result_text,
        "citations": to_json(_CLEAN_CITATIONS if citations is None else citations),
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
        "session_id": "unit-session",
        "execution_time": {},
    }
    state.update(extra)
    return state


class TestPostProcessClean:
    def test_post_01_clean_output_passes_through(self):
        result = PostProcessNode()(_make_state(_CLEAN_ANSWER))
        assert result["status"] == AgentStatus.SUCCESS.value
        # State carries the plain string, never the bare enum.
        assert isinstance(result["status"], str)
        assert not isinstance(result["status"], AgentStatus)
        assert result["formatted_output"] == _CLEAN_ANSWER

    def test_post_02_empty_result_is_non_fatal(self):
        result = PostProcessNode()(_make_state(""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == ""


class TestPostProcessCredentialLayer:
    def _assert_blocked(self, result, secret):
        assert result["status"] == AgentStatus.ERROR.value
        assert any("output blocked" in str(e) for e in result["error_log"])
        # The raw secret must not survive into either surfaced field.
        assert secret not in str(result.get("formatted_output", ""))
        assert secret not in str(result.get("result", ""))
        assert "[OUTPUT BLOCKED at the output boundary" in result["formatted_output"]

    def test_post_03_api_key_is_blocked(self):
        secret = "sk-ABCDEF0123456789abcdef"
        result = PostProcessNode()(_make_state(f"{_CLEAN_ANSWER}\n<!-- debug api_key={secret} -->"))
        self._assert_blocked(result, secret)

    def test_post_04_credential_assignment_is_blocked(self):
        secret = "password=super_secret_value_123"
        result = PostProcessNode()(_make_state(f"{_CLEAN_ANSWER}\ninternal note: {secret}"))
        self._assert_blocked(result, "super_secret_value_123")

    def test_post_05_jwt_is_blocked(self):
        result = PostProcessNode()(_make_state(f"{_CLEAN_ANSWER}\nsession token {_FAKE_JWT}"))
        self._assert_blocked(result, _FAKE_JWT)

    def test_post_06_bearer_token_is_blocked(self):
        secret = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
        result = PostProcessNode()(_make_state(f"{_CLEAN_ANSWER}\nauthorization: {secret}"))
        self._assert_blocked(result, secret)


class TestDocumentInvariantLayer:
    """The published output contract: the document's structure is this template's
    own. Both directions are probed — a real rendered answer passes, and each
    clause is shown to fire on the document that breaks it. A gate checked only
    against violations cannot tell "enforcing" from "always failing"."""

    def test_post_07_a_real_answer_satisfies_every_clause(self):
        assert _enforce_document_invariant(_CLEAN_ANSWER, _CLEAN_CITATIONS) is None

    def test_post_08_each_clause_fires_on_the_document_that_breaks_it(self):
        cases = {
            "stray_heading": _CLEAN_ANSWER.replace("## Sources", "## Sources\n\n### Client Rate Card"),
            "title_heading": _CLEAN_ANSWER + "\n\n# Professional Services Knowledge Base Search Result",
            "sources_heading": _CLEAN_ANSWER.replace("## Sources", "Sources"),
            "disclaimer": _CLEAN_ANSWER.replace("does not replace engagement-specific", "is fine to use"),
            "source_ref": _CLEAN_ANSWER.replace("## Sources\n", "## Sources\n- [9] Approved Rate Card (billing)\n"),
        }
        for clause, document in cases.items():
            assert _enforce_document_invariant(document, _CLEAN_CITATIONS) == clause

    def test_a_forged_section_is_refused_end_of_node(self):
        forged = _CLEAN_ANSWER.replace("## Sources", "## Sources\n\n## Approved Rates")
        result = PostProcessNode()(_make_state(forged))
        assert result["status"] == AgentStatus.ERROR.value
        assert "document invariant" in str(result["error_log"])
        assert "[OUTPUT BLOCKED at the output boundary" in result["formatted_output"]

    def test_numbers_are_never_rewritten_at_this_boundary(self):
        # This template renders no monetary aggregate, so no rounding grid applies
        # here; both layers are pure scans. A boundary that rewrote figures would
        # falsify the passages the answer is grounded in — see docs/02_design.md.
        for value in (
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            "JPY 1234.56",
            "JPY 1,000",
            "kb-001",
            "sku_48210",
            "STAR 2026",
        ):
            document = _CLEAN_ANSWER.replace("engagement.", f"engagement, {value}.")
            result = PostProcessNode()(_make_state(document))
            assert result["formatted_output"] == document, value


class TestCredentialScanRecursion:
    """The credential scan must recurse into containers — not just the string
    call sites execute() happens to use today. A value carried one level down is
    exactly what a shallow scan misses; the flat control proves the scan itself
    works, so a clean nested result cannot be read as "the probe was wrong"."""

    def test_clean_string_returns_none(self):
        assert _security_gate_output(_CLEAN_ANSWER) is None

    def test_none_returns_none(self):
        assert _security_gate_output(None) is None

    def test_violation_nested_in_dict_is_found(self):
        secret = "sk-ABCDEF0123456789abcdef"
        nested = {"outer": {"inner": f"note: api_key={secret} here"}}
        assert _security_gate_output(nested) == "api_key"

    def test_violation_nested_in_list_of_dicts_is_found(self):
        secret = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
        nested = [{"a": "clean"}, {"b": f"authorization: {secret}"}]
        assert _security_gate_output(nested) == "bearer_token"

    def test_violation_nested_in_tuple_is_found(self):
        secret = "password=super_secret_value_123"
        nested = ("clean entry", {"note": secret})
        assert _security_gate_output(nested) == "credential_assignment"

    def test_scalar_non_string_is_clean(self):
        assert _security_gate_output(42) is None
        assert _security_gate_output(True) is None
