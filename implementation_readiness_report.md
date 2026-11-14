# Implementation Readiness Review Report (Stage 9)

**Document Audited:** `docs/designs/nexus_autonomous_architecture.md` (DetailedRange: Lines 235–303)  
**Standard Applied:** [Implementation Readiness Standard](/home/vatsal/.gemini/config/plugins/egm/resources/document_standards/implementation-readiness-standard.md)  
**Reviewers:** Pragmatic Tech Lead (`egm-implementation-readiness`), Codebase Quality Auditor (`egm-codebase-details`), Technical Copy Editor (`egm-style`)  
**Date:** 2026-11-13  
**Status:** REVISE (Blockers Found)

---

## Executive Summary

The Implementation Readiness review swarm stress-tested the execution plan against the codebase implementation in `src/nexus/` and the Zero-Decision Rule. 

While the architectural blueprint is solid, the Detailed Implementation Plan (lines 235–303) currently fails the Implementation Readiness Standard. Crucial architectural wiring—specifically StateGraph routing in `builder.py` and `routes.py`—is omitted from the task breakdown. Additionally, `default_planner.py` is mischaracterized as a graph node rather than a helper module, Phase 6 conflates multiple discrete systems into a single bullet list, and concrete file-level targets with `[Create]`, `[Modify]`, and `[Delete]` annotations are missing.

---

## Consolidated Findings Grouped by Severity

### Blocker Findings

#### 1. Target Scope Standard Violation: Missing Explicit File Manifest (`egm-implementation-readiness`, `egm-codebase-details`)
* **Affected Scope:** `## Detailed Implementation Plan` (Lines 235–271)
* **Discrepancy:** The Implementation Readiness Standard mandates an explicit **Target Scope** manifest listing every file to be created, modified, or deleted with its exact absolute or repository path before Phase 1. The current plan provides only parenthetical directory hints (e.g., `src/nexus/tools/`), leaving file creation and placement decisions to the implementer.
* **Remediation:** Add a comprehensive Target Scope table before Phase 1 explicitly categorizing each file with `[Create]`, `[Modify]`, or `[Delete]`.

#### 2. Graph Topology Inversion & Route Disconnects (`egm-implementation-readiness`, `egm-codebase-details`)
* **Affected Scope:** Lines 248–252 (Phase 3), 264–270 (Phase 6), and `src/nexus/graph/builder.py`, `src/nexus/graph/routes.py`
* **Discrepancy:** 
  1. `src/nexus/graph/builder.py` currently hardwires a linear analysis chain: `START -> triage -> collect -> diagnose -> plan -> govern`. It does not support `triage -> diagnose` with a conditional edge falling back to `collect`.
  2. `src/nexus/graph/routes.py` (`route_after_approval`) only returns `"execute"` or `"escalate"`. It contains no conditional branch to route an operator `"edit"` action back to `"govern"` or `"plan"`.
  3. `builder.py` and `routes.py` are not listed in the Phase 3 or Phase 6 tasks, creating an unexecutable plan.
* **Remediation:** Explicitly add `src/nexus/graph/builder.py` and `src/nexus/graph/routes.py` as `[Modify]` targets. Define the new conditional routing functions (`route_after_diagnose` with `"plan"` vs `"fallback_collect"`, and `route_after_approval` returning `"govern"` on `"edit"`).

#### 3. Non-Existent Node Destination: `default_planner.py` (`egm-codebase-details`)
* **Affected Scope:** Line 255 (Phase 4: Planning & Verification)
* **Discrepancy:** The plan specifies: *"Ensure validation failures route to `default_planner.py`"*. However, in the codebase, `src/nexus/graph/nodes/default_planner.py` is not a registered LangGraph node—it is a library of helper functions (`build_k8s_default_steps`, `build_aws_default_steps`). StateGraph edges cannot route to Python helper libraries.
* **Remediation:** Clarify that deterministic fallback planning is executed directly inside `plan_remediation_node` (invoking `build_k8s_default_steps` or `build_aws_default_steps` when `state["fallback_active"] == True` or when LLM structured output fails validation).

---

### Major Findings

#### 4. Phase 6 Scope Conflation (`egm-implementation-readiness`)
* **Affected Scope:** Lines 264–270 (Phase 6: HITL Interrupt & Routing)
* **Discrepancy:** Phase 6 packs approval, execution, verification, rollback, audit logging, and Slack notification handling across 5 distinct subsystem files into a single 5-step phase without file-level task separation.
* **Remediation:** Partition Phase 6 into distinct, file-specific implementation tasks:
  * `src/nexus/integration/slack_templates.py`: Block Kit template formatting.
  * `src/nexus/graph/nodes/approval.py`: LangGraph `interrupt()` payload and resume handling.
  * `src/nexus/graph/nodes/execute.py`: Sequential tool execution via `ToolRegistry`.
  * `src/nexus/graph/nodes/verify.py` & `rollback.py`: Metric verification and automated inverse execution.
  * `src/nexus/graph/nodes/learn.py`: Audit trail recording in PostgreSQL.

