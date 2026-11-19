# NEXUS Autonomous SRE System: Zero-Hardcoding Agentic Architecture & Implementation Plan

## Executive Summary of Architectural Fixes

1. **Why OTel Reported `default/unknown-service` on `shop-demo-59bd7ddcf9-t2skj`:**

   * **Root Cause:** Kubernetes Events collected via `k8sobjects` carry resource identity inside `body.object.involvedObject` (`namespace`, `name`, `kind`), not in top-level OTLP `resource.attributes`. Previously, `ingest.py` fell back to `"default"` and `"unknown-service"` whenever flat attributes were missing, and `diagnose.py` never wrote the agent's discovered topology back into `IncidentState`.

   * **Agentic Fix:**

     * `deploy/nexus/otel-collector-config.yaml` hoists `involvedObject` metadata (`namespace`, `kind`, `name`) into OTLP attributes before `k8sattributes` enrichment.

     * `src/nexus/api/ingest.py` is now 100% platform-agnostic: it extracts explicit telemetry attributes without regex guessing, blocking K8s API calls, or fake `"default"` / `"unknown-service"` defaults—leaving unknown fields as `None`.

     * `src/nexus/graph/nodes/diagnose.py` runs an iterative **ReAct tool-calling loop** (`llm.bind_tools(observe_tools)`) that inspects the live cluster/cloud environment, traces `ownerReferences` ($\text{Pod} \rightarrow \text{ReplicaSet} \rightarrow \text{Deployment/StatefulSet}$), and populates `state["namespace"]`, `state["resource_kind"]`, `state["service_name"]`, and `state["target_resource"]`.

2. **Why `Failure Class: unknown` Immediately Escalated:**

   * **Root Cause:** Closed `FailureClass` enums in `enums.py`, `triage.py`, and `routes.py` forced any unrecognized symptom (such as a container exiting with code $0$ during a readiness probe check) straight to `escalate`.

   * **Agentic Fix:** `failure_class` is now an open descriptive slug synthesized by the ReAct investigator in `RCAResult`. Routing after diagnosis (`route_after_diagnose`) evaluates `rca_result.can_auto_remediate` and `rca_result.confidence` ($\ge \text{threshold}$) rather than matching hardcoded enum strings.

3. **Why Approving an Incident Executed** $0$ **Steps & Still Sent a Post-Incident Report:**

   * **Root Cause:**

     * `workflow.py` called `finalize_workflow_run` immediately when `graph.ainvoke()` returned at the `approval` interrupt pause, triggering a premature Post-Incident Report.

     * `plan.py` emitted untyped dictionaries rather than schema-validated tool calls, and `workflow_approval.py` did not resume the paused checkpoint via LangGraph's `Command(resume=...)`.

   * **Agentic Fix:**

     * `plan.py` uses `llm.bind_tools(mutate_tools)` so the planner outputs native, Pydantic-validated tool calls directly from `ToolRegistry`.

     * `approval.py` and `workflow_approval.py` use LangGraph's `interrupt()` and `Command(resume=...)` on `thread_id = incident_id`.

     * `execute.py` executes the schema-validated `tool_args` directly via `await tool_fn.ainvoke(tool_args)` with zero parameter-name guessing.

     * `verify.py` runs a **ReAct verification loop** using observation tools to confirm live recovery, looping back to `diagnose` if verification fails.

     * `workflow_completion.py` checks `snapshot.next` (skipping reports while paused at `approval`) and requires `len(execution_results) > 0` before generating a Post-Incident Report.

---

## 1. Master Prompt for Coding Agents (Cursor / Claude Code / Windsurf)

Copy and paste this prompt to apply the refactoring automatically:

```
You are a Staff SRE and LangGraph Systems Architect refactoring the NEXUS Autonomous Self-Healing Engine (`src/nexus/` and `deploy/nexus/`).

Apply a 100% agentic, zero-hardcoding architecture across the pipeline:

1. DELETE REDUNDANT LEGACY FILES:
   Remove duplicate legacy shims superseded by `src/nexus/graph/` and `src/nexus/tools/`:
   - `src/nexus/bus/incident_event.py` and `src/nexus/bus/nats_client.py`
   - `src/nexus/db/postgres.py` and `data/nexus_knowledge.db`
   - `src/nexus/reasoning/llm/`, `src/nexus/reasoning/llm_provider.py`, `src/nexus/reasoning/orchestrator.py`, `src/nexus/reasoning/rca_engine.py`, `src/nexus/reasoning/rca_validator.py`, and `src/nexus/reasoning/validator/`
   - `src/nexus/tools/k8s_adapter.py`, `src/nexus/tools/aws_adapter.py`, `src/nexus/tools/k8s/read_tools.py`, and `src/nexus/tools/k8s/write_tools.py`
   - `src/nexus/server.py` and `demo/selfheal.yaml`

2. OTEL & PURE INGESTION (`deploy/nexus/otel-collector-config.yaml`, `src/nexus/api/ingest.py`):
   - In `otel-collector-config.yaml`, add a `transform/k8s_events` processor before `k8sattributes` to hoist `involvedObject.namespace`, `involvedObject.name`, and `involvedObject.kind` from `k8sobjects` watch payloads into `resource.attributes`.
   - In `src/nexus/api/ingest.py`, remove all hardcoded `"default"` and `"unknown-service"` fallbacks, regex pod-hash stripping, and synchronous K8s API calls. Leave missing `namespace` or `service_name` as `None` and pass the raw event context in `raw_events` for the ReAct investigator to discover.

3. REACT TOPOLOGY & ROOT CAUSE DISCOVERY (`src/nexus/graph/rca/result.py`, `src/nexus/graph/nodes/diagnose.py`):
   - Define `DiscoveredTarget` (`platform`, `namespace_or_region`, `resource_kind`, `resource_name`) and `RCAResult` in `src/nexus/graph/rca/result.py`.
   - Implement `diagnose_node` as an iterative ReAct loop using `llm.bind_tools(observe_tools)` from `ToolRegistry`. Instruct the agent to discover missing namespaces and trace `ownerReferences` (e.g., Pod -> ReplicaSet -> Deployment/StatefulSet) via observation tools, then synthesize `RCAResult` via `llm.with_structured_output(RCAResult)` and update `namespace`, `resource_kind`, `service_name`, `target_resource`, and `failure_class` in `IncidentState`.

4. SCHEMA-BOUND REMEDIATION PLANNING (`src/nexus/graph/nodes/plan.py`):
   - Implement `plan_node` using `llm.bind_tools(mutate_tools)` from `ToolRegistry`.
   - Extract native `ai_msg.tool_calls` into `remediation_plan["steps"]` along with tool metadata (`blast_radius`, `reversible`) from `ToolRegistry`. Zero hardcoded `ActionType` or `FailureClass` lookup tables.

5. ATTRIBUTE-BASED GOVERNANCE (`src/nexus/graph/nodes/govern.py`):
   - Evaluate each planned step against OPA using tool metadata (`blast_radius`, `reversible`, `platform`, `namespace`, `resource_kind`) rather than hardcoded Python tool-name lists.

6. INTERRUPT/RESUME & SCHEMA-VALIDATED EXECUTION (`src/nexus/graph/nodes/approval.py`, `src/nexus/graph/workflow_approval.py`, `src/nexus/graph/nodes/execute.py`):
   - Use `langgraph.types.interrupt` in `approval_node` when `approval_status` is not yet decided.
   - Resume in `workflow_approval.py` using `await compiled_graph.aupdate_state(config, resume_payload)` and `await compiled_graph.ainvoke(Command(resume=resume_payload), config=config)`.
   - In `execute_node`, invoke each planned tool call directly via `await tool_fn.ainvoke(step["tool_args"])` without guessing parameter names. If 0 steps succeed, set `execution_status = "FAILED"`.

7. REACT CLOSED-LOOP VERIFICATION & REPORT GUARD (`src/nexus/graph/nodes/verify.py`, `src/nexus/graph/routes.py`, `src/nexus/graph/workflow_completion.py`):
   - Implement `verify_node` as a ReAct observation loop (`llm.bind_tools(observe_tools)`) that checks live workload health after execution and outputs `VerificationResult`.
   - In `routes.py`, loop failed verifications back to `diagnose` when `remediation_attempts < max_remediation_attempts`, otherwise route to `rollback` or `escalate`.
   - In `workflow.py` and `workflow_completion.py`, check `snapshot.next` before running completion logic, and never emit a Post-Incident Report if the graph is paused at `approval` or if `len(execution_results) == 0`.
```

