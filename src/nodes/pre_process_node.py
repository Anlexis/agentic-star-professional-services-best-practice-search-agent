"""AgentCore Platform v1.0"""

# SVC-C2-005 — PreProcessNode (outer pre_process slot; the external-facing gate)
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level — the trust gate)
#   - Reject empty / non-string / over-long input early, fail fast
#   - Refuse instruction-override payloads in the query text
#   - Validate every caller-supplied field against explicit bounds and fail CLOSED
#     on any violation, naming the FIELD and never echoing the VALUE
#   - Surface-strip direct client / engagement identifiers from the query text so a
#     raw confidential identifier never reaches a domain node or the checkpoint
#   - Publish the validated query and the validated search request for the pipeline
#
# Caller data arrives on two channels and both are validated here:
#
#   input          the consultant's question. Either plain text or a JSON envelope
#                  carrying it under query / question.
#   input_context  the structured channel. Fields:
#                    channel       inert slug, [a-z0-9_]{1,32}
#                    category      inert slug, [a-z0-9_]{1,32} — knowledge-base
#                                  category filter
#                    top_k         finite integer, 1..20 — result count
#                    min_score     finite number, 0..1 — caller-tightened
#                                  relevance floor
#                    focus_labels  list of engagement focus terms (service line,
#                                  practice area, delivery phase), entry-capped;
#                                  each boosts matching passages and is rendered
#                                  into the answer
#
# The structured channel carries the focus labels because the query field is one
# the framework's input gate rewrites: its personal-name heuristic matches any two
# consecutive capitalised words, which is exactly the shape of a service line
# ("Change Management", "Cloud Assessment", "Discovery Phase"), so a label carried
# there would arrive as a mask token and the ranking would key off the mask.
# Moving the labels off that field is only half the fix — the fields that travel on
# the structured channel are screened HERE instead, and a contact identifier in one
# of them is refused rather than masked.
#
# When no structured request is submitted the run degrades to the plain query and
# the declared defaults. There is no path on which unvalidated caller data reaches
# the pipeline.
#
# Returns ONLY the state keys this node writes (partial-dict contract).
# Node contract: extend FunctionNode; implement execute(self, state) -> dict — no
# extra parameter. Config reaches nodes through State seeding, never a per-call
# execute() argument.

import json
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, TOO_LONG
from src.schemas.state import from_json, to_json

# Hard cap on the query text (defence in depth on input size).
_MAX_QUERY_CHARS = 2000

# Focus-label entry cap used when config/config.yaml declares none.
_DEFAULT_MAX_FOCUS_LABELS = 10

_MAX_FOCUS_LABEL_CHARS = 60

# Caller top_k / relevance-floor bounds. Both are also enforced downstream; the
# contract is stated once here so a violation is refused at the boundary rather
# than silently clamped inside the pipeline.
_TOP_K_MIN, _TOP_K_MAX = 1, 20
_MIN_SCORE_MIN, _MIN_SCORE_MAX = 0.0, 1.0

# Caller strings that select behaviour are inert identifiers — lowercase
# alphanumerics and underscore, bounded length. Free text in such a field is
# caller-controlled output and log injection.
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Focus labels are the only caller free text that reaches the rendered answer, so
# they are locked to a label alphabet: word characters in any script — so a
# Japanese practice-area name is accepted as readily as an English one — plus
# space, ampersand and the punctuation service lines use ("Cloud & Data",
# "Post-Merger Integration", "Finance (Controllership)"). Everything else —
# quotes, brackets, hashes, asterisks, backticks, angle brackets, pipes,
# backslashes, colons, control characters and every newline — is refused, so a
# label can neither carry markup nor break the line structure of the document it
# is rendered into.
_LABEL_RE = re.compile(r"^[\w &.,()/+-]+$")

# Space runs only — a label's tabs and newlines are refused, never collapsed.
_SPACES_RE = re.compile(r" +")
_WHITESPACE_RE = re.compile(r"\s+")

