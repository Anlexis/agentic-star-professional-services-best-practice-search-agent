# Answer Synthesis Prompt — SVC-C2-005 (v2 LLM upgrade seam)

> **v1 does NOT use this prompt at runtime.** v1 of `GenerateAnswerNode` is
> deterministic (rule-based grounded assembly over `ranked_documents`); no
> node reads this file. It documents the synthesis contract for the v2 LLM
> upgrade described in `docs/02_design.md` ("v1 Implementation Note — LLM
> synthesis"), so the v2 swap changes only the inside of
> `GenerateAnswerNode.execute()`.

## Contract (v2 GenerateAnswerNode)

- **Input:** the same `ranked_documents` JSON (id / title / category / source /
  score / excerpt) and `search_query` the v1 node reads.
- **Output:** the same state contract — `grounded_answer` (str, with numbered
  `[n]` citation markers) and `citations` (JSON list of
  `{ref, id, title, source}`).
- **Grounding rule:** every factual statement in the answer must be traceable
  to one of the supplied passages via a `[n]` marker; content not present in
  the passages must not be asserted.
- **No-coverage rule:** when no passage supports the question, say so and
  recommend refining the query or escalating to the knowledge-management team
  — never answer from parametric knowledge.
- **Tone:** neutral, advisory-appropriate, no client-specific legal, tax, or
  regulatory advice (the advisory disclaimer is appended downstream by
  `OutputFormatNode`).

## Prompt template

```
You answer professional-services methodology, guidance, benchmark, and
past-engagement questions strictly from the knowledge-base passages provided
below.

Question:
{search_query}

Passages (each with a reference number):
{ranked_documents}

Rules:
1. Use ONLY the passages above. If they do not answer the question, say the
   knowledge base has insufficient coverage and stop.
2. Mark every factual statement with the [n] reference of its passage.
3. Do not give client-specific legal, tax, or regulatory advice.
4. Keep the answer under 300 words.
```

## Manifest coupling

The `llm` block in `config/agent.yaml` (`temperature`, `max_tokens`) is already
forwarded to the inner graph via
`KnowledgeSearchGraphNode._parent_config()` under
`config["configurable"]["llm"]`; the v2 node reads it from there.