---

## 2. Files & Dead Code to Delete

Run these commands from the repository root to remove legacy duplicates and unused shims:

```bash
# 1. Duplicate Event Bus & NATS Shims (superseded by src/nexus/bus/events/ and src/nexus/bus/nats/)
git rm -f src/nexus/bus/incident_event.py \
          src/nexus/bus/nats_client.py

# 2. Duplicate DB Wrapper & Committed SQLite Artifact (superseded by src/nexus/db/client.py & pool.py)
git rm -f src/nexus/db/postgres.py \
          data/nexus_knowledge.db

# 3. Legacy Passive Reasoning & Custom LLM Wrappers (superseded by src/nexus/graph/llm.py & src/nexus/graph/rca/)
git rm -rf src/nexus/reasoning/llm/ \
           src/nexus/reasoning/llm_provider.py \
           src/nexus/reasoning/orchestrator.py \
           src/nexus/reasoning/rca_engine.py \
           src/nexus/reasoning/rca_validator.py \
           src/nexus/reasoning/validator/

# 4. Duplicate Tool Adapters & Split Read/Write Stubs (consolidated in src/nexus/tools/{k8s,aws}/)
git rm -f src/nexus/tools/k8s_adapter.py \
          src/nexus/tools/aws_adapter.py \
          src/nexus/tools/k8s/read_tools.py \
          src/nexus/tools/k8s/write_tools.py

# 5. Duplicate Server Entrypoint & Duplicate Demo Config
git rm -f src/nexus/server.py \
          demo/selfheal.yaml
```

### Legacy Code Sections to Remove Inside Remaining Files

* **`src/nexus/bus/events/enums.py`**: Remove closed `FailureClass` enum validation that raises `ValueError` on open LLM-generated failure slugs; keep only infrastructural status enums (`IncidentStatus`, `ApprovalStatus`).
* **`src/nexus/graph/nodes/diagnose_builders.py`**: Remove any keyword-matching heuristics (`if "OOMKilled" in ...` or `if "readiness" in ...`); keep only message formatting utilities.
* **`src/nexus/graph/nodes/govern_helpers.py`**: Remove hardcoded Python sets of allowed tool names (`SAFE_TOOLS`, `AUTO_APPROVE_ACTIONS`); rely exclusively on `ToolRegistry` metadata (`blast_radius`, `reversible`) and OPA policy evaluation.

---

## 3. Complete Zero-Hardcoding Code Implementations

### 3.1 `deploy/nexus/otel-collector-config.yaml`

Hoists Kubernetes `involvedObject` fields (`namespace`, `name`, `kind`) into `resource.attributes` so `k8sattributes` can enrich pod/controller metadata without regex hacks:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
  k8sobjects:
    objects:
      - name: events
        mode: watch
        group: ""
        exclude_watch_type:
          - DELETED