# Control characters (tab and newline excepted) are stripped from the query before
# it is measured and normalised.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Surface-level identifier patterns redacted from the query text before
# validated_input is written, and REFUSED outright on the structured channel.
# Downstream domain nodes only ever operate on the normalised query, knowledge-base
# passage summaries, and validated labels — never a raw client/engagement
# identifier.
# Each pattern is written to consume the WHOLE identifier it fires on, not just
# the part that matched the reference shape. A partial redaction is worse than
# none: "ENE-FAC-20260712-001" reduced to "ENE-[REDACTED]-001" both destroys the
# token and leaves its structure and suffix in the clear. The single-character
# guard classes at each end are what makes the match whole — they refuse to start
# or stop in the middle of an identifier run.
_IDENT_GUARD_L = r"(?<![A-Za-z0-9_-])"
_IDENT_GUARD_R = r"(?![A-Za-z0-9_-])"

_CLIENT_IDENTIFIER_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    # Client / engagement reference codes: an identifier run carrying a 2-6 letter
    # prefix + hyphen + 4-8 digits anywhere inside it ("ENG-2026-00123",
    # "CLI-004521", "ENE-FAC-20260712-001").
    (
        "engagement_reference",
        re.compile(_IDENT_GUARD_L + r"[A-Za-z0-9-]*[A-Z]{2,6}-\d{4,8}[A-Za-z0-9-]*" + _IDENT_GUARD_R),
    ),
    # Long client / contract reference numbers: 10-19 digits, optionally grouped in
    # fours. The guards extend the match over the whole run so a longer number is
    # never reduced to a redacted middle with its ends still readable.
    (
        "client_reference_number",
        re.compile(_IDENT_GUARD_L + r"\d{4}[- ]?\d{4}[- ]?\d{2,11}" + _IDENT_GUARD_R),
    ),
    # E-mail addresses.
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]
_REDACTION = "[REDACTED]"

# Instruction-override phrasings: text addressed to the MODEL rather than a
# knowledge-base question addressed to this agent. The platform input gate refuses
# high-confidence payloads too, but this template must not depend on that — where
# that gate is absent or configured off, an unchecked payload would reach the
# retrieval path and an answer would be returned. Refusal is stated in terms of
# BEHAVIOUR (error status, no answer, no citations), never a gate's wording.
#
# Deliberately narrow: each alternative requires the imperative override shape, so
# a genuine professional-services question that happens to use these words is
# unaffected ("How do I override the standard discovery timeline?", "Which prior
# instructions govern subcontracted delivery?", "Who acts as an approver while the
# engagement manager is on leave?" all pass — see the probes in
# tests/unit/test_caller_contract.py).
_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)"
    r"|disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)"
    r"|forget\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts)"
    r"|(?:reveal|show|print|repeat|output|disclose)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|instructions|initial\s+prompt)"
    r"|you\s+are\s+now\s+(?:a|an)\s"
    r"|act\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root)\s+mode"
    r"|override\s+(?:your|the)\s+(?:instruction|instructions|rules|safety)",
    re.IGNORECASE,
)

# Contact identifiers have no place in an engagement focus label, and the label
# alphabet alone does not exclude them (it permits digits, spaces, dots and
# hyphens). Only high-precision detector types are screened:
#   - the personal-name heuristics are excluded, because they match any two Title
#     Case words, which is what a service line IS ("Change Management", "Cloud
#     Assessment") — screening on them would refuse ordinary labels, which is the
#     very failure this channel exists to avoid;
#   - every remaining type is a shape no service line has, and each is a personal
#     identifier that must not enter a knowledge-base answer, so a label carrying
#     one is refused outright rather than masked.
_SCREENED_PII_TYPES = frozenset({"email", "phone_jp", "phone_us", "ssn_us", "credit_card", "my_number_jp"})


def _surface_strip_identifiers(text: str) -> str:
    """Redact direct client / engagement identifier tokens from free-text query."""
    for _name, pattern in _CLIENT_IDENTIFIER_PATTERNS:
        text = pattern.sub(_REDACTION, text)
    return text


# Fields the legacy JSON envelope may carry alongside the query. Both are shapes
# the framework input gate leaves alone — an inert lowercase slug and a one-or-two
# digit count — so they remain usable on the query channel. The focus labels are
# NOT accepted here: they are Title Case by nature and would arrive masked.
_ENVELOPE_REQUEST_KEYS = ("category", "top_k")


