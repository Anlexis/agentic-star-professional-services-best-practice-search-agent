# Test Specification — SVC-C2-005

## Strategy

- Node-level unit tests for all 7 nodes, graph composition for both layers, the caller-data
  contract, and end-to-end tests through the real ASGI entry point.
- Test types: unit (`tests/unit/`) and boundary (`tests/proof_of_boundary/`).
- Every test is deterministic — no model call, no network beyond the in-process ASGI transport.
- Screens are probed in BOTH directions. A screen that only ever fires is as broken as one that
  never does: it blocks real work. Where a screen exists, the suite pins a hostile input that is
  refused AND an ordinary domain input that is not.

## Framework compliance

| TC-ID | Test | Expected result | Result |
|-------|------|-----------------|--------|
| TC-01 | State contract: flat TypedDict | no Pydantic / dataclass | Pass |
| TC-02 | invalid input is refused | nothing published either way: a value the caller can correct completes carrying a reason code, an instruction-override query ends with an error status | Pass — `tests/unit/test_pre_process_node.py`, `tests/unit/test_caller_contract.py` |
| TC-03 | no credentials in State | credential scan: 0 violations | Pass |
| TC-04 | `InvocationContext` via config only | direct access raises | Pass |
| TC-05 | no duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from every `execute()` body | 0 duplicates |
| TC-06 | the default input gate is not overridden | `TypeError` at class definition if overridden (`@final`) | 0 overrides — `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | the default output gate is not overridden | `TypeError` at class definition if overridden (`@final`) | 0 overrides — `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` enforced | insufficient trust → refused | Pass — `tests/unit/test_trust_gate.py` |
| TC-09 | domain input checks | this template screens the caller contract in `PreProcessNode.execute()` rather than through an extension hook | Pass — `tests/unit/test_caller_contract.py` |
| TC-10 | domain output checks | the output boundary is the module-level credential scan + document invariant, called from `execute()` | Pass — `tests/unit/test_post_process_node.py` |
| TC-11 | at least one domain `emit_trace_event()` per `execute()` | domain event emitted on every success path | ≥1 per node — 7/7 |

## Boundary tests

| PB-ID | Boundary | Test | Result |
|-------|----------|------|--------|
| PB-1 | node → event emitter | `emit_trace_event()` fires on every invocation path | Pass |
| PB-2 | State serialization | post-invoke State is primitives only — `test_state_safety.py` | Pass |
| PB-3 | external service | N/A — no live external service; the corpus is a shipped file | N/A |
| PB-4 | import isolation | no platform-internal imports; AST scan — `test_import_isolation.py` | Pass |
| PB-5 | checkpoint safety | no tokens or Pydantic objects in a checkpoint — `test_state_safety.py` | Pass |
| PB-6 | invoke execution order | `__call__()`: trust gate → `node_start` → input gate → `execute()` → output gate → `node_complete` — `test_pb_invoke_order.py` | Pass |
| PB-7 | interrupt propagation *(conditional)* | no graph class declares `propagate_hitl=True` — auto-waived, `test_pb7_hitl_interrupt_propagation.py` | Auto-waived |
| PB-BOOT | server entry point boots | `import src.api.server` does not raise; the module-level agent is compiled — `test_server_boot.py` | Pass |
| PB-E2E | the public path | the real ASGI `/invoke` with bearer auth — `test_invoke_e2e.py` | Pass |

PB-1 through PB-6 are mandatory. PB-7 is auto-waived for this non-interactive template.

## End-to-end (`tests/proof_of_boundary/test_invoke_e2e.py`)

Everything here runs against the compiled agent behind `/invoke`, because that is the surface a
caller reaches. Two of these properties are invisible from a node-level test: the framework does
not forward the caller-data channel into a subgraph, and a runtime setting read from the wrong
file degrades silently to a default that happens to match the declaration.

| Area | What is proven |
|---|---|
| auth boundary | no bearer token → 401, and the query is not echoed |
| real work | a corpus-grounded answer with citations, and an explicit no-coverage answer for an out-of-domain question rather than an invented one |
| declared config is live | the declared result count is changed on disk and the change is read back out of the rendered answer (1 → 1, 2 → 2, 4 → 4) |
| caller data changes the answer | a focus label retrieves a passage the query channel would have masked; the category filter narrows the answer; a caller may tighten the result count but not widen it past the declaration |
| validation rejection | 12 parametrized out-of-contract fields each complete with no answer and no citations, the response body being the fixed correct-the-value sentence rather than a result; an oversized channel is refused at the adapter with 413; an instruction-override query ends with an error status and no output |
| output contract | the rendered document carries only this template's own headings; caller text cannot open a section or forge a source line; numbers and identifiers arrive byte-identical; client identifiers are redacted whole |

## Caller-data contract (`tests/unit/test_caller_contract.py`)

`execute()` is called DIRECTLY in the refusal tests. The framework's input gate refuses
high-confidence payloads too, but this template must not depend on that: where the platform gate
is absent or configured off, an unchecked payload would reach the retrieval path and an answer
would come back. Calling `execute()` with no wrapper in front proves the refusal belongs to this
node. Assertions are behavioural — nothing carried forward, the field named in the internal log
and the value never echoed, and the terminal status that matches the kind of refusal: a value the
caller can correct completes carrying a reason code, an instruction-override query ends with an
error status. Never the wording of any gate.

| § | Coverage |
|---|---|
| PRE-01..04 | baseline request accepted; absent channel degrades; empty and over-long input refused — each completing with a reason code and publishing nothing |
| PRE-05..08 | the non-finite matrix per numeric field — `"NaN"`, `"Infinity"`, `"-Infinity"`, raw `float("nan")`, raw `float("inf")`, bools, strings, out-of-range and fractional values — each refused, and in-range values accepted |
| PRE-09..10 | `channel` and `category` locked to an inert identifier |
| PRE-11..14 | focus labels: ordinary service lines (English and Japanese) accepted; contact identifiers refused BY THE PII SCREEN and client identifiers BY THE CLIENT SCREEN — each case asserts the label alphabet accepts it first, so a screen the alphabet already pre-empts cannot pass as load-bearing; structural characters and the entry cap refused |
| override screen | 5 override payloads refused on the query, each ending with an error status; the same payload refused on the structured channel, where the focus-label screen reports it as a correctable field error and the run completes with a reason code; 5 ordinary questions using the same words unaffected |
| confidentiality | 5 identifier shapes redacted WHOLE (nothing left in the clear); 8 ordinary tokens — decimals, ratios, percentages, inert codes — byte-identical |

## Node-level unit tests

| § | File | Node under test |
|---|------|-----------------|
| 2.1 | `test_pre_process_node.py` | `PreProcessNode` |
| 2.2 | `test_input_validate_node.py` | `InputValidateNode` (VAL-01..VAL-09) |
| 2.3 | `test_retrieve_node.py` | `RetrieveNode` (RET-01..RET-08) |
| 2.4 | `test_rerank_filter_node.py` | `RerankFilterNode` (RRF-01..RRF-08) |
| 2.5 | `test_generate_answer_node.py` | `GenerateAnswerNode` (GEN-01..GEN-05) |
| 2.6 | `test_output_format_node.py` | `OutputFormatNode` (FMT-01..FMT-05) |
| 2.7 | `test_post_process_node.py` | `PostProcessNode` (POST-01..POST-08) — credential layer, document invariant both directions, and the numbers-are-never-rewritten property |
| 2.8 | `test_config_manifest.py` | manifest ↔ code and declaration ↔ forwarded settings (CFG-01..CFG-08) |
| 2.9 | `test_retrieval_quality.py` | golden-query retrieval precision (QUAL-01..QUAL-10) |
| 2.10 | `test_caller_contract.py` | the caller-data contract (PRE-01..PRE-14) |

## Graph composition

| § | File | Coverage |
|---|------|----------|
| INT-01..04 | `test_domain_workflow_graph.py` | inner graph construction, settings forwarding, the caller-data bridge, output shape, full inner invoke |
| INT-05..12 | `test_graph_composition.py` | outer graph construction, the `GraphNode` contracts, `extract_input()` stashing the validated request, the settings floor with no config file, full end-to-end invoke (success, no coverage, anonymous denial) |
| INT-13 | `test_graph_composition.py` | `get_output()`'s second credential pass over `citations`: clean citations surfaced on success; a credential-shaped value nested inside one citation flips the status to error and withholds citations entirely even though the top-level answer is clean; a non-success state never attaches citations |

## Business logic

| TC-ID | Test | Input | Expected result | Result |
|-------|------|-------|-----------------|--------|
| BL-01 | golden-query retrieval precision | 10 domain queries, one per corpus category (`test_retrieval_quality.py`) | each query's top result is the expected corpus entry | Pass |
| BL-02 | no-coverage fallback | an out-of-domain query | an explicit no-coverage answer, never a fabricated one | Pass |

## Execution summary

- `tests/` (unit + boundary, full tree): **264 passed, 1 skipped**, zero warnings from this
  template's own code.
- The single skip is the auto-waived interrupt-propagation stub (no graph class declares
  `propagate_hitl=True`).
- Verified against the real `agenticstar-agentcore==1.0.1` wheel — the version the pipeline
  installs — not a local stub.
