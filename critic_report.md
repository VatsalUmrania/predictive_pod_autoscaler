# Critic Review Report (Stage 7)

**Document Audited:** `docs/designs/nexus_autonomous_architecture.md` (FeatureRange: lines 1–335)  
**Standard Applied:** [Feature Specification Standard](/home/vatsal/.gemini/config/plugins/egm/resources/document_standards/feature-spec-standard.md)  
**Reviewers:** Hostile Design Critic (`egm-design`), Structural Layout Validator (`egm-structural`), Technical Copy Editor (`egm-style`)  
**Date:** 2026-11-13  
**Status:** REVISE (Blockers Found)

---

## Executive Summary

The Critic Review swarm stress-tested the feature specification across architectural robustness, document structure, and technical prose style. While the core agentic vision and zero-trust metadata model are sound, the document fails the Feature Specification Standard due to missing mandatory sections (`## Alternatives Considered`, `## Risks & Mitigations`, `## Non-Functional Requirements`), structural sequence inversion, and a critical governance vulnerability where the fallback path completely bypasses OPA policy evaluation.

---

## Consolidated Findings Grouped by Severity

### Blocker Findings

#### 1. Missing Mandatory "Alternatives Considered" Section (`egm-design`, `egm-structural`)
* **Affected Scope:** Feature Specification Root
* **Discrepancy:** The Feature Specification Standard ("The Alternatives Rule") mandates proving that other viable designs were evaluated and rejected with specific, documented trade-offs. The document presents choices (in-process Python tools, LangGraph ReAct, decoupled OPA) as foregone conclusions without documenting evaluated alternatives.
* **Remediation:** Add `## Alternatives Considered` covering:
  1. *In-Process Python Tool Adapters vs. External MCP Daemon Sidecars / Proxies*.
  2. *Hypothesis-Driven ReAct Loop vs. Static Directed Acyclic Graphs (DAGs)*.
  3. *Decoupled OPA Rego Engine vs. Python-Native Policy Rules (e.g., Pydantic / Casbin)*.

#### 2. Missing Dedicated "Risks & Mitigations" Section (`egm-design`)
* **Affected Scope:** Feature Specification Root
* **Discrepancy:** The standard requires explicitly documenting constraints, failure modes, security implications, and scalability limits. Structured assessments of prompt injection via telemetry logs, runaway agent loops, concurrent cross-incident race conditions, and blast radius containment failures are missing.
* **Remediation:** Add a formal `## Risks & Mitigations` section explicitly enumerating:
  1. *Security Risks*: Indirect prompt injection in telemetry logs, credential boundaries, and unauthenticated tool execution.
  2. *Operational & Scalability Risks*: Telemetry storms, API quota exhaustion, and partial rollback failure states.
  3. *Concrete Mitigations* for each identified risk vector.

#### 3. Deterministic Fallback Bypasses Zero-Trust OPA Governance (`egm-design`)
* **Affected Scope:** Lines 16, 218, 277 (Overview, CUJ 2, Architecture Flowchart)
* **Discrepancy:** Line 16 and the flowchart state that deterministic fallback completely bypasses OPA. Bypassing OPA during an outage creates a critical safety flaw: active circuit breakers (`circuit_breaker_open == true`), cooldowns, and environment freezes are ignored when the system degrades. Degraded mode must never degrade safety guardrails.
* **Remediation:** Route the deterministic fallback path through `govern_node` / OPA before reaching `execute_node`, or mandate that `action_ladder.py` enforce identical circuit breaker and cooldown policies.

---

### Major Findings

#### 4. Structural Layout Inversion (`egm-structural`)
* **Affected Scope:** Lines 52–196 (`Technical Specifications & Data Contracts`) & Lines 198–230 (`Critical User Journeys`)
* **Discrepancy:** The standard template sequence is: `Overview` -> `The Problem` -> `Goals` -> `Non-Goals` -> `Critical User Journeys` -> `Technical Plan`. In the current draft, data contracts are promoted to a top-level H2 *before* CUJs, sandwiching user journeys between disjoint technical sections.
* **Remediation:** Relocate data contracts and schemas into `## Technical Plan (Architecture)`, positioning `## Critical User Journeys (CUJs)` directly after `## Non-Goals`.

