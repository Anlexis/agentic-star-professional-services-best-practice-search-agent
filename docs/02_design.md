# Template Design Specification — SVC-C2-005

**Template ID:** SVC-C2-005
**Template Name:** ProfessionalServicesKnowledgeAgent
**Category:** Cat 2 (multi-step domain workflow — retrieval pattern)
**Industry:** SVC

## Position in the framework

| Aspect | Value |
|---|---|
| Agent class | `ProfessionalServicesKnowledgeAgent` (alias `Graph`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Inner graph base | `BaseGraph` — `DomainWorkflowGraph` |
| Pattern | Two-layer nested: fixed outer backbone + a `GraphNode` in the `main` slot wrapping an inner workflow |
| State | flat `TypedDict` composition (never Pydantic — msgpack incompatible); structured fields stored as JSON strings via `to_json()` / `from_json()` |
| Node | `FunctionNode` subclasses; override `execute(self, state) -> dict` only — no extra parameter. Config reaches a node through State (see "Runtime configuration") |
| Graph | composition via `register_nodes()`; the outer `add_edges()` is NOT overridden |

## Purpose

An internal knowledge-base search agent for professional-services delivery. Consultant,
associate and engagement-manager questions over methodology, regulatory and industry guidance,
benchmark material and past-engagement insight are answered from a seeded corpus: retrieve →
rerank and filter → grounded answer with citations, plus a standing advisory line. Retrieval and
answer assembly are deterministic — keyword scoring over the corpus and rule-based assembly from
the retrieved passages — so the same question always returns the same answer and no model is
called.

## Architecture

### Outer backbone (`AgentBaseGraph`)

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry, bounded by max_retry)
                                   pre_process
```

| Slot | Class | Responsibility | required_trust_level |
|------|-------|----------------|----------------------|
| initialize | framework default | session_id, trust_level, schema_version | — (framework) |
| pre_process | `PreProcessNode` | trust gate; validate the query; refuse instruction-override payloads; validate every caller field against its bound; surface-strip client/engagement identifiers → `validated_input`, `caller_request` | `VERIFIED_EXTERNAL` |
| main | `KnowledgeSearchGraphNode` (`GraphNode`) | delegates to the inner `DomainWorkflowGraph`; maps the inner `formatted_answer` → outer `result` | — (delegation) |
| post_process | `PostProcessNode` | the output boundary — credential scan + document invariant, fail closed | `VERIFIED_EXTERNAL` |
| finalize | framework default | response metadata, total time | — (framework) |

### Inner graph (`DomainWorkflowGraph` — `BaseGraph`, linear)

```
START → input_validate → retrieve → rerank_filter → generate_answer → output_format → END
```

All five inner nodes declare `required_trust_level = TrustLevel.ANONYMOUS`. The external trust
gate lives on the outer backbone; a stricter inner level would deny a real authenticated invoke
at runtime.

| Node | Responsibility | Input State | Output State |
|------|----------------|-------------|--------------|
| `InputValidateNode` | normalise the query; re-check the SHAPE of the transported caller request and publish the search filters | `validated_input`, `input_context` | `search_query`, `query_filters`, `intake_notes` |
| `RetrieveNode` | keyword retrieval over the seeded corpus: tokenise the query AND the caller's focus labels, score title / tags / content overlap, apply the category filter | `search_query`, `query_filters`, `retrieval_config` | `retrieved_documents`, `intake_notes` |
| `RerankFilterNode` | category-match boost, focus-label boost, drop everything below the relevance floor, cap at the result count | `retrieved_documents`, `query_filters`, `retrieval_config` | `ranked_documents` |
| `GenerateAnswerNode` | rule-based grounded assembly from the ranked passages only, with numbered citation markers | `ranked_documents`, `search_query` | `grounded_answer`, `citations` |
| `OutputFormatNode` | compose the document: focus line, body, Sources list, advisory line | `grounded_answer`, `citations`, `query_filters` | `formatted_answer`, `status` |

### Data flow

```
user_input + input_context
  → PreProcessNode                        → validated_input, caller_request
  → KnowledgeSearchGraphNode.extract_input → inner invoke(validated_input)
                                             + caller_request stashed on the bridge
        → input_validate                  → search_query / query_filters
        → retrieve                        → retrieved_documents
        → rerank_filter                   → ranked_documents
        → generate_answer                 → grounded_answer / citations
        → output_format                   → formatted_answer
     get_output() → {formatted_answer, citations, status, ...}
  → KnowledgeSearchGraphNode.merge_output  → result, knowledge_base_answer, citations
  → PostProcessNode                        → formatted_output (gated)