#### 5. Incomplete Tool Schemas & Metadata in Phase 1 (`egm-codebase-details`)
* **Affected Scope:** Lines 237–242 (Phase 1)
* **Discrepancy:** Phase 1 mandates implementing `aws_dynamodb_update_table`, `aws_rds_delete_db_instance`, and `aws_dynamodb_describe_table` in `src/nexus/tools/aws/`, but fails to specify:
  1. The specific file placement (`mutate_tools.py` vs `observe_tools.py`).
  2. The Pydantic argument schemas (`DynamoDBUpdateTableInput`, `RDSDeleteInstanceInput`, `DynamoDBDescribeTableInput`).
  3. The `blast_radius: str` property addition to `NexusTool`.
  4. The concrete wrapper implementation mechanics of `.as_langchain_tool()` using `langchain_core.tools.StructuredTool`.
* **Remediation:** Explicitly detail the Pydantic schemas, file targets, and method signatures in Phase 1.

#### 6. State Contract Drift & Ingestion Pipeline (`egm-codebase-details`)
* **Affected Scope:** Lines 243–247 (Phase 2)
* **Discrepancy:** 
  1. `IncidentGraphState` currently contains a stale comment (`# 1. Deterministic Telemetry (NO ReAct loop!)`) and lacks the required `fallback_active: bool` flag.
  2. The plan does not specify how `src/nexus/graph/nodes/triage.py` extracts `AnomalySignal` fields (e.g., `incident_id`, `service`, `metrics`, `logs_snippet`) or where `workflow_events.py` is updated.
  3. `diagnose_node` must map LLM output directly into `state["diagnosis"]` matching the `DiagnosisResult` schema to prevent downstream node crashes.
* **Remediation:** Specify the exact fields to add to `IncidentGraphState`, define the ingestion logic in `triage_node`, and ensure contract compatibility in `diagnose_node`.

#### 7. OPA Payload & `PolicyEngine` Contract Drift in Phase 5 (`egm-codebase-details`)
* **Affected Scope:** Lines 258–263 (Phase 5)
* **Discrepancy:** In `src/nexus/governance/policy_engine.py`, `PolicyEngine.evaluate()` currently evaluates actions individually against separate endpoints (`/v1/data/nexus/allow_action`, `/v1/data/nexus/deny_reasons`, `/v1/data/nexus/requires_human_approval`). The architecture mandates a unified evaluation against `/v1/data/nexus/governance`.
* **Remediation:** Explicitly task `PolicyEngine` refactoring in Phase 5 to support batch evaluation of `RemediationPlan` steps and OPA endpoint alignment.

#### 8. Advisory Lock Lifecycle Precision (`egm-codebase-details`)
* **Affected Scope:** Line 259 (Phase 5)
* **Discrepancy:** The plan mentions acquiring PostgreSQL advisory locks in `govern_node`, but does not specify the lock release timing or re-acquisition in `execute_node`. Holding a lock across `approval_node` starves `PostgresPool` (max 10 connections).
* **Remediation:** Specify short-lived lock windows:
  * In `govern_node`: Acquire lock via `SELECT pg_try_advisory_lock(zlib.crc32(service.encode()))`, evaluate cooldown/circuit breaker, record reservation, and release immediately before `approval_node`.
  * In `execute_node`: Re-acquire lock immediately prior to executing mutating tools and release after verification.

#### 9. Missing Per-Phase Exit Criteria & Executable Test Commands (`egm-implementation-readiness`)
* **Affected Scope:** Lines 235–271
* **Discrepancy:** The plan lists verification tests at the very end of the document, but provides no per-phase exit criteria or executable commands to verify stability incrementally at each phase checkpoint.
* **Remediation:** Add an explicit `Exit Criteria` section with concrete CLI commands (`pytest ...`) to every phase.

---

### Nit Findings

* **Typo / Consistency in Field Names:** Ensure `tool_name` and `parameters` in `PlanStep` remain the primary contract keys without introducing conflicting aliases (`egm-style`).
* **Test Path Precision:** Ensure all test paths in `Verification Plan` match the existing directory structure under `tests/unit/nexus/` and `tests/integration/` (`egm-implementation-readiness`).

---

## Conclusion & Verdict

**Verdict:** **REVISE (3 Blockers, 6 Majors)**  
The plan cannot be handed off to an implementer until the Target Scope manifest, Graph routing updates, tool schema definitions, advisory lock lifecycles, and per-phase exit criteria are codified in `docs/designs/nexus_autonomous_architecture.md`.
