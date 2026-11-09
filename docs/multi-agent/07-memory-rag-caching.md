# 07 — Memory, RAG & Caching

## 1. Memory types

| Type | Scope | Store | Example | Lifetime |
|---|---|---|---|---|
| **Working state** | One graph run | LangGraph state | Plan, agent results | Request |
| **Thread memory** | One conversation | Checkpointer (`AsyncPostgresSaver`) | "And what about tomorrow?" refers to the previous question | 30 days TTL |
| **Long-term user memory** | User × household | LangGraph `Store` (`AsyncPostgresStore` + pgvector index) | "Prefers Hindi", "runs washing machine on weekends", "asked about adding a battery 3 times" | Until deleted by user |
| **Household facts** | Household | OLTP (onboarding tables) | Panels, inverter, tariff | Source of truth, never copied into memory |
| **Episodic** | Household | Store, namespace `episodes` | "Outage on 2026-10-12, 3 h, battery covered 2.4 h" | 1 year |
| **Procedural** | Global | Prompt/policy registry | Prompt versions, rules | Versioned |

### Rules for memory

- Memory stores **preferences and episodes**, not numbers that live in telemetry/onboarding. Numbers are always re-read.
- Writes happen **after** the answer (in `finalize`, async) through a small `extract` call or deterministic rules.
  Never block the response on memory writes.
- Memory is masked (no PII) and user-visible: a "What Suryaa remembers" page with delete. This also covers the
  DPDP right to erasure ([09](./09-security-compliance.md)).
- Namespaces: `("user", user_id, "prefs")`, `("household", household_id, "episodes")`. The namespace is built from
  runtime context, never from model output.

### Thread memory trimming

`load_memory` keeps the last k turns (e.g. 6) plus a rolling summary (generated every N turns with the `router`
model). Older tool payloads are never kept in messages, only final answers. This keeps tokens flat as conversations grow.

## 2. RAG upgrades (Knowledge Agent)

Today: hashing embedder (256-dim), hybrid cosine + lexical, boosts, MMR, owner/shared scope filter. A solid base.

| Upgrade | Why | Priority |
|---|---|---|
| Real embedding model (multilingual) | Hashing has no semantics; Hindi/Marathi questions vs English manuals need a multilingual embedder | **P0** |
| HNSW index on `embedding_vec` + `ef_search` tuning | Sub-linear ANN at scale | P0 |
| True BM25 (Postgres full-text `tsvector` or ParadeDB `pg_search`) fused with vector via RRF | Fault codes ("E04"), model numbers and tariff clause ids are lexical | P0 |
| Structure-aware chunking (headings, tables, fault-code tables as rows) | Manuals are tabular | P1 |
| Contextual chunk headers ("Growatt SPF 5000 manual › Fault codes › E04") prepended before embedding | Better recall for short chunks | P1 |
| Reranker (cross-encoder) top-30 → top-5 | Precision | P1 |
| Metadata routing (brand, model, DISCOM, effective date) from household profile | The user's inverter brand is known, so filter first | P0 (cheap, high value) |
| Tariff orders as **structured tables** in the Tariff Engine, RAG only for explanations | Numbers must be computed, not retrieved | P0 |
| Freshness: `effective_date`, supersession links between tariff orders | Avoid quoting an old tariff | P1 |
| Ingest pipeline as async job with PII masking + injection scan | See [04](./04-guardrails-gates-masking.md) | P0 |
| Embedding version column + background re-embed | Model upgrades without downtime (already filtered by `embedding_model`) | Done ✅ |

## 3. Caching layers

| Cache | Key | TTL | Hit means |
|---|---|---|---|
| **Single-flight request memo** | Tool + args within request | Request | One live read serves 5 tools (exists ✅) |
| **Live snapshot** | `volta:live:<home>` (Redis) | Until next tick | Tools never hit Timescale for "now" (exists ✅) |
| **Classification cache** | Normalised question hash | 1 h | Skip router LLM |
| **Deterministic node cache** | `(node, inputs_hash)` | Short | e.g. forecast for the same home/hour |
| **Retrieval cache** | `(query_norm, filters, embed_model)` | 1 day, bust on doc publish | Skip embedding + ANN |
| **Embedding cache** | `sha256(text) + model` | Long | Skip re-embedding identical chunks/queries |
| **Provider prompt cache** | Static system prefix ≥ provider minimum | Provider-managed | Cheaper input tokens. Only worth it once prompts exceed the 1k-token minimum (see `ai_implementation_report.md` §14) |
| **Semantic answer cache** | ⚠️ Only for **static** intents (document Q&A, home_profile, "how does net metering work") | 1 day | Skip LLM entirely |

Never semantic-cache answers that depend on live telemetry. "Should I run the geyser?" has a different correct
answer every few minutes. The cache key must include the intent class, and live intents are excluded by policy.

## 4. Context engineering checklist (per agent)

- Facts are **projected** per intent (existing `projection.py`) and **budgeted** (`fit_to_budget`).
- Static system prompt core first (cacheable), per-intent guides appended.
- Retrieved chunks are capped by token count, not just by k.
- Each agent sees only its `AgentCall`, not the full conversation, unless it declares `needs_history=True`.
- The synthesizer sees `AgentResult.facts` and drafts, never raw tool payloads.
