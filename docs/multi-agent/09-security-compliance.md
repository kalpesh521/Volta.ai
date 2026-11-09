# 09 — Security & Compliance

## 1. Threat model (AI-specific, short)

| Threat | Vector | Primary control | Defence in depth |
|---|---|---|---|
| Cross-tenant data access | Prompt asks for another home; tool arg smuggling | `household_id` bound from runtime context, never a tool arg (exists ✅) | Row-level checks in gateways, Postgres RLS on knowledge, red-team cases |
| Unsafe physical action | Injection, social engineering ("SOC is 90 %, trust me") | Deterministic `safety.py` with **facts from telemetry only** + human confirmation | Go executor re-check, kill switch, per-device rate limit |
| Indirect injection | Uploaded manuals/bills, shared docs | Docs treated as data; Knowledge Agent has no action capability | Ingest scan, quarantine, citations required |
| Data exfiltration through the LLM | Model echoes PII or secrets | PII never enters prompts (vault) | Output PII check, log redaction |
| Model/provider data retention | Prompts stored by vendor | Masked prompts + zero-retention / no-training settings on provider accounts | Regional endpoints where available |
| Denial of wallet | Scripted long prompts, loops | Budgets at edge (tokens/day), recursion limits, max agents | Abuse cool-down, anomaly alert on cost |
| Supply chain | Malicious package, model, or MCP server | Pinned deps + hashes, SBOM, image signing | Network egress allow-list for agent pods |
| Replay/forged confirmations | Re-sending confirm request | Single-use proposals, TTL, ownership check on resume | Idempotent executor |

## 2. Identity and authorization across agents

- The **user** is the principal for every agent and tool call. Agents do not have their own broader permissions.
- Tool authZ is explicit: each agent declares `allowed_tools`, and the tool wrapper checks `(agent, tool, scope)`
  against policy (G5). Write-capable tools (`propose_action`, `create_rule`, `create_ticket`) exist only in the
  agents that need them.
- Service-to-service: mTLS (service mesh or SPIFFE IDs). The agent service accepts identity headers **only**
  from the edge.
- Internal ingest uses `INGEST_TOKEN` today. In production, replace it with mTLS or short-lived tokens per producer.

## 3. Secrets

- Provider keys live in a secret manager (Vault / cloud KMS-backed secrets), injected at runtime, rotated. Today
  they are `SecretStr` from `.env`, which is fine for dev only.
- If you adopt the LLM gateway, only the gateway holds provider keys; agent pods do not.
- Remove the commented credentials at the bottom of the root `.env.example` (a sample name/email/password is
  committed there) and rotate that password if it is real.

## 4. Audit trail

| Event | Stored | Retention |
|---|---|---|
| Every action proposal, check result, confirmation, publish, executor transition | Append-only `action_audit` (Postgres), hash-chained rows | ≥ 1 year |
| Rule creation/activation/deactivation | `rule_audit` | ≥ 1 year |
| Gate rejections with reason | Logs + metrics | 90 days |
| Prompt/model version per answer | Trace + response `meta` | 30–90 days |
| Admin/ops changes (flags, policies) | Git + flag service audit log | Permanent |

## 5. Data protection — India DPDP Act 2023 (and DPDP Rules)

Volta serves Indian households (DISCOMs, INR tariffs), so plan for the Digital Personal Data Protection Act.
Confirm specifics with legal counsel. This is an engineering checklist, not legal advice.

| Obligation (summary) | Engineering control |
|---|---|
| Notice & consent for processing, including AI features | Consent record per purpose (assistant, memory, bill analysis, notifications) with versioned notice text |
| Purpose limitation & data minimisation | Projection + PII vault. LLM sees energy facts only |
| Right to access / correction / erasure | "What Suryaa remembers" page; erasure job deletes Store memories, checkpoints, uploaded docs, embeddings for that user |
| Cross-border transfer | Know where each LLM provider processes data. Prefer India/approved regions or self-hosted models for sensitive paths |
| Security safeguards & breach notification | Encryption at rest/in transit, audit, incident runbook with notification timelines |
| Retention limits | TTLs on checkpoints, logs, traces, vault (minutes), uploaded bills (configurable) |

## 6. Hardening checklist

- [ ] `MemorySaver` replaced by Postgres checkpointer; proposals single-use with TTL
- [ ] Rate limiting backed by Redis (today in-memory `slowapi`)
- [ ] Postgres RLS on knowledge tables keyed by `owner_user_id`, in addition to the SQL predicate
- [ ] Egress allow-list: agent pods can reach only the LLM gateway/providers, Postgres, Redis, queue
- [ ] Provider accounts set to zero data retention / no training where offered
- [ ] Container images: non-root, read-only FS, signed, scanned
- [ ] Red-team suite in CI (hard gate)
- [ ] Secrets out of `.env` in production