```

## The caller-data contract

Caller data arrives on two channels and both are validated in `PreProcessNode`, which owns the
boundary. A field that breaks its bound is refused: the pipeline does not run, no answer is
assembled and no citations are attached. HOW that refusal is reported depends on whether the
caller can act on it — see "Two ways a request is refused" below. Either way the caller-facing
text names WHAT to correct and never echoes the VALUE; the field name stays in `error_log`, the
internal channel.

| Channel | Field | Contract |
|---|---|---|
| `input` | the query | plain text, or a JSON envelope `{"query", "category", "top_k"}`; control characters stripped, whitespace collapsed, 2000 characters maximum |
| `input_context` | `channel` | inert identifier, `[a-z0-9_]{1,32}` |
| `input_context` | `category` | inert identifier, `[a-z0-9_]{1,32}` — the corpus category filter |
| `input_context` | `top_k` | whole number, finite, 1–20 |
| `input_context` | `min_score` | number, finite, 0–1 — a caller may TIGHTEN the relevance floor, never lower it |
| `input_context` | `focus_labels` | list of engagement focus terms; entry-capped by `max_focus_labels`; each 60 characters or fewer of the label alphabet `[\w &.,()/+-]` |

Absent structured data degrades to the plain query against the declared defaults. There is no
path on which unvalidated caller data reaches the pipeline.

**Why the focus labels travel on the structured channel.** The framework's input gate masks
detected personal data on the query field, and its personal-name heuristic matches any two
consecutive capitalised words — which is the shape of a service line ("Change Management"), a
practice area ("Cloud Assessment") or a delivery phase ("Discovery Phase"). A label sent inside
the query arrives as a mask token and the ranking keys off the mask. Moving the labels to a
channel the framework does not scan is only half the fix; the other half is that this template
screens that channel itself.

**What the template's own screen does.** Every focus label is checked against the label alphabet,
then against `framework.security.pii_detector.detect_pii`, then against this template's
client-identifier patterns, then against an instruction-override screen. A label carrying a
contact identifier is REFUSED, not masked — a masked label would silently change which passages
rank. The screened detector types are `email`, `phone_jp`, `phone_us`, `ssn_us`, `credit_card`
and `my_number_jp`.

The `name` and `name_jp` heuristics are deliberately EXCLUDED from that screen. They match any
two Title Case words, which is precisely what a service line is, so screening on them would
refuse the ordinary labels this channel exists to carry — the same failure the channel was
introduced to avoid. Every other detector type is a shape no service line has and a personal
identifier that has no place in a knowledge-base answer, so a label carrying one is refused
outright.

### Two ways a request is refused

A refusal the caller can act on must not end the conversation. A value the caller can correct
ends the run as COMPLETED, carrying a reason code, and that reason is rendered as the response
body — so the caller fixes the value and sends the request again on the same conversation. A
refusal that is not a value to correct, and a contract break the caller is not party to, still
TERMINATES: the run ends with an error status and no output at all.

| Refusal | Reason code | Terminal status | Response body |
|---|---|---|---|
| empty, whitespace-only, missing or non-string query | `EMPTY_INPUT` | success | the fixed sentence for that code |
| query over the character limit | `QUESTION_TOO_LONG` | success | the fixed sentence for that code |
| a caller field that breaks its bound — `channel`, `category`, `top_k`, `min_score`, or any focus label, including one carrying a contact identifier, a client identifier, structural characters or override content, and the entry cap | `INVALID_REQUEST` | success | the fixed sentence for that code |
| an instruction-override payload on the QUERY channel | — | error | none |
| trust denial at a boundary node | — | error | none |
| output refused at a security gate — credential content or the document invariant | — | error | none |
| an inner-graph failure, re-raised as `SubgraphError` under `error_strategy = "propagate"` | — | error | none |

The reason codes travel in State as `error_code`; they are never part of the response envelope.
The sentences are the fixed strings in `src/services/failure_message.py` — each names what to
correct and nothing else, and none of them echoes the rejected value, an internal field path or a
gate message.

A completed-with-a-reason run also withholds every structured field: `get_output()` returns the
base envelope only, so no `citations` key is attached at all. A run that did not carry out the
request must not hand back something that reads like a result.

Once the reason is settled, the rest of the pipeline is skipped rather than re-deciding it: the
`main` slot returns the reason without invoking the inner workflow, and the output boundary
renders the sentence instead of gating an answer that was never assembled. Without that skip a
completed decline would go on to the retrieval path and overwrite the specific reason with a
vaguer one. On the terminating path the framework already does the same thing by itself —
`BaseNode.__call__()` skips `execute()` once the state carries an error status, which is why the
nodes after a terminating refusal never run.

**One asymmetry to be aware of.** An instruction-override payload terminates when it arrives on
the query channel, but is reported as a correctable field error when it arrives inside a focus
label, because the focus-label screen reports every verdict it reaches through the one field-error
path. Both channels refuse the payload and neither produces an answer; only the report differs.

## Runtime configuration

Two files, two jobs:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the static manifest — identity, category, entry point, trust level, declared requirements. Flat: every key at root level | the agent registry |
| `config/config.yaml` | every runtime parameter — `max_retry`, `timeout_s`, and the `retrieval` block (`top_k`, `score_threshold`, `kb_path`), plus `max_focus_labels` | the registry, which passes it as `Graph(config=...)` |

The standalone server reads the same file through `_runtime_config()` and constructs the agent
with it, so a registry-loaded agent and a deployed one run on identical settings.

From there a declared value reaches its consumer by one route each:

- `max_retry` — consumed by `AgentBaseGraph` for retry routing.
- the `retrieval` block — validated once in `declared_settings()` (type, finiteness, range),
  handed to `KnowledgeSearchGraphNode` at `register_nodes()`, forwarded by `_parent_config()`
  into the inner graph's constructor, and republished by `_extra_initial_state()` as the
  `retrieval_config` State field, which is where `RetrieveNode` and `RerankFilterNode` read it.
  `execute(self, state) -> dict` takes no config parameter, so State is the only plumbing route.
- `max_focus_labels` — seeded into the outer initial state as `runtime_settings`, where
  `PreProcessNode` reads the entry cap it enforces.

The module-level defaults in the node files are a FLOOR for a key the declaration omits, not a
mirror of the file. A declared value always wins, which is what makes the declaration observable:
`tests/proof_of_boundary/test_invoke_e2e.py` changes the declared result count on disk and reads
the change back out of the rendered answer. A reader pointing at the wrong file would return the
floor and that test would fail.

An out-of-contract declaration (NaN, Infinity, a string, out of range) is not forwarded and the
consumer keeps its floor. This matters for non-finite values in particular: NaN compares False
against every bound, so forwarding one would silently disable the filter it configures rather
than fail.

## State definition

| Field | Type | Purpose | Layer |
|-------|------|---------|-------|
| `validated_input` | `NotRequired[str]` | identifier-stripped query text | outer |
| `caller_request` | `NotRequired[Optional[str]]` (JSON) | the validated caller request | outer |
| `runtime_settings` | `NotRequired[Optional[str]]` (JSON) | validated runtime settings seeded for the boundary node | outer |
| `error_code` | `Optional[str]` | the reason a completed run did not carry out the request; internal only, never surfaced in the response envelope | both |
| `knowledge_base_answer` | `NotRequired[str]` | final answer, mapped from the inner `formatted_answer` | outer |
| `search_query` | `NotRequired[str]` | normalised search query | inner |
| `query_filters` | `NotRequired[Optional[str]]` (JSON) | `category`, `top_k`, `min_score`, `focus_labels` | inner |
| `retrieval_config` | `NotRequired[Optional[str]]` (JSON) | the declared retrieval settings | inner |
| `retrieved_documents` | `NotRequired[Optional[str]]` (JSON) | scored candidates | inner |
| `ranked_documents` | `NotRequired[Optional[str]]` (JSON) | reranked + filtered passages | inner |
| `grounded_answer` | `NotRequired[str]` | assembled answer body | inner |
| `citations` | `NotRequired[Optional[str]]` (JSON) | `[{ref, id, title, source}]` | inner |
| `formatted_answer` | `NotRequired[str]` | the rendered document | inner |
| `intake_notes` | `NotRequired[Optional[str]]` (JSON) | parse notes (no personal data) | inner |
| `trace_id` / `correlation_id` | `Optional[str]` | framework-managed tracing | both |

**Constraints:** flat `TypedDict` only; structured fields stored as JSON strings by every
producer AND consumer; domain fields `NotRequired`; `formatted_output` is not redeclared (it
stays framework-owned); no credentials, tokens or raw client identifiers in State; no Pydantic
models, dataclasses or arbitrary Python objects.

### The caller-data bridge

`GraphNode.execute()` invokes the inner graph as `subgraph.invoke(user_input, session_id=...,
ctx=...)` and does not forward the outer state's `input_context`. An inner read of
`state["input_context"]` would therefore always see `{}`. `src/graph/context_bridge.py` closes
that with a `ContextVar`: `extract_input()` stashes the validated request immediately before the
invoke, and the inner `_extra_initial_state()` reads it back. Only values `PreProcessNode` has
already validated are stashed — the bridge is a transport, never a second contract, and
`InputValidateNode` re-checks the shape of what arrives rather than trusting it. A `ContextVar`
keeps the hand-off correct per thread, so concurrent invocations cannot see each other's request.

## Security boundaries

- **Trust gate.** Every node declares `required_trust_level`. The two outer boundary nodes require
  `VERIFIED_EXTERNAL`; the inner domain nodes run at `ANONYMOUS`. The standalone server elevates
  an authenticated bearer caller to `VERIFIED_EXTERNAL` (`INVOKE_AUTH_TOKEN`) and never demotes a
  trust level established upstream.
- **Input screens.** `PreProcessNode` refuses instruction-override payloads on BOTH channels. The
  screen is deliberately narrow — each alternative requires the imperative override shape — so a
  genuine question using the same words ("How do I override the standard discovery timeline?") is
  unaffected; both directions are pinned in `tests/unit/test_caller_contract.py`. The template
  enforces this itself rather than relying on the platform gate: where that gate is absent or
  configured off, an unchecked payload would otherwise reach the retrieval path and return an
  answer. Neither channel produces an answer for such a payload; the two channels report the
  refusal differently — see "Two ways a request is refused".
- **Confidentiality screen.** Client and engagement reference codes, long reference numbers and
  e-mail addresses are redacted from the query before `validated_input` is written, and refused
  outright on the structured channel. Each pattern is guarded so it consumes the WHOLE identifier
  it fires on: a partial redaction destroys the token and leaves its structure and suffix
  readable, which is worse than none. The screen deliberately over-redacts rather than under —
  a standards reference of the same shape is redacted too, and for a confidentiality control that
  is the safe direction.
- **Output boundary.** `PostProcessNode` runs two independent layers from `execute()`, each with
  its own audit event, and fails closed on either: a credential scan that recurses into
  dict / list / tuple / set containers (a value nested one level down is exactly what a
  top-level-only scan misses), and the document invariant below. The same credential scan runs a
  second time in `get_output()` over the `citations` field, which nothing else inspects.
- **Secrets.** `requires.secrets` and `requires.extras` are both empty, derived from the code:
  this template calls no secrets provider and constructs no model client. `INVOKE_AUTH_TOKEN` is
  a deployment-level caller credential read from the environment in `src/api/server.py` only.
- **Audit.** Every node's `execute()` emits exactly one domain event on its success path:
  `pre_process_complete`, `input_validate_complete`, `retrieve_complete`,
  `rerank_filter_complete`, `generate_answer_complete`, `output_format_complete`,
  `post_process_complete`. The output boundary additionally emits `output_blocked_credential` or
  `output_blocked_document_invariant` when it refuses, and `post_process_degraded` in place of
  `post_process_complete` on a run that completed carrying a reason instead of an answer — so a
  declined run is still one auditable event at the boundary, carrying the reason code. The nodes
  such a run skips emit nothing, which is what a node that did no work should record. Nodes do not
  emit the lifecycle events — `BaseNode.__call__()` owns those.

## Output boundary — the invariant this template enforces

The published contract is that **the rendered document's structure is this template's own**.
`_enforce_document_invariant()` checks it, line-anchored, and refuses the whole answer on any
violation:

| Clause | Rule |
|---|---|
| `title_heading` | exactly one line that is the title heading |
| `sources_heading` | exactly one line that is the Sources heading |
| `stray_heading` | no other heading line at any level — caller text that became a section would appear here |
| `source_ref` | every `- [n]` source line carries a ref the pipeline emitted |
| `disclaimer` | the standing advisory line is present |

Caller text reaches the document only as single-line quoted fragments: the query is
whitespace-collapsed before it is echoed and focus labels are alphabet-locked at the boundary. So
in normal operation the invariant is satisfied by construction, and the check is the independent
backstop that catches a renderer regression or an escape from the render path. The clauses are
line-anchored on purpose — a question that merely MENTIONS `## Sources` is an ordinary thing to
ask about a document, and refusing to answer it would be the screen blocking real work.

