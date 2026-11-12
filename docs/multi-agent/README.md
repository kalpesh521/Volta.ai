# Volta.ai — Multi-Agent System Architecture

Target architecture for moving Suryaa from one assistant graph to a production, enterprise-grade
**multi-agent system on LangGraph**, with a **Go performance plane** for the hot, non-LLM paths.

> Status: design (2026-11-08). Builds on what already exists in `backend/app/modules/assistant`
> (deterministic Q&A graph, RAG, confirm-before-act actions, alerts, evals). Nothing here throws
> that work away — every agent below wraps or extends a module that is already in the repo.

## Read in this order

| # | Document | Answers |
|---|---|---|
| 01 | [Architecture overview](./01-architecture-overview.md) | The target system on one page: planes, layers, request paths, principles |
| 02 | [Agent catalog](./02-agent-catalog.md) | Every agent and sub-agent: job, tools, model tier, autonomy level, I/O contract |
| 03 | [LangGraph design](./03-langgraph-design.md) | Supervisor + subgraphs, state, `Send` fan-out, checkpoints, interrupts, streaming, code skeletons |
| 04 | [Guardrails, gates & masking](./04-guardrails-gates-masking.md) | Input/output guardrails, the 10 gates, PII masking vault, injection defence |
| 05 | [Evals](./05-evals.md) | Offline, online, trajectory, safety, LLM-as-judge, CI release gates |
| 06 | [Go performance plane](./06-golang-performance-plane.md) | **Future option, not in the current roadmap.** Where Go would beat FastAPI, and service contracts if you ever add it |
| 07 | [Memory, RAG & caching](./07-memory-rag-caching.md) | Thread memory, long-term store, retrieval upgrades, semantic cache |
| 08 | [Observability & LLMOps](./08-observability-llmops.md) | Tracing, metrics, SLOs, cost, prompt registry, model routing |
| 09 | [Security & compliance](./09-security-compliance.md) | Tenant isolation, tool authZ, audit, secrets, DPDP Act 2023 |
| 10 | [Deployment & scaling](./10-deployment-scaling.md) | Kubernetes topology, autoscaling, resilience, DR |
| 11 | [Roadmap & suggestions](./11-roadmap-suggestions.md) | Phased plan from today's code, plus new agent ideas |
| 12 | [Phase 0 implementation](./12-phase0-implementation.md) | What was built in Phase 0, how to configure and operate it, known limits |
| 13 | [Progress tracker](./13-progress-tracker.md) | Status of every phase and item, what each delivered, open follow-ups, decision log |
| 14 | [Phase 1 implementation](./14-phase1-implementation.md) | The supervisor, three agents, thread memory, regeneration, streaming and trajectory evals: how they work, configure and operate, known limits |
| 15 | [Phase 2 implementation](./15-phase2-implementation.md) | Input guard, PII masking and vault, token budgets, policy checks, kill switches and the prompt and model registry; plus the Phase 1 wrap-up (the `actions/` move, conversation lock, real-model parity with a capped spend) |

## The five rules that run through every document

1. **LLM explains, code calculates, code decides.** Numbers come from analytics, actions are approved by
   deterministic safety checks, and the LLM only writes the explanation.
2. **Deterministic first, LLM second.** Rules route most traffic for 0 tokens. An LLM call has to be justified.
3. **Every boundary is a gate.** Input, intent, data, budget, tool, action, human, output and release
   each have an explicit pass/fail check that is logged.
4. **Python for reasoning, Go for throughput.** LangGraph orchestration stays in Python. Per-tick, per-connection
   and per-byte work (ingest, fan-out, guard fast-path, gateway, command executor) moves to Go.
5. **Nothing ships without an eval.** Prompt, model, tool and graph changes are blocked by the eval gate in CI.