def _extract_request(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Split the query channel into (query text, request fields it carried).

    A plain string is the whole query. A JSON envelope
    ({"query": ..., "category": ..., "top_k": ...}) yields the query plus the
    request fields it declared; those are merged with the structured channel and
    validated by the same rules, so there is one contract, not two.
    """
    text = raw.strip()
    if not text:
        return "", {}
    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text, {}  # not JSON — treat the whole string as the query
        if isinstance(payload, dict):
            query = ""
            for key in ("query", "question", "text"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    query = value.strip()
                    break
            carried = {key: payload[key] for key in _ENVELOPE_REQUEST_KEYS if key in payload}
            return query, carried
    return text, {}


def _finite_in_range(value: Any, field: str, lo: float, hi: float) -> Tuple[Optional[float], Optional[str]]:
    """Validate one caller-supplied number. Returns (value, error).

    Fail CLOSED. Bools, strings and other non-numerics are refused, and so are NaN
    and +/-Infinity: those parse through float() and arrive intact in a raw JSON
    body, and every comparison against NaN is False — so a NaN relevance floor
    would pass every bound check and then admit every passage, disabling the
    filter the caller asked to tighten. The rejected value is never echoed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"PreProcessNode: {field} must be a number between {lo:g} and {hi:g}"
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None, f"PreProcessNode: {field} must be a number between {lo:g} and {hi:g}"
    return parsed, None


def _validate_slug(value: Any, field: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate one inert caller identifier. Returns (value, error). Fail CLOSED."""
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        return None, (f"PreProcessNode: {field} must be 1-32 characters of a-z, 0-9 and underscore")
    return value, None


def _validate_focus_label(value: Any, index: int) -> Tuple[Optional[str], Optional[str]]:
    """Validate one caller focus label. Returns (normalised_value, error).

    Fail CLOSED: only a string of the label alphabet within the length limit is
    accepted. The rejected value is never echoed — the error names the field
    position and restates the contract.

    The alphabet is checked on the value AS SUPPLIED, before space runs are
    collapsed. Normalising first would silently repair a label carrying a newline
    or a tab into an acceptable one, and caller-controlled line structure is
    exactly what this contract exists to keep out of the rendered answer.
    """
    field = f"focus_labels[{index}]"
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        return None, (
            f"PreProcessNode: {field} must be {_MAX_FOCUS_LABEL_CHARS} characters or "
            "fewer of letters, digits, space and . , ( ) / & + -"
        )
    collapsed = _SPACES_RE.sub(" ", value).strip()
    if not collapsed or len(collapsed) > _MAX_FOCUS_LABEL_CHARS:
        return None, (
            f"PreProcessNode: {field} must be {_MAX_FOCUS_LABEL_CHARS} characters or "
            "fewer of letters, digits, space and . , ( ) / & + -"
        )
    if [f for f in detect_pii(collapsed) if f.get("type") in _SCREENED_PII_TYPES]:
        return None, f"PreProcessNode: {field} must not contain contact identifiers"
    for name, pattern in _CLIENT_IDENTIFIER_PATTERNS:
        if pattern.search(collapsed):
            return None, (f"PreProcessNode: {field} must not contain a client or engagement " f"identifier ({name})")
    # The structured channel gets the same instruction-override screen the query
    # gets. A label is short and alphabet-restricted, but an override instruction
    # fits inside both bounds, and labels are rendered into the answer — so a
    # screen applied only to the query would leave the structured channel as an
    # open path to the same content.
    if _INJECTION_RE.search(collapsed):
        return None, f"PreProcessNode: {field} refused - instruction-override content"
    return collapsed, None


def _validate_request(input_context: Any, max_focus_labels: int) -> Tuple[Dict[str, Any], Optional[str]]:
    """Validate the structured caller request. Returns (record, error).

    An absent or empty channel degrades to the baseline request — the plain query
    against the declared defaults. Any field that IS supplied must satisfy its
    bound; there is no partial acceptance and no clamping.
    """
    record: Dict[str, Any] = {}
    if input_context in (None, {}):
        return record, None
    if not isinstance(input_context, dict):
        return {}, "PreProcessNode: input_context must be an object"

    if "channel" in input_context:
        channel, error = _validate_slug(input_context.get("channel"), "channel")
        if error:
            return {}, error
        record["channel"] = channel

    if "category" in input_context:
        category, error = _validate_slug(input_context.get("category"), "category")
        if error:
            return {}, error
        record["category"] = category

    if "top_k" in input_context:
        top_k, error = _finite_in_range(input_context.get("top_k"), "top_k", _TOP_K_MIN, _TOP_K_MAX)
        if error:
            return {}, error
        assert top_k is not None  # _finite_in_range returns a value whenever error is None
        if top_k != int(top_k):
            return {}, (f"PreProcessNode: top_k must be a whole number between " f"{_TOP_K_MIN} and {_TOP_K_MAX}")
        record["top_k"] = int(top_k)

    if "min_score" in input_context:
        min_score, error = _finite_in_range(input_context.get("min_score"), "min_score", _MIN_SCORE_MIN, _MIN_SCORE_MAX)
        if error:
            return {}, error
        record["min_score"] = min_score

    if "focus_labels" in input_context:
        raw_labels = input_context.get("focus_labels")
        if not isinstance(raw_labels, list):
            return {}, "PreProcessNode: focus_labels must be a list"
        if len(raw_labels) > max_focus_labels:
            return {}, (f"PreProcessNode: focus_labels must hold {max_focus_labels} entries " "or fewer")
        labels: List[str] = []
        for index, raw_label in enumerate(raw_labels):
            label, error = _validate_focus_label(raw_label, index)
            if error:
                return {}, error
            if label is not None:
                labels.append(label)
        record["focus_labels"] = labels

    return record, None


class PreProcessNode(FunctionNode):
    """Trust gate, caller-data contract and confidentiality screen.

    Rejects empty / invalid input before the inner workflow runs, refuses a caller
    field that breaks its bound, surface-strips direct client and engagement
    identifiers from the query, and publishes the validated request.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        if not raw_input or not isinstance(raw_input, str) or not raw_input.strip():
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        query, envelope_request = _extract_request(_CONTROL_CHARS_RE.sub("", raw_input))
        query = _WHITESPACE_RE.sub(" ", query).strip()
        if not query:
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input carries no query text"],
            }
        if len(query) > _MAX_QUERY_CHARS:
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: query must be {_MAX_QUERY_CHARS} characters or fewer"],
            }
        # This one terminates. The checks above complete carrying a reason
        # because the caller can correct the value; a refusal is not a value to
        # correct, and reporting it the same way would read as an invitation to
        # reword the request until it is accepted.
        if _INJECTION_RE.search(query):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: query refused - instruction-override content"],
            }

        settings = from_json(state.get("runtime_settings"), {}) or {}
        max_focus_labels = settings.get("max_focus_labels")
        if not isinstance(max_focus_labels, int) or max_focus_labels < 0:
            max_focus_labels = _DEFAULT_MAX_FOCUS_LABELS

        # The structured channel is the explicit one, so it wins on a key both
        # channels declare; either way the merged mapping goes through one validator.
        merged_request: Dict[str, Any] = dict(envelope_request)
        if isinstance(input_context, dict):
            merged_request.update(input_context)
        elif input_context not in (None, {}):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        record, error = _validate_request(merged_request, max_focus_labels)
        if error:
            emit_progress(INPUT_REJECTED)
            return {"status": AgentStatus.SUCCESS.value, "error_code": "INVALID_REQUEST", "error_log": [error]}

        validated_input = _surface_strip_identifiers(query)

        # Domain audit: a search request was accepted and surface-redacted (no direct
        # client or engagement identifier in the payload).
        emit_trace_event(
            "pre_process_complete",
            {
                "input_chars": len(validated_input),
                "focus_labels": len(record.get("focus_labels") or []),
                "has_category": "category" in record,
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "caller_request": to_json(record),
            "enriched_context": {
                "source": "ProfessionalServicesKnowledgeAgent",
                "channel": record.get("channel", "unknown"),
            },
            "status": AgentStatus.SUCCESS.value,
        }