**No monetary rounding grid applies here.** Agents that emit external financial reports round
every rendered aggregate to a fixed grid at this boundary. This template renders no monetary
aggregate: the answer is quoted corpus text, citation markers and the caller's own echoed query.
A numeric snap would be actively wrong for it — passages are quoted from the corpus, and
rewriting a figure inside a quoted passage would falsify the guidance the answer is grounded in,
breaking the grounding property the template exists to provide. Both boundary layers are
therefore pure SCANS: they return a verdict and never rewrite the document. That also means their
order carries no risk, since neither can destroy evidence the other needs.
`tests/proof_of_boundary/test_invoke_e2e.py` pins the consequence — decimals, percentages,
ratios, grouped amounts and inert identifiers all arrive byte-identical through a real invoke.

## Advisory line

Every answer carries the standing advisory line (informational only; summarises internal
methodology, guidance and benchmark material; does not replace engagement-specific analysis or
client-specific legal, tax or regulatory advice). It is appended by `OutputFormatNode` as part of
the domain output contract — not injected by `post_process`, which only gates. Its presence is a
clause of the output invariant, so an answer that lost it is refused rather than shipped.

## Human-in-the-loop

`hitl.enabled` stays `false`. No externally-impactful action exists: the agent performs read-only
corpus search and answer assembly, with no filing, no write to any external system and no
autonomous action, so no pre-action approval gate applies.