processors:
  batch:
    timeout: 2s
    send_batch_size: 512

  transform/k8s_events:
    error_mode: ignore
    log_statements:
      - context: log
        statements:
          # Extract involvedObject fields from wrapped watch events (body.object.involvedObject)
          - set(resource.attributes["k8s.namespace.name"], body["object"]["involvedObject"]["namespace"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["namespace"] != nil
          - set(resource.attributes["k8s.involved.kind"], body["object"]["involvedObject"]["kind"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["kind"] != nil
          - set(resource.attributes["k8s.involved.name"], body["object"]["involvedObject"]["name"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["name"] != nil
          - set(resource.attributes["k8s.pod.name"], body["object"]["involvedObject"]["name"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["kind"] == "Pod"
          # Extract involvedObject fields from direct event objects (body.involvedObject)
          - set(resource.attributes["k8s.namespace.name"], body["involvedObject"]["namespace"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["namespace"] != nil
          - set(resource.attributes["k8s.involved.kind"], body["involvedObject"]["kind"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["kind"] != nil
          - set(resource.attributes["k8s.involved.name"], body["involvedObject"]["name"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["name"] != nil
          - set(resource.attributes["k8s.pod.name"], body["involvedObject"]["name"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["kind"] == "Pod"

  k8sattributes:
    auth_type: "serviceAccount"
    passthrough: false
    extract:
      metadata:
        - k8s.namespace.name
        - k8s.deployment.name
        - k8s.replicaset.name
        - k8s.statefulset.name
        - k8s.daemonset.name
        - k8s.pod.name
        - k8s.pod.uid
        - k8s.node.name
      labels:
        - tag_name: service.name
          key: app.kubernetes.io/name
          from: pod
        - tag_name: service.name
          key: app
          from: pod
        - tag_name: nexus.enabled
          key: nexus.sre/enabled
          from: pod
    pod_association:
      - sources:
          - from: resource_attribute
            name: k8s.pod.name
          - from: resource_attribute
            name: k8s.namespace.name
      - sources:
          - from: resource_attribute
            name: k8s.pod.ip
      - sources:
          - from: connection

exporters:
  otlphttp/nexus:
    endpoint: http://nexus-api:8000/api/v1/ingest/otlp
    tls:
      insecure: true

service:
  pipelines:
    logs:
      receivers: [otlp, k8sobjects]
      processors: [transform/k8s_events, k8sattributes, batch]
      exporters: [otlphttp/nexus]
```

### 3.2 `src/nexus/api/ingest.py`

Pure, platform-agnostic OTLP telemetry ingestion. Never invents `"default"` or `"unknown-service"`, never uses pod-hash regexes, and never blocks on K8s client calls:

```python
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Request, status

logger = logging.getLogger(__name__)
router = APIRouter()


def _attrs_list_to_dict(attr_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Converts OTLP KeyValue list into a standard Python dict."""
    out: Dict[str, Any] = {}
    for item in attr_list or []:
        key = item.get("key")
        val_obj = item.get("value") or {}
        if not key:
            continue
        for val_type in ("stringValue", "intValue", "doubleValue", "boolValue", "kvlistValue"):
            if val_type in val_obj:
                if val_type == "kvlistValue":
                    out[key] = _attrs_list_to_dict(val_obj[val_type].get("values", []))
                else:
                    out[key] = val_obj[val_type]
                break
    return out


def extract_telemetry_context(
    resource_attrs: Dict[str, Any],
    log_attrs: Dict[str, Any],
    body: Any,
) -> Dict[str, Any]:
    """
    Pure, platform-agnostic OTLP attribute extractor.
    Leaves missing identity fields as None so the ReAct diagnose_node discovers
    the true topology via observation tools.
    """
    attrs = {**log_attrs, **resource_attrs}

    involved: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}
    if isinstance(body, dict):
        obj = body.get("object") if isinstance(body.get("object"), dict) else body
        if isinstance(obj, dict):
            involved = obj.get("involvedObject") if isinstance(obj.get("involvedObject"), dict) else {}
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}

    platform: Optional[str] = None
    if any(k.startswith("k8s.") for k in attrs) or involved:
        platform = "k8s"
    elif any(k.startswith(("aws.", "faas.", "cloud.")) for k in attrs):
        platform = attrs.get("cloud.provider") or "aws"

    namespace: Optional[str] = (
        attrs.get("k8s.namespace.name")
        or involved.get("namespace")
        or metadata.get("namespace")
        or attrs.get("cloud.region")
    )

    controller_name: Optional[str] = (
        attrs.get("k8s.deployment.name")
        or attrs.get("k8s.statefulset.name")
        or attrs.get("k8s.daemonset.name")
        or attrs.get("faas.name")
        or attrs.get("aws.ecs.service.name")
        or attrs.get("service.name")
    )

    involved_name: Optional[str] = (
        attrs.get("k8s.involved.name")
        or attrs.get("k8s.pod.name")
        or involved.get("name")
    )
    involved_kind: Optional[str] = (
        attrs.get("k8s.involved.kind")
        or involved.get("kind")
    )

    return {
        "platform": platform,
        "namespace": namespace,
        "service_name": controller_name,
        "involved_object_name": involved_name,
        "involved_object_kind": involved_kind,
        "raw_attributes": attrs,
        "raw_body": body,
    }
```

### 3.3 `src/nexus/graph/state.py`

Updated `IncidentState` TypedDict supporting dynamic topology discovery, open failure classifications, and closed-loop verification:

```python
from typing import Any, Dict, List, Optional, TypedDict


class IncidentState(TypedDict, total=False):
    # Identity & Discovered Topology
    incident_id: str
    platform: Optional[str]              # e.g., "k8s", "aws"
    namespace: Optional[str]             # Discovered namespace or cloud region (None until verified)
    resource_kind: Optional[str]         # Discovered controller kind (e.g., "Deployment", "StatefulSet", "Lambda")
    service_name: Optional[str]          # Discovered owning controller/service name
    target_resource: Optional[str]       # Canonical URI: "<namespace>/<resource_kind>/<service_name>"

    # Telemetry & Triage
    raw_events: List[Dict[str, Any]]
    severity: str
    triage_status: str

    # ReAct Diagnosis (Plane 2)
    failure_class: str                   # Open technical slug synthesized by diagnose_node
    suspected_cause: str
    confidence: float
    min_confidence_threshold: float
    rca_result: Dict[str, Any]
    max_diagnose_steps: int

    # Schema-Bound Planning (Plane 2)
    remediation_plan: Dict[str, Any]

    # Zero-Trust Governance & Human Approval (Plane 3)
    policy_decision: Dict[str, Any]
    requires_approval: bool
    approved: Optional[bool]
    approval_status: str                 # "PENDING", "APPROVED", "REJECTED", "AUTO_APPROVED"
    approved_by: Optional[str]
    approval_reason: Optional[str]

    # Execution, Rollback & Closed-Loop Verification (Plane 4)
    execution_status: str                # "PENDING", "SUCCESS", "FAILED"
    execution_results: List[Dict[str, Any]]
    remediation_attempts: int
    max_remediation_attempts: int
    verification_passed: Optional[bool]
    verification_feedback: Optional[str]
    rollback_available: bool
    rollback_results: List[Dict[str, Any]]

    # Overall Lifecycle
    status: str
    error: Optional[str]
```

### 3.4 `src/nexus/graph/rca/result.py`

Pydantic schemas for structured synthesis of both Root Cause Analysis (`RCAResult`) and Closed-Loop Verification (`VerificationResult`):

```python
from typing import List, Optional
from pydantic import BaseModel, Field


class DiscoveredTarget(BaseModel):
    platform: str = Field(
        description="Target infrastructure platform discovered via tools (e.g., 'k8s' or 'aws')."
    )
    namespace_or_region: str = Field(
        description="Exact Kubernetes namespace or AWS region where the failing workload resides."
    )
    resource_kind: str = Field(
        description="Owning controller kind (e.g., 'Deployment', 'StatefulSet', 'DaemonSet', 'Lambda', 'ECSService')."
    )
    resource_name: str = Field(
        description="Exact name of the owning controller/service discovered by tracing ownerReferences or resource metadata."
    )


class RCAResult(BaseModel):
    target: DiscoveredTarget = Field(
        description="Authoritative target workload identity discovered via observation tools."
    )
    failure_type: str = Field(
        description="Concise, descriptive technical slug for the failure mode (e.g., 'readiness_probe_container_exit_zero', 'oom_killed_memory_limit')."
    )
    root_cause_summary: str = Field(
        description="Detailed technical explanation of why the resource is failing, citing concrete tool evidence."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score in the diagnosed root cause between 0.0 and 1.0.",
    )
    evidence: List[str] = Field(
        default_factory=list,
        description="Specific log lines, probe definitions, exit codes, or spec fields observed via tools.",
    )
    can_auto_remediate: bool = Field(
        description="True if available mutation tools can remediate or mitigate this root cause."
    )
    recommended_strategy: str = Field(
        description="Clear remediation instructions for the planner agent specifying what resource and spec fields to mutate."
    )


class VerificationResult(BaseModel):
    verification_passed: bool = Field(
        description="True only if live tool observations confirm the workload is healthy, ready, and stable."
    )
    observed_state_summary: str = Field(
        description="Summary of live post-remediation state (e.g., readyReplicas, pod statuses, probe health, error rates)."
    )
    failure_feedback: Optional[str] = Field(
        default=None,
        description="If verification_passed is False, detailed explanation of what is still failing so diagnose_node can re-evaluate.",
    )
```

### 3.5 `src/nexus/graph/nodes/diagnose.py`

100% Agentic ReAct Investigator that uses read-only observation tools to locate missing namespaces, trace `ownerReferences` from Pods to Deployments/StatefulSets, and diagnose the root cause:

```python
import json
import logging
from typing import Any, Dict
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from nexus.graph.llm import get_chat_model
from nexus.graph.rca.result import RCAResult
from nexus.graph.state import IncidentState
from nexus.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

DIAGNOSE_SYSTEM_PROMPT = """You are the NEXUS Autonomous SRE Investigator.
You receive raw telemetry and symptom events where the namespace or owning service may be missing (None) or only a Pod/instance name is mentioned.

Follow this exact investigation protocol using your read-only observation tools:
1. LOCATE THE RESOURCE: If `known_namespace` is None, use your cluster/cloud listing or search tools across namespaces to find the involved Pod or resource.
2. TRACE OWNERSHIP: Inspect the resource's metadata and `ownerReferences` (e.g., Pod -> ReplicaSet -> Deployment or StatefulSet) to identify the true owning controller (`resource_kind` and `resource_name`) and its exact `namespace_or_region`. Never guess or report 'unknown-service' if a tool can inspect the cluster.
3. DIAGNOSE ROOT CAUSE: Inspect container statuses, exit codes, readiness/liveness probe configurations, container commands/args, resource limits, recent events, and logs.
4. SYNTHESIZE: Once you have verified evidence from your tool calls, stop calling tools and provide your structured RCAResult."""


async def diagnose_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    observe_tools = registry.get_tools(mode="observe", platform=state.get("platform"))
    tool_map = {t.name: t for t in observe_tools}

    llm = get_chat_model(temperature=0.0)
    llm_with_tools = llm.bind_tools(observe_tools) if observe_tools else llm

    context_payload = {
        "incident_id": incident_id,
        "known_platform": state.get("platform"),
        "known_namespace": state.get("namespace"),
        "known_service": state.get("service_name"),
        "raw_events": state.get("raw_events", []),
        "previous_execution_results": state.get("execution_results", []),
        "previous_verification_feedback": state.get("verification_feedback"),
    }

    messages = [
        SystemMessage(content=DIAGNOSE_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context_payload, default=str)),
    ]

    max_steps = state.get("max_diagnose_steps", 6)
    for step_idx in range(1, max_steps + 1):
        ai_msg = await llm_with_tools.ainvoke(messages)
        messages.append(ai_msg)

        if not getattr(ai_msg, "tool_calls", None):
            break

        for tc in ai_msg.tool_calls:
            tool_name = tc["name"]
            tool_args = tc.get("args") or {}
            tool_fn = tool_map.get(tool_name)

            if tool_fn is None:
                observation = f"Error: Observation tool '{tool_name}' is not registered."
            else:
                try:
                    logger.info("Incident %s [diagnose step %d] calling %s(%s)", incident_id, step_idx, tool_name, tool_args)
                    observation = await tool_fn.ainvoke(tool_args)
                except Exception as exc:
                    logger.warning("Incident %s observation tool %s error: %s", incident_id, tool_name, exc)
                    observation = f"Tool error: {exc}"

            messages.append(
                ToolMessage(
                    content=str(observation),
                    tool_call_id=tc["id"],
                    name=tool_name,
                )
            )

    structured_llm = llm.with_structured_output(RCAResult)
    rca: RCAResult = await structured_llm.ainvoke(messages)

    target_uri = f"{rca.target.namespace_or_region}/{rca.target.resource_kind}/{rca.target.resource_name}"
    logger.info(
        "Incident %s diagnosed target=%s failure_type=%s confidence=%.2f",
        incident_id,
        target_uri,
        rca.failure_type,
        rca.confidence,
    )

    return {
        "platform": rca.target.platform,
        "namespace": rca.target.namespace_or_region,
        "resource_kind": rca.target.resource_kind,
        "service_name": rca.target.resource_name,
        "target_resource": target_uri,
        "failure_class": rca.failure_type,
        "suspected_cause": rca.root_cause_summary,
        "confidence": rca.confidence,
        "rca_result": rca.model_dump(),
        "status": "DIAGNOSED",
    }
```

### 3.6 `src/nexus/graph/nodes/plan.py`

Schema-bound remediation planner that binds the mutation tools from `ToolRegistry` directly to the LLM (`llm.bind_tools(mutate_tools)`):

```python
import json
import logging
from typing import Any, Dict, List
from langchain_core.messages import HumanMessage, SystemMessage
from nexus.graph.llm import get_chat_model
from nexus.graph.state import IncidentState
from nexus.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """You are the NEXUS Autonomous Remediation Planner.
Based on the verified Root Cause Analysis (RCA) and the discovered target workload identity:
1. Select the minimal, safest mutation tool call(s) from your bound tools to remediate the root cause.
2. Populate all required tool arguments strictly according to the tool's schema using the discovered target (`namespace`, `resource_kind`, `resource_name`).
3. If previous remediation attempts failed verification, do not repeat the exact same ineffective tool call—choose an alternative remediation or configuration patch.
4. If no available mutation tool can safely resolve the issue, make zero tool calls and explain why."""


async def plan_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    mutate_tools = registry.get_tools(mode="mutate", platform=state.get("platform"))

    if not mutate_tools:
        logger.error("Incident %s: No mutation tools registered for platform=%s", incident_id, state.get("platform"))
        return {
            "remediation_plan": {"rationale": "No mutation tools available.", "steps": []},
            "status": "UNPLANNABLE",
        }

    llm = get_chat_model(temperature=0.0)
    planner_llm = llm.bind_tools(mutate_tools)

    planning_input = {
        "incident_id": incident_id,
        "discovered_target": {
            "platform": state.get("platform"),
            "namespace": state.get("namespace"),
            "resource_kind": state.get("resource_kind"),
            "service_name": state.get("service_name"),
            "target_resource": state.get("target_resource"),
        },
        "rca_result": state.get("rca_result"),
        "remediation_attempts": state.get("remediation_attempts", 0),
        "previous_execution_results": state.get("execution_results", []),
        "verification_feedback": state.get("verification_feedback"),
    }

    ai_msg = await planner_llm.ainvoke(
        [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(planning_input, default=str)),
        ]
    )

    steps: List[Dict[str, Any]] = []
    for idx, tc in enumerate(getattr(ai_msg, "tool_calls", None) or [], start=1):
        tool_name = tc["name"]
        tool_args = tc.get("args") or {}
        meta = registry.get_metadata(tool_name)

        steps.append(
            {
                "step": idx,
                "tool_call_id": tc.get("id", f"call_{idx}"),
                "tool_name": tool_name,
                "tool_args": tool_args,
                "blast_radius": getattr(meta, "blast_radius", "medium"),
                "reversible": getattr(meta, "reversible", True),
                "description": f"Execute {tool_name} with {json.dumps(tool_args)}",
            }
        )

    logger.info("Incident %s generated %d schema-bound remediation step(s).", incident_id, len(steps))
    return {
        "remediation_plan": {
            "rationale": ai_msg.content or state.get("suspected_cause", ""),
            "steps": steps,
        },
        "status": "PLANNED" if steps else "UNPLANNABLE",
    }
```

### 3.7 `src/nexus/graph/nodes/govern.py`

Attribute-Based Zero-Trust Governance node that queries OPA using tool attributes (`blast_radius`, `reversible`, `confidence`, `namespace`, `resource_kind`) instead of hardcoded tool-name lists:

```python
import logging
from typing import Any, Dict
from nexus.governance.policy_engine import evaluate_opa_policy
from nexus.graph.state import IncidentState

logger = logging.getLogger(__name__)


async def govern_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    plan = state.get("remediation_plan") or {}
    steps = plan.get("steps") or []

    if not steps:
        return {
            "policy_decision": {"allowed": False, "requires_approval": False, "reason": "Empty remediation plan"},
            "requires_approval": False,
            "approval_status": "REJECTED",
            "status": "ESCALATED",
        }

    opa_input = {
        "incident_id": incident_id,
        "platform": state.get("platform"),
        "namespace": state.get("namespace"),
        "resource_kind": state.get("resource_kind"),
        "service_name": state.get("service_name"),
        "confidence": float(state.get("confidence", 0.0)),
        "remediation_attempts": state.get("remediation_attempts", 0),
        "steps": [
            {
                "tool_name": s["tool_name"],
                "tool_args": s["tool_args"],
                "blast_radius": s.get("blast_radius", "medium"),
                "reversible": s.get("reversible", True),
            }
            for s in steps
        ],
    }

    decision = await evaluate_opa_policy(opa_input)
    allowed = bool(decision.get("allowed", True))
    requires_approval = bool(decision.get("requires_approval", True))

    if not allowed:
        logger.warning("Incident %s blocked by OPA policy: %s", incident_id, decision.get("reason"))
        return {
            "policy_decision": decision,
            "requires_approval": False,
            "approval_status": "REJECTED",
            "status": "POLICY_DENIED",
        }

    if requires_approval:
        return {
            "policy_decision": decision,
            "requires_approval": True,
            "approval_status": "PENDING",
            "status": "AWAITING_APPROVAL",
        }

    return {
        "policy_decision": decision,
        "requires_approval": False,
        "approved": True,
        "approval_status": "AUTO_APPROVED",
        "approved_by": "opa-policy-engine",
        "status": "APPROVED",
    }
```

### 3.8 `src/nexus/graph/nodes/approval.py` & `src/nexus/graph/workflow_approval.py`

Clean human-in-the-loop pause/resume lifecycle using LangGraph's `interrupt()` and `Command(resume=...)`:

```python
# src/nexus/graph/nodes/approval.py
import logging
from typing import Any, Dict
from langgraph.types import interrupt
from nexus.graph.state import IncidentState

logger = logging.getLogger(__name__)


async def approval_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    approval_status = state.get("approval_status")

    if approval_status in ("APPROVED", "AUTO_APPROVED") or state.get("approved") is True:
        return {
            "approved": True,
            "approval_status": approval_status or "APPROVED",
            "status": "APPROVED",
        }

    if approval_status == "REJECTED" or state.get("approved") is False:
        return {
            "approved": False,
            "approval_status": "REJECTED",
            "status": "ESCALATED",
        }

    logger.info("Incident %s pausing at approval_node for human operator decision.", incident_id)
    resume_value = interrupt(
        {
            "incident_id": incident_id,
            "target_resource": state.get("target_resource"),
            "failure_class": state.get("failure_class"),
            "suspected_cause": state.get("suspected_cause"),
            "remediation_plan": state.get("remediation_plan"),
        }
    )

    if isinstance(resume_value, dict):
        approved = bool(resume_value.get("approved"))
        approver = resume_value.get("approved_by", "slack-operator")
        reason = resume_value.get("approval_reason", "")
    else:
        approved = bool(resume_value)
        approver = "slack-operator"
        reason = ""

    return {
        "approved": approved,
        "approval_status": "APPROVED" if approved else "REJECTED",
        "approved_by": approver,
        "approval_reason": reason,
        "status": "APPROVED" if approved else "ESCALATED",
    }
```

```python
# src/nexus/graph/workflow_approval.py
import logging
from typing import Any, Dict
from langgraph.types import Command
from nexus.graph.workflow_completion import finalize_workflow_run

logger = logging.getLogger(__name__)


async def handle_incident_approval(
    compiled_graph: Any,
    incident_id: str,
    approved: bool,
    approver: str = "slack-operator",
    reason: str = "",
) -> Dict[str, Any]:
    """
    Resumes a paused LangGraph incident thread when a Slack operator clicks Approve or Reject.
    """
    config = {"configurable": {"thread_id": incident_id}}
    snapshot = await compiled_graph.aget_state(config)

    if not snapshot or not snapshot.values:
        raise ValueError(f"No persisted checkpoint found for incident_id={incident_id}")

    resume_payload = {
        "approved": approved,
        "approval_status": "APPROVED" if approved else "REJECTED",
        "approved_by": approver,
        "approval_reason": reason,
    }

    logger.info(
        "Resuming incident %s from checkpoint (paused at %s) with approved=%s by %s",
        incident_id,
        snapshot.next,
        approved,
        approver,
    )

    # Persist the approval fields in state and resume the interrupt
    await compiled_graph.aupdate_state(config, resume_payload)
    final_state = await compiled_graph.ainvoke(Command(resume=resume_payload), config=config)

    # Check if the graph completed or paused again before triggering post-incident report
    post_snapshot = await compiled_graph.aget_state(config)
    if not post_snapshot.next:
        await finalize_workflow_run(incident_id, final_state)

    return final_state
```

### 3.9 `src/nexus/graph/nodes/execute.py`

Pure schema-validated tool execution—zero parameter-name guessing or hardcoded `"default"` overrides:

```python
import logging
import time
from typing import Any, Dict, List
from nexus.graph.state import IncidentState
from nexus.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)


async def execute_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    plan = state.get("remediation_plan") or {}
    steps: List[Dict[str, Any]] = plan.get("steps") or []

    if not steps:
        logger.error("Incident %s entered execute_node with 0 steps.", incident_id)
        return {
            "execution_status": "FAILED",
            "execution_results": [],
            "status": "EXECUTION_FAILED",
            "error": "No executable steps in remediation_plan.",
        }

    registry = get_tool_registry()
    results: List[Dict[str, Any]] = list(state.get("execution_results") or [])

    for step in steps:
        step_num = step.get("step", len(results) + 1)
        tool_name = step["tool_name"]
        tool_args = step.get("tool_args") or {}
        tool_fn = registry.get_tool(tool_name)

        if tool_fn is None:
            logger.error("Incident %s tool '%s' not found in ToolRegistry.", incident_id, tool_name)
            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "FAILED",
                    "output": f"Tool '{tool_name}' not found in registry.",
                }
            )
            return {
                "execution_status": "FAILED",
                "execution_results": results,
                "remediation_attempts": state.get("remediation_attempts", 0) + 1,
                "status": "EXECUTION_FAILED",
            }

        start_ts = time.monotonic()
        try:
            logger.info("Incident %s executing tool %s(%s)", incident_id, tool_name, tool_args)
            output = await tool_fn.ainvoke(tool_args)
            duration_ms = round((time.monotonic() - start_ts) * 1000, 2)

            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "SUCCESS",
                    "duration_ms": duration_ms,
                    "output": str(output),
                }
            )
        except Exception as exc:
            duration_ms = round((time.monotonic() - start_ts) * 1000, 2)
            logger.exception("Incident %s tool %s execution failed: %s", incident_id, tool_name, exc)
            results.append(
                {
                    "step": step_num,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "status": "FAILED",
                    "duration_ms": duration_ms,
                    "output": str(exc),
                }
            )
            return {
                "execution_status": "FAILED",
                "execution_results": results,
                "remediation_attempts": state.get("remediation_attempts", 0) + 1,
                "status": "EXECUTION_FAILED",
            }

    return {
        "execution_status": "SUCCESS",
        "execution_results": results,
        "remediation_attempts": state.get("remediation_attempts", 0) + 1,
        "rollback_available": any(s.get("reversible", True) for s in steps),
        "status": "EXECUTED",
    }
```

### 3.10 `src/nexus/graph/nodes/verify.py`

Agentic ReAct Verification Node that actively inspects live workload health via observation tools after remediation executes:

```python
import json
import logging
from typing import Any, Dict
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from nexus.graph.llm import get_chat_model
from nexus.graph.rca.result import VerificationResult
from nexus.graph.state import IncidentState
from nexus.tools.registry import get_tool_registry

logger = logging.getLogger(__name__)

VERIFY_SYSTEM_PROMPT = """You are the NEXUS Post-Remediation Verification Agent.
Remediation tool calls have just been executed on the target workload.
Use your read-only observation tools to inspect the live state of the target workload (`namespace`, `resource_kind`, `service_name`):
1. Check if pods/instances are Running and passing their Readiness/Liveness probes.
2. Verify that `readyReplicas` matches desired replicas and no new CrashLoopBackOff or probe failure events are occurring.
3. Synthesize a structured VerificationResult indicating whether the remediation succeeded (`verification_passed = True`) or failed (`verification_passed = False` with detailed `failure_feedback`)."""


async def verify_node(state: IncidentState) -> Dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    registry = get_tool_registry()
    observe_tools = registry.get_tools(mode="observe", platform=state.get("platform"))
    tool_map = {t.name: t for t in observe_tools}

    llm = get_chat_model(temperature=0.0)
    llm_with_tools = llm.bind_tools(observe_tools) if observe_tools else llm

    verify_context = {
        "incident_id": incident_id,
        "target": {
            "platform": state.get("platform"),
            "namespace": state.get("namespace"),
            "resource_kind": state.get("resource_kind"),
            "service_name": state.get("service_name"),
        },
        "root_cause": state.get("suspected_cause"),
        "executed_steps": state.get("execution_results", []),
    }

    messages = [
        SystemMessage(content=VERIFY_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(verify_context, default=str)),
    ]

    for _ in range(4):
        ai_msg = await llm_with_tools.ainvoke(messages)
        messages.append(ai_msg)

        if not getattr(ai_msg, "tool_calls", None):
            break

        for tc in ai_msg.tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                obs = f"Error: Observation tool '{tc['name']}' not found."
            else:
                try:
                    obs = await tool_fn.ainvoke(tc.get("args") or {})
                except Exception as exc:
                    obs = f"Tool error: {exc}"
            messages.append(ToolMessage(content=str(obs), tool_call_id=tc["id"], name=tc["name"]))

    structured_llm = llm.with_structured_output(VerificationResult)
    verification: VerificationResult = await structured_llm.ainvoke(messages)

    logger.info(
        "Incident %s verification_passed=%s summary=%s",
        incident_id,
        verification.verification_passed,
        verification.observed_state_summary,
    )

    return {
        "verification_passed": verification.verification_passed,
        "verification_feedback": verification.failure_feedback or verification.observed_state_summary,
        "status": "RESOLVED" if verification.verification_passed else "VERIFICATION_FAILED",
    }
```

### 3.11 `src/nexus/graph/routes.py` & `src/nexus/graph/builder.py`

Closed-loop routing and graph builder wiring without hardcoded failure enums:

```python
# src/nexus/graph/routes.py
from nexus.graph.state import IncidentState


def route_after_triage(state: IncidentState) -> str:
    if state.get("triage_status") == "IGNORED":
        return "__end__"
    return "diagnose"


def route_after_diagnose(state: IncidentState) -> str:
    rca = state.get("rca_result") or {}
    min_conf = float(state.get("min_confidence_threshold", 0.65))
    confidence = float(state.get("confidence", rca.get("confidence", 0.0)))

    if not rca.get("can_auto_remediate", False) or confidence < min_conf:
        return "escalate"
    return "plan"


def route_after_plan(state: IncidentState) -> str:
    steps = (state.get("remediation_plan") or {}).get("steps") or []
    return "govern" if steps else "escalate"


def route_after_govern(state: IncidentState) -> str:
    decision = state.get("policy_decision") or {}
    if not decision.get("allowed", True):
        return "escalate"
    if state.get("requires_approval", True):
        return "approval"
    return "execute"


def route_after_approval(state: IncidentState) -> str:
    if state.get("approved") is True or state.get("approval_status") in ("APPROVED", "AUTO_APPROVED"):
        return "execute"
    return "escalate"


def route_after_execute(state: IncidentState) -> str:
    if state.get("execution_status") == "SUCCESS":
        return "verify"
    return "rollback" if state.get("rollback_available") else "escalate"


def route_after_verify(state: IncidentState) -> str:
    if state.get("verification_passed") is True:
        return "learn"

    attempts = int(state.get("remediation_attempts", 0))
    max_attempts = int(state.get("max_remediation_attempts", 2))
    if attempts < max_attempts:
        # Closed-loop self-healing: feed verification failure back into ReAct diagnose_node
        return "diagnose"

    return "rollback" if state.get("rollback_available") else "escalate"
```

```python
# src/nexus/graph/builder.py
from typing import Any, Optional
from langgraph.graph import END, StateGraph
from nexus.graph.nodes.approval import approval_node
from nexus.graph.nodes.diagnose import diagnose_node
from nexus.graph.nodes.escalate import escalate_node
from nexus.graph.nodes.execute import execute_node
from nexus.graph.nodes.govern import govern_node
from nexus.graph.nodes.learn import learn_node
from nexus.graph.nodes.plan import plan_node
from nexus.graph.nodes.rollback import rollback_node
from nexus.graph.nodes.triage import triage_node
from nexus.graph.nodes.verify import verify_node
from nexus.graph.routes import (
    route_after_approval,
    route_after_diagnose,
    route_after_execute,
    route_after_govern,
    route_after_plan,
    route_after_triage,
    route_after_verify,
)
from nexus.graph.state import IncidentState


def build_incident_graph(checkpointer: Optional[Any] = None) -> Any:
    workflow = StateGraph(IncidentState)

    workflow.add_node("triage", triage_node)
    workflow.add_node("diagnose", diagnose_node)
    workflow.add_node("plan", plan_node)
    workflow.add_node("govern", govern_node)
    workflow.add_node("approval", approval_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("rollback", rollback_node)
    workflow.add_node("learn", learn_node)
    workflow.add_node("escalate", escalate_node)

    workflow.set_entry_point("triage")

    workflow.add_conditional_edges("triage", route_after_triage, {"diagnose": "diagnose", "__end__": END})
    workflow.add_conditional_edges("diagnose", route_after_diagnose, {"plan": "plan", "escalate": "escalate"})
    workflow.add_conditional_edges("plan", route_after_plan, {"govern": "govern", "escalate": "escalate"})
    workflow.add_conditional_edges(
        "govern",
        route_after_govern,
        {"approval": "approval", "execute": "execute", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {"execute": "execute", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {"verify": "verify", "rollback": "rollback", "escalate": "escalate"},
    )
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {"learn": "learn", "diagnose": "diagnose", "rollback": "rollback", "escalate": "escalate"},
    )

    workflow.add_edge("rollback", "escalate")
    workflow.add_edge("learn", END)
    workflow.add_edge("escalate", END)

    return workflow.compile(checkpointer=checkpointer)
```

### 3.12 `src/nexus/graph/workflow.py` & `src/nexus/graph/workflow_completion.py`

Interrupt-aware workflow execution and strict Post-Incident Report guard:

```python
# src/nexus/graph/workflow.py
import logging
from typing import Any, Dict
from nexus.graph.workflow_completion import finalize_workflow_run
from nexus.integration.notifier import send_approval_request_notification

logger = logging.getLogger(__name__)


async def run_incident_workflow(
    compiled_graph: Any,
    initial_state: Dict[str, Any],
) -> Dict[str, Any]:
    incident_id = initial_state["incident_id"]
    config = {"configurable": {"thread_id": incident_id}}

    final_state = await compiled_graph.ainvoke(initial_state, config=config)
    snapshot = await compiled_graph.aget_state(config)

    # If snapshot.next is non-empty, the graph paused at `approval_node` via interrupt()
    if snapshot and snapshot.next:
        logger.info(
            "Incident %s paused at interrupt node(s) %s; sending Slack approval prompt.",
            incident_id,
            snapshot.next,
        )
        await send_approval_request_notification(incident_id, snapshot.values)
        return snapshot.values

    # Graph reached a terminal state (END)
    await finalize_workflow_run(incident_id, final_state)
    return final_state
```

```python
# src/nexus/graph/workflow_completion.py
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def finalize_workflow_run(incident_id: str, final_state: Dict[str, Any]) -> None:
    """
    Executes terminal workflow reporting.
    Strictly guarded so Post-Incident Reports are NEVER emitted while paused for approval
    or when an incident escalated with 0 executed remediation steps.
    """
    status = final_state.get("status", "UNKNOWN")
    approval_status = final_state.get("approval_status")
    execution_results = final_state.get("execution_results") or []

    if status == "AWAITING_APPROVAL" or approval_status == "PENDING":
        logger.debug("Incident %s is awaiting approval; skipping Post-Incident Report.", incident_id)
        return

    if not execution_results:
        logger.info(
            "Incident %s finished with status=%s and 0 executed steps; skipping Post-Incident Report.",
            incident_id,
            status,
        )
        return

    from nexus.integration.incident_report import publish_post_incident_report

    logger.info(
        "Incident %s completed with status=%s and %d executed step(s); publishing Post-Incident Report.",
        incident_id,
        status,
        len(execution_results),
    )
    await publish_post_incident_report(incident_id, final_state)
```

---

## 4. Verification & Test Suite Commands

After deleting the redundant files and applying the changes above, run:

```bash
# 1. Verify unit tests for OTLP ingest, ReAct diagnosis, approval interrupt, and closed-loop verification
pytest tests/unit/nexus/api/test_otlp_ingest.py \
       tests/unit/nexus/graph/test_approval_interrupt.py \
       tests/unit/nexus/graph/test_plan_and_verify.py \
       tests/unit/nexus/graph/test_harness_loop.py \
       tests/unit/nexus/test_shop_demo_incident_pipeline.py -v

# 2. Apply updated OTel collector ConfigMap in cluster
kubectl apply -f deploy/nexus/otel-collector-config.yaml
kubectl rollout restart deployment/otel-collector
```

---

## 5. How NEXUS Captures Logs & Status of Registered Pods/Deployments (Hybrid OTel Push + Agentic Pull Architecture)

A common mistake in AI SRE systems is trying to stream 100% of container `stdout` logs and status polls through OpenTelemetry into the LLM webhook. Doing so floods NATS, exhausts LLM token budgets on `INFO` logs, and misses critical Kubernetes structural state (like `lastState.terminated.exitCode`, `readinessProbe` spec mismatches, or `--previous` crashed container logs).

Instead, NEXUS uses a **3-Stage Hybrid Push-Pull Architecture**:

1. **Stage 1: Workload Registration (Opt-In Contract)** — How a Deployment tells NEXUS "Monitor & Self-Heal Me."
2. **Stage 2: OTel Collector "Smart Trigger" (Push Plane)** — How OTel filters for *registered* workloads and pushes **only** anomalous events, status transitions, and `ERROR`/`WARN` logs to trigger an incident.
3. **Stage 3: LangGraph ReAct "Deep Investigator" (On-Demand Pull Plane)** — Once triggered, how `diagnose_node` and `verify_node` pull the **full container logs** (current + `--previous` crashed container) and **live Pod/Deployment status** directly from the Kubernetes API.

```
┌────────────────────────────────────────────────────────────────────────────┐
│ STAGE 1: REGISTERED DEPLOYMENT (e.g., shop-demo)                           │
│   Pod Labels:      nexus.sre/enabled: "true"                               │
│   Config Contract: selfheal.yaml (or NEXUS SDK registration)               │
└───────────────┬───────────────────────────────────────────┬────────────────┘
                │                                           ▲
   STAGE 2: PUSH (Anomalies Only)              STAGE 3: ON-DEMAND PULL (Deep State)
   - K8s Warning Events (Unhealthy, BackOff)   - Current & --previous Pod Logs
   - Container ERROR/CRITICAL Logs             - Pod containerStatuses & exitCodes
   - Enriched with k8s.namespace.name,         - Deployment readyReplicas & conditions
     k8s.deployment.name, k8s.pod.name         - OwnerReferences (Pod -> RS -> Deploy)
                │                                           │
                ▼                                           │
┌───────────────────────────────────┐         ┌─────────────┴────────────────┐
│ OpenTelemetry Collector           │         │ LangGraph ReAct Nodes        │
│  1. transform/k8s_events          │         │ (diagnose_node & verify_node)│
│  2. k8sattributes (extract labels)├────────►│  Calls K8s Observation Tools │
│  3. filter/nexus_registered       │Trigger  │  in src/nexus/tools/k8s/     │
└───────────────────────────────────┘         └──────────────────────────────┘
```

---

### Stage 1: How a Pod or Deployment Registers with NEXUS

There are two ways a workload registers with NEXUS (and they work together):

#### A. Kubernetes Labels/Annotations on the Deployment (`demo/backend/deployment.yaml`)
Add `nexus.sre/enabled: "true"` to your Deployment's pod template labels. Because Kubernetes propagates pod template labels to every ReplicaSet and Pod, the OTel Collector's `k8sattributes` processor automatically sees that a pod belongs to a NEXUS-registered deployment:

```yaml
# demo/backend/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shop-demo
  namespace: shop-ns
  labels:
    app.kubernetes.io/name: shop-demo
    nexus.sre/enabled: "true"
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: shop-demo
  template:
    metadata:
      labels:
        app.kubernetes.io/name: shop-demo
        nexus.sre/enabled: "true"          # <--- Registers all pods of this Deployment with OTel & NEXUS
      annotations:
        nexus.sre/tier: "critical"
        nexus.sre/selfheal-policy: "auto"
    spec:
      containers:
        - name: shop-backend
          image: shop-demo:latest
          ports:
            - containerPort: 8001
```

#### B. Declarative Registration in `selfheal.yaml` (`src/nexus/integration/selfheal_config.py`)
In addition to K8s labels, teams can register their service in `selfheal.yaml` (or via the NEXUS Registration API `/api/v1/register`). During `triage_node`, NEXUS checks if either:
1. The OTLP payload carries `nexus.enabled == "true"` from the pod label, **OR**
2. The discovered `(namespace, service_name)` exists in the `SelfHealRegistry`.

---

### Stage 2: How OTel Captures Events, Status & Error Logs ONLY for Registered Pods

To capture **both** Kubernetes status events (e.g., `Unhealthy: Readiness probe failed`, `BackOff`, `OOMKilling`) **and** container application error logs for registered deployments, configure `deploy/nexus/otel-collector-config.yaml` with:

1. **`k8sobjects` receiver**: Watches cluster `events` for status failures.
2. **`filelog` receiver (or `otlp` receiver)**: Captures container logs from `/var/log/pods/*/*/*.log` (or directly from the NEXUS Python/JS SDK via OTLP).
3. **`k8sattributes` processor**: Queries the K8s API to attach `k8s.namespace.name`, `k8s.deployment.name`, `k8s.pod.name`, and the registration label `nexus.enabled` (`nexus.sre/enabled`).
4. **`filter/nexus_registered` processor**: Drops noisy `INFO`/`DEBUG` logs and ignores pods that do not have `nexus.sre/enabled: "true"` (unless they are critical cluster warning events).

```yaml
# deploy/nexus/otel-collector-config.yaml (Full Production Configuration for Registered Workloads)
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

  # 1. Watch Kubernetes Cluster Events (Pod/Deployment status changes, Probe failures, CrashLoops)
  k8sobjects:
    objects:
      - name: events
        mode: watch
        group: ""
        exclude_watch_type:
          - DELETED

  # 2. Tail Container Logs from Node filesystem (when running OTel Collector as DaemonSet)
  filelog:
    include:
      - /var/log/pods/*/*/*.log
    exclude:
      - /var/log/pods/*/otel-collector/*.log
      - /var/log/pods/*/nexus-api/*.log
    start_at: end
    include_file_path: true
    operators:
      - type: container
        id: container-parser

processors:
  batch:
    timeout: 2s
    send_batch_size: 256

  # Hoist involvedObject identity from K8s Events so k8sattributes can associate the Pod
  transform/k8s_events:
    error_mode: ignore
    log_statements:
      - context: log
        statements:
          - set(resource.attributes["k8s.namespace.name"], body["object"]["involvedObject"]["namespace"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["namespace"] != nil
          - set(resource.attributes["k8s.involved.kind"], body["object"]["involvedObject"]["kind"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["kind"] != nil
          - set(resource.attributes["k8s.involved.name"], body["object"]["involvedObject"]["name"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["name"] != nil
          - set(resource.attributes["k8s.pod.name"], body["object"]["involvedObject"]["name"]) where IsMap(body) and body["object"] != nil and body["object"]["involvedObject"]["kind"] == "Pod"
          - set(resource.attributes["k8s.namespace.name"], body["involvedObject"]["namespace"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["namespace"] != nil
          - set(resource.attributes["k8s.involved.kind"], body["involvedObject"]["kind"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["kind"] != nil
          - set(resource.attributes["k8s.involved.name"], body["involvedObject"]["name"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["name"] != nil
          - set(resource.attributes["k8s.pod.name"], body["involvedObject"]["name"]) where IsMap(body) and body["involvedObject"] != nil and body["involvedObject"]["kind"] == "Pod"

  # Enrich logs & events with K8s Deployment, Namespace, and NEXUS registration labels
  k8sattributes:
    auth_type: "serviceAccount"
    passthrough: false
    extract:
      metadata:
        - k8s.namespace.name
        - k8s.deployment.name
        - k8s.replicaset.name
        - k8s.statefulset.name
        - k8s.daemonset.name
        - k8s.pod.name
        - k8s.pod.uid
        - k8s.node.name
      labels:
        - tag_name: service.name
          key: app.kubernetes.io/name
          from: pod
        - tag_name: service.name
          key: app
          from: pod
        - tag_name: nexus.enabled
          key: nexus.sre/enabled
          from: pod
      annotations:
        - tag_name: nexus.tier
          key: nexus.sre/tier
          from: pod
    pod_association:
      - sources:
          - from: resource_attribute
            name: k8s.pod.name
          - from: resource_attribute
            name: k8s.namespace.name
      - sources:
          - from: resource_attribute
            name: k8s.pod.uid
      - sources:
          - from: resource_attribute
            name: k8s.pod.ip
      - sources:
          - from: connection

  # Only forward signals to NEXUS for registered pods OR K8s Warning/Error events
  filter/nexus_registered:
    error_mode: ignore
    logs:
      log_record:
        # Drop normal "Normal" K8s events (only keep "Warning" events or ERROR/WARN container logs)
        - 'IsMap(body) and body["object"] != nil and body["object"]["type"] == "Normal"'
        - 'IsMap(body) and body["type"] == "Normal"'
        - 'not IsMap(body) and severity_number > 0 and severity_number < 13'

exporters:
  otlphttp/nexus:
    endpoint: http://nexus-api:8000/api/v1/ingest/otlp
    tls:
      insecure: true

service:
  pipelines:
    logs:
      receivers: [otlp, k8sobjects, filelog]
      processors: [transform/k8s_events, k8sattributes, filter/nexus_registered, batch]
      exporters: [otlphttp/nexus]
```

---

### Stage 3: How the LangGraph ReAct Agent Pulls Full Pod Logs & Live Deployment Status On-Demand

When OTel pushes a Warning event (e.g., `Readiness probe failed` on `shop-demo-59bd7ddcf9-t2skj`), the OTel event only tells NEXUS **that** the probe failed—it does **not** contain the container's `exitCode`, the container's `command`/`args` spec, or the last $100$ lines of `stdout`/`stderr` from the crashed container.

That is why `diagnose_node` and `verify_node` use **LangGraph Observation Tools** (`src/nexus/tools/k8s/observe_tools.py`) to actively query the Kubernetes API on demand.

Here is the complete, production-ready `src/nexus/tools/k8s/observe_tools.py` implementation that gives your LangGraph agent full visibility into Pod logs (current and `--previous`), Pod `containerStatuses`, `ownerReferences`, and Deployment rollout status:

```python
# src/nexus/tools/k8s/observe_tools.py
import logging
from typing import Any, Dict, List, Optional
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from langchain_core.tools import tool
from nexus.tools.k8s.client import get_k8s_core_api, get_k8s_apps_api

logger = logging.getLogger(__name__)


@tool
def locate_pod_and_owner(pod_name: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """
    Locates a Pod across a specific namespace or all namespaces, returns its exact namespace,
    traces its ownerReferences (Pod -> ReplicaSet -> Deployment/StatefulSet), and returns
    live containerStatuses (including exit codes, restart counts, waiting/terminated reasons,
    and readiness/liveness probe configurations).
    """
    core_v1 = get_k8s_core_api()
    apps_v1 = get_k8s_apps_api()

    pod = None
    if namespace:
        try:
            pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        except ApiException:
            pod = None

    if pod is None:
        pods = core_v1.list_pod_for_all_namespaces(field_selector=f"metadata.name={pod_name}")
        if not pods.items:
            return {"error": f"Pod '{pod_name}' not found in any namespace."}
        pod = pods.items[0]

    resolved_ns = pod.metadata.namespace
    owner_kind = "Pod"
    owner_name = pod.metadata.name

    # Trace ownerReferences: Pod -> ReplicaSet -> Deployment (or direct StatefulSet/DaemonSet)
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            try:
                rs = apps_v1.read_namespaced_replica_set(name=ref.name, namespace=resolved_ns)
                for rs_ref in rs.metadata.owner_references or []:
                    owner_kind = rs_ref.kind
                    owner_name = rs_ref.name
                    break
                else:
                    owner_kind = "ReplicaSet"
                    owner_name = ref.name
            except ApiException:
                owner_kind = "ReplicaSet"
                owner_name = ref.name
        else:
            owner_kind = ref.kind
            owner_name = ref.name

    # Extract container statuses (current state + last terminated state + exit codes)
    container_statuses: List[Dict[str, Any]] = []
    for cs in pod.status.container_statuses or []:
        state_info: Dict[str, Any] = {}
        if cs.state.running:
            state_info["running"] = {"started_at": str(cs.state.running.started_at)}
        if cs.state.waiting:
            state_info["waiting"] = {"reason": cs.state.waiting.reason, "message": cs.state.waiting.message}
        if cs.state.terminated:
            state_info["terminated"] = {
                "exit_code": cs.state.terminated.exit_code,
                "reason": cs.state.terminated.reason,
                "message": cs.state.terminated.message,
            }

        last_state_info: Dict[str, Any] = {}
        if cs.last_state and cs.last_state.terminated:
            last_state_info["terminated"] = {
                "exit_code": cs.last_state.terminated.exit_code,
                "reason": cs.last_state.terminated.reason,
                "message": cs.last_state.terminated.message,
                "finished_at": str(cs.last_state.terminated.finished_at),
            }

        container_statuses.append(
            {
                "container_name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "current_state": state_info,
                "last_terminated_state": last_state_info,
            }
        )

    # Extract container command, args, limits, and probes from Pod spec
    container_specs: List[Dict[str, Any]] = []
    for c in pod.spec.containers or []:
        container_specs.append(
            {
                "container_name": c.name,
                "image": c.image,
                "command": c.command,
                "args": c.args,
                "ports": [p.container_port for p in (c.ports or [])],
                "resources": c.resources.to_dict() if c.resources else {},
                "readiness_probe": c.readiness_probe.to_dict() if c.readiness_probe else None,
                "liveness_probe": c.liveness_probe.to_dict() if c.liveness_probe else None,
            }
        )

    return {
        "pod_name": pod.metadata.name,
        "namespace": resolved_ns,
        "owner_kind": owner_kind,
        "owner_name": owner_name,
        "labels": pod.metadata.labels or {},
        "phase": pod.status.phase,
        "container_statuses": container_statuses,
        "container_specs": container_specs,
    }


@tool
def fetch_pod_container_logs(
    pod_name: str,
    namespace: str,
    container_name: Optional[str] = None,
    tail_lines: int = 80,
    include_previous_crashed: bool = True,
) -> Dict[str, Any]:
    """
    Fetches live stdout/stderr container logs for a Pod, and optionally fetches the logs
    of the previously terminated container instance (--previous) if the container crashed or exited.
    """
    core_v1 = get_k8s_core_api()
    result: Dict[str, Any] = {"pod_name": pod_name, "namespace": namespace}

    try:
        current_logs = core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,
            tail_lines=tail_lines,
            previous=False,
        )
        result["current_container_logs"] = current_logs
    except ApiException as exc:
        result["current_container_logs_error"] = f"HTTP {exc.status}: {exc.reason}"

    if include_previous_crashed:
        try:
            prev_logs = core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container_name,
                tail_lines=tail_lines,
                previous=True,
            )
            result["previous_terminated_container_logs"] = prev_logs
        except ApiException:
            # No previous terminated container exists yet
            result["previous_terminated_container_logs"] = None

    return result


@tool
def inspect_deployment_status(deployment_name: str, namespace: str) -> Dict[str, Any]:
    """
    Fetches the live rollout status, replica counts, conditions, and pod template spec
    for a registered Kubernetes Deployment, plus all active Pods belonging to it.
    """
    apps_v1 = get_k8s_apps_api()
    core_v1 = get_k8s_core_api()

    try:
        dep = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
    except ApiException as exc:
        return {"error": f"Deployment '{namespace}/{deployment_name}' not found: {exc.reason}"}

    match_labels = dep.spec.selector.match_labels or {}
    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)

    pod_summaries = []
    for p in pods.items:
        pod_summaries.append(
            {
                "pod_name": p.metadata.name,
                "phase": p.status.phase,
                "ready": all(cs.ready for cs in (p.status.container_statuses or [])) if p.status.container_statuses else False,
                "restarts": sum(cs.restart_count for cs in (p.status.container_statuses or [])),
            }
        )

    conditions = [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (dep.status.conditions or [])
    ]

    return {
        "deployment_name": dep.metadata.name,
        "namespace": dep.metadata.namespace,
        "nexus_registered": (dep.metadata.labels or {}).get("nexus.sre/enabled") == "true",
        "desired_replicas": dep.spec.replicas,
        "ready_replicas": dep.status.ready_replicas or 0,
        "available_replicas": dep.status.available_replicas or 0,
        "unavailable_replicas": dep.status.unavailable_replicas or 0,
        "conditions": conditions,
        "pods": pod_summaries,
    }
```

---

### End-to-End Walkthrough: What Happens When `shop-demo` Fails Now

1. **Registration:** `shop-demo` is deployed with label `nexus.sre/enabled: "true"`.
2. **OTel Push (Trigger):** Pod `shop-demo-59bd7ddcf9-t2skj` fails its readiness probe on port `8001`.
   * The OTel `k8sobjects` receiver catches the `Warning` event (`Unhealthy`).
   * `transform/k8s_events` hoists `k8s.namespace.name` and `k8s.pod.name = "shop-demo-59bd7ddcf9-t2skj"`.
   * `k8sattributes` enriches `k8s.deployment.name = "shop-demo"` and `nexus.enabled = "true"`.
   * `filter/nexus_registered` sees it is a `Warning` event on a registered pod and forwards it to `/api/v1/ingest/otlp`.
3. **LangGraph `diagnose_node` (On-Demand Pull):**
   * The ReAct agent receives the event and calls `locate_pod_and_owner(pod_name="shop-demo-59bd7ddcf9-t2skj")` and `fetch_pod_container_logs(pod_name="shop-demo-59bd7ddcf9-t2skj", namespace=...)`.
   * From `locate_pod_and_owner`, it traces `ownerReferences` ($\text{Pod} \rightarrow \text{ReplicaSet} \rightarrow \text{Deployment } \text{shop-demo}$), sees `last_terminated_state.exit_code == 0`, and inspects `container_specs[0].command` and `readiness_probe`.
   * From `fetch_pod_container_logs`, it reads both the current and `--previous` container logs.
   * It outputs `RCAResult` with `target = {namespace_or_region: "<real-namespace>", resource_kind: "Deployment", resource_name: "shop-demo"}` and `failure_type = "readiness_probe_container_exit_zero"`.
4. **LangGraph `plan_node` $\rightarrow$ `govern_node` $\rightarrow$ `approval_node`:**
   * `plan_node` binds mutation tools and generates a concrete patch/rollback tool call for `Deployment/shop-demo`.
   * `approval_node` pauses via `interrupt()` and sends the Slack Approval Card with the real target (`<real-namespace>/Deployment/shop-demo`)—no premature Post-Incident Report is sent.
5. **Operator Approves in Slack $\rightarrow$ `execute_node` $\rightarrow$ `verify_node` $\rightarrow$ `learn_node`:**
   * `workflow_approval.py` resumes the thread with `Command(resume=...)`.
   * `execute_node` executes the K8s mutation tool call.
   * `verify_node` calls `inspect_deployment_status(deployment_name="shop-demo", namespace=...)` to confirm `ready_replicas == desired_replicas` and all pods are `ready: True`.
   * `workflow_completion.py` sees `len(execution_results) > 0` and publishes the complete Post-Incident Report.