#### 5. Missing Mandatory "Non-Functional Requirements" Section (`egm-structural`)
* **Affected Scope:** Feature Specification Root
* **Discrepancy:** The template standard mandates `## Non-Functional Requirements` defining operational SLAs, latency budgets, token consumption ceilings, and audit logging guarantees.
* **Remediation:** Add `## Non-Functional Requirements` covering Security & Privacy, Performance & Latency Budgets, Resilience & Reliability, and Auditability.

#### 6. Governance Bypass on Operator Plan Edits (`egm-design`)
* **Affected Scope:** Line 285 (`Flowchart: Approval Node`)
* **Discrepancy:** The flowchart routes `Approval -- "Approve / Edit" --> Execute`. If an operator edits tool arguments or substitutes actions during Slack HITL review, routing directly to `Execute` bypasses OPA policy validation, potentially introducing uncontained blast radiuses.
* **Remediation:** Decouple "Approve" from "Edit". Route operator edits back to `govern_node` for OPA re-evaluation:
  - `Approval -- "Approve" --> Execute`
  - `Approval -- "Edit" --> Govern`

#### 7. Latency Budget Contradiction & Log Truncation Policy (`egm-design`)
* **Affected Scope:** Lines 37, 205, 374 (`Goals`, `CUJ 1`, `diagnose_node`)
* **Discrepancy:** 8 sequential ReAct turns with LLM generation (2–4s) and infrastructure API queries (1–3s) can easily exceed the hard 45s timeout under real-world latency, causing premature fallback. Ingesting untruncated logs also risks context window bloat.
* **Remediation:** Calibrate per-turn tool timeouts (5s), enforce strict log truncation (max 100 lines / 4KB), and align the overall diagnostic timeout threshold to 60s.

#### 8. Unsafe and Unspecified Condition Evaluator in Verification Node (`egm-design`)
* **Affected Scope:** Lines 101–103, 290–291 (`PlanStep Data Contract`)
* **Discrepancy:** `expected_condition: str` lacks an evaluation contract. Using Python `eval()` creates an RCE / prompt injection vulnerability.
* **Remediation:** Replace free-form strings with a type-safe verification contract (metric key, operator, threshold, window) or specify a sandboxed expression engine (Google CEL).

#### 9. Concurrency & Race Conditions in Cooldown Enforcement (`egm-design`)
* **Affected Scope:** Lines 185–190 (`Zero-Trust OPA Governance Payload`)
* **Discrepancy:** Concurrent alerts for the same service can both evaluate `cooldown_active == false` before either records a remediation timestamp, triggering conflicting actions.
* **Remediation:** Specify distributed locking (PostgreSQL advisory lock or Redis lock keyed by `target_service`) acquired during `govern_node` and held through `verify_node`.

#### 10. Malformed Mermaid Syntax (`egm-structural`)
* **Affected Scope:** Lines 303–306 (Mermaid Flowchart)
* **Discrepancy:** Using inline quotes in dotted links (`-. "..." .->`) violates Mermaid syntax and fails in standard renderers.
* **Remediation:** Replace with standard pipe annotation: `-.->|"Read-Only (L0_OBSERVE)"| Tools`.

#### 11. Hyperbolic Framing & Anthropomorphism (`egm-style`)
* **Affected Scope:** Lines 11, 24, 27, 205
* **Discrepancy:** Hyperbolic certainty ("guaranteed"), pejorative phrasing ("rule-bound", "naive", "forced into rigid"), anthropomorphism ("blinding the system"), and premature claims ("verified RCA" at step 2).
* **Remediation:** Apply objective technical phrasing as detailed in the style audit.

---

### Nit Findings

* **Markdown Indentation**: Ensure 4-space indentation for fenced code blocks nested in list items (`egm-structural`).
* **Informal Abbreviations**: Replace `w/` with `with` in flowchart labels (`egm-style`).
* **Decorative Modifiers**: Remove "sudden", "critical", and conversational phrasing in CUJ narratives (`egm-style`).
* **Mermaid Subgraph Targeting**: Target internal gateway node rather than compound subgraph identifier (`egm-structural`).
