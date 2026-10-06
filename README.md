# Professional Services Best-Practice Search Agent

AI agent for searching professional services knowledge and best practices, built with Agentic Star.

> **Category**: Cat 2 (a domain pipeline: a retrieval workflow behind the fixed agent backbone)
> **Industry**: Services
> **Template ID**: SVC-C2-005

## Overview

Answers a consultant's question from an internal professional-services knowledge base —
methodology, regulatory and industry guidance, benchmark material and past-engagement insight —
quoting only the passages that clear a relevance threshold and citing each one back to its source
entry. When no passage clears the threshold the agent says so rather than composing an answer
anyway, and every answer carries a standing advisory line.

A caller may also submit the engagement's own focus labels — service line, practice area, delivery
phase — alongside the question. Those terms take part in retrieval and ranking, and the answer
states which focus was applied. They travel on a separate structured channel for a concrete reason:
a service line is written as consecutive capitalised words, which is the shape the platform's input
screen masks on the question field, so a label sent inside the question would arrive as a mask token
and the ranking would key off the mask. The structured channel is screened by this template instead,
and a label carrying a contact or client identifier is refused rather than masked.

Retrieval and answer composition are deterministic and offline: the shipped corpus is scored by
content-term overlap and the answer is assembled from the retrieved text, so the same question
always returns the same answer. Swapping in a vector store and a constrained model is a
configuration change, not a rewrite.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, graphs, schemas)
tests/        unit and boundary tests
config/       agent manifest, runtime parameters, seeded knowledge base, prompts
docs/         design and test documentation
```

See `docs/02_design.md` for the architecture and the security boundaries, and
`docs/03_test_spec.md` for what the suite covers.

## Customising

1. Replace `config/kb/professional_services_kb.json` with your own corpus. Each entry carries
   `id`, `title`, `category`, `source`, `tags` and `content`; nothing else in the pipeline needs
   to change.
2. Tune `config/config.yaml` — the result count, the relevance threshold, the corpus path and the
   cap on how many focus labels a caller may submit. Those values are read at start-up and reach
   the pipeline; the module defaults are only a floor for a key you leave out.
3. Adapt the advisory line in `src/nodes/output_format_node.py` to your own disclosure duty, and
   the client-identifier patterns in `src/nodes/pre_process_node.py` to your own reference formats.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