## Answer synthesis

Answer assembly is deterministic: `GenerateAnswerNode` builds the body rule-based from the ranked
passages (a lead sentence plus one cited point per passage). There is no model call and no model
client dependency, which is why `generation_mode` is `deterministic` and `requires.extras` is
empty. The upgrade seam is documented in `config/prompts/answer_synthesis_prompt.md`: a later
node swaps the assembly for a model call over the same `ranked_documents` input and emits the
same `grounded_answer` / `citations` contract, so no other node changes.

## Composition

- Pattern: `GraphNode` (subgraph) in the outer `main` slot.
- Composition target: `DomainWorkflowGraph` (inner `BaseGraph`).
- Error propagation: `propagate` — inner errors re-raised as `SubgraphError`, surfaced by the
  framework as an error status rather than an exception to the caller.

## Import isolation

- [x] No platform-internal SDK imports.
- [x] Import targets: `framework/` and `shared/` only.
- [x] No pre-framework base-class names in any base position.

## Design decisions

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | `AgentBaseGraph` | `AutonomousBaseGraph` | **`AgentBaseGraph`** | fixed multi-step retrieval workflow, no autonomous loop |
| Composition | single `main` node | `GraphNode` → inner `BaseGraph` | **nested** | a five-step workflow exceeds one slot; nesting keeps the outer backbone untouched |
| Config into domain nodes | ctor injection | State seeding | **State seeding** | `execute(self, state) -> dict` carries no config parameter; the inner graph already republishes settings into State per invocation |
| Caller structured data | inside the query string | the caller-data channel | **the channel** | the query field is masked by the framework's input gate, and a service line is exactly the shape it masks |
| Answer synthesis | rule-based assembly | model call | **rule-based** | deterministic and testable offline; the model seam is documented |
| Corpus | external vector store | seeded JSON corpus | **seeded JSON** | self-contained and deterministic; the retrieval contract is store-agnostic for a later swap |
| Output invariant | monetary rounding grid | document-structure invariant | **structure invariant** | nothing monetary is rendered, and rewriting figures would falsify quoted guidance |
