# NEXUS Autonomous SRE Refactoring: Full Hardcoding Audit & Master Implementation Prompt

## PART 1: Complete Audit of Hardcoded Bottlenecks in NEXUS (AWS & Kubernetes)

A full inspection of `src/nexus/` and `deploy/opa/` reveals **7 layers of hardcoding** that currently prevent NEXUS from acting as a general-purpose, cross-provider self-healing agent.

### 1. Ingestion, Log Parsing & Event Bus (`src/nexus/bus/` & `src/nexus/telemetry/`)
* **`src/nexus/bus/events/enums.py` & `src/nexus/bus/events/contexts.py`**: Hardcodes closed `Enum` classes for failure types and rigid context models (`K8sContext`, `DBContext`, `AWSContext`). Any failure outside this list fails Pydantic validation before reaching NATS.
* **`src/nexus/telemetry/log_parser.py` & `src/nexus/telemetry/log_shipper.py`**: Uses static regex rules to match known error strings (e.g., `OOMKilled`, `ProvisionedThroughputExceededException`, `Connection refused`) and maps them to hardcoded enums. Unrecognized stack traces or new AWS/K8s service errors are silently dropped.

### 2. Polling Specialist Checkers (`src/nexus/agents/`)
* **`src/nexus/agents/k8s/constants.py`, `pod_checker.py`, `deployment_checker.py`**: Only polls `v1.Pod` and `apps/v1.Deployment` for `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, and replica count mismatches. It is completely blind to `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `PVC` storage exhaustion, `Ingress` misconfigurations, `NetworkPolicy` drops, `CoreDNS` failures, or `Node` disk/PID pressure.
* **`src/nexus/agents/metrics/queries.py` & `checker.py`**: Hardcodes a small set of PromQL query templates and static threshold comparisons.
* **`src/nexus/agents/db/postgres.py`, `mysql.py`, `mongo.py`**: Hardcodes specific connection-pool and slow-query checks rather than exposing database inspection tools to the RCA agent.
* **`src/nexus/agents/nginx/parser.py` & `evaluator.py`**: Hardcodes nginx log parsing and HTTP 502/504 thresholds.

### 3. Platform Adapters & Static Snapshot Collectors (`src/nexus/graph/platform/`)
* **`src/nexus/graph/platform/aws_target.py`, `aws_snapshot.py`, `aws_telemetry.py`, `aws.py`**:
  * `aws_target.py` only parses targets for a handful of hardcoded AWS resource types (Lambda, DynamoDB, ECS). Any incident on RDS, SQS, ALB/ELB, API Gateway, S3, Step Functions, ElastiCache, CloudFront, or Route53 fails target resolution.
  * `aws_telemetry.py` and `aws_snapshot.py` pre-fetch a static dictionary of specific CloudWatch metric names before the LLM even runs, rather than letting the agent query CloudWatch Metrics, Logs Insights, and CloudTrail dynamically.
* **`src/nexus/graph/platform/k8s_target.py`, `k8s_snapshot.py`, `k8s_telemetry.py`, `k8s.py`**: Hardcodes target resolution to `namespace/deployment/pod` and dumps a fixed snapshot into `collect.py`.

### 4. Correlation, Confidence & Validation Heuristics (`src/nexus/reasoning/`)
* **`src/nexus/reasoning/correlator_rules.py` & `event_correlator.py`**: Uses a hardcoded lookup table of `EventType` pairs to correlate events.
* **`src/nexus/reasoning/confidence_constants.py` & `confidence_scorer.py`**: Computes confidence scores from static weights tied to predefined agent names and enum types.
* **`src/nexus/reasoning/validator/consistency.py` & `downgrade.py`**: Downgrades valid LLM diagnoses if they do not match hardcoded keyword/enum expectations.

### 5. Single-Shot RCA & Template Planners (`src/nexus/graph/nodes/` & `src/nexus/graph/rca/`)
* **`src/nexus/graph/nodes/collect.py`**: Forces upfront static telemetry collection instead of iterative tool use.
* **`src/nexus/graph/rca/synthesis_rules.py`, `multi_agent_rca.py`, `log_analyst.py`, `metrics_analyst.py`, `topology_analyst.py`**: Runs a single-pass prompt over static text and overrides the LLM with deterministic `if/else` rules in `synthesis_rules.py`.
* **`src/nexus/graph/nodes/default_planner.py`, `k8s_plan_helpers.py`, `adaptive_recovery.py`**: Hardcodes `incident_type -> tool_name` mappings (`restart_pod`, `scale_deployment`, `rollback_deployment`, `update_table_capacity`).

### 6. Narrow, Duplicated Tool Implementations (`src/nexus/tools/` & `src/nexus/agents/k8s_tools/`)
* **Duplicated K8s Tools**: K8s tools are split across `src/nexus/agents/k8s_tools/` and `src/nexus/tools/k8s/` and only wrap fixed `CoreV1Api` / `AppsV1Api` methods for Pods and Deployments.
* **Narrow AWS Tools (`src/nexus/tools/aws/observe_tools.py`, `mutate_tools.py`, `schemas.py`)**: Only wraps a few specific AWS operations (such as DynamoDB capacity and Lambda concurrency), leaving 98% of AWS services without tools.

### 7. Hardcoded Governance, Live-State Checkers & KB Seed Patterns (`src/nexus/governance/` & `src/nexus/learning/`)
* **`deploy/opa/nexus_policies.rego` & `src/nexus/governance/policy_engine.py`**: Matches literal tool/action strings (`restart_pod`, `scale_deployment`).
* **`src/nexus/governance/action_ladder.py`**: Hardcodes a fixed escalation sequence (`restart -> scale -> rollback`).
* **`src/nexus/governance/live_state/pod_checker.py`, `deployment_checker.py`, `validator.py`**: Only validates live state for K8s Pods and Deployments; breaks or no-ops on AWS resources or other K8s kinds.
* **`src/nexus/learning/kb_patterns.py`**: Pre-seeds the knowledge base with hardcoded regex/enum patterns.

---

## PART 2: Copy-Paste Master Prompt for Your Coding Agent

Copy everything below and provide it directly to your coding agent (Antigravity IDE, Claude Code, or Cursor):

---

### PROMPT START

You are a Principal Autonomous Systems & SRE Architect refactoring the NEXUS self-healing engine (`src/nexus/`).
Your goal is to remove all hardcoded failure detection enums, static telemetry snapshots, narrow platform target parsers, heuristic RCA rules, hardcoded remediation planners, and action-name-locked OPA policies. You will replace them with a **provider-agnostic, tool-calling Agentic Workflow in LangGraph** that supports **any AWS service** and **any Kubernetes resource/CRD** dynamically.

```text
[Universal Symptom Ingest] (CloudWatch / EventBridge / Alertmanager / OTel / K8s Events)
        │
        ▼
[1. ReAct Investigator Agent (diagnose.py)]
    Loops over Read-Only Tools (Universal Boto3 Inspect + CloudWatch/CloudTrail + K8s DynamicClient + PromQL + MCP)
    Tests & falsifies hypotheses until root cause is proven by tool outputs -> emits `RCAReport`
        │
        ▼
[2. Schema-Driven Planner Agent (plan.py)]
    Uses Mutating Tool Schemas + RCA Pre-State Snapshot to synthesize `RemediationPlan`
    Every step includes `tool_name`, `tool_arguments`, exact inverse `rollback_call`, and read-only `verification_checks`
        │
        ▼
[3. Attribute-Based OPA Governance Gate (govern.py + nexus_policies.rego)]
    Deterministic Python risk classifier + Rego ABAC policy (evaluates blast_radius, reversibility, service sensitivity, confidence, cost_delta)
        │
        ├──► [ALLOW_AUTO] ────────────────────────────────────────┐
        │                                                         ▼
        └──► [REQUIRE_APPROVAL] ──► [approval.py (LangGraph interrupt)] ──► [execute.py] ──► [verify.py]
                                     Sends Slack/Console Approval Card       Runs planned     Runs `verification_checks`
                                     with RCA Evidence, Plan & Rollback;     tool calls via   read tools; auto-triggers
                                     resumes on human `/approve`             ToolRegistry     `rollback.py` if unhealthy
```

---

#### 1. FILES TO DELETE COMPLETELY

Delete the following hardcoded files and clean up all imports/exports referencing them across `src/nexus/` and `tests/`:

* **Static Collection & Narrow Platform Snapshots:**
  * `src/nexus/graph/nodes/collect.py`
  * `src/nexus/graph/platform/aws_snapshot.py`
  * `src/nexus/graph/platform/aws_target.py`
  * `src/nexus/graph/platform/aws_telemetry.py`
  * `src/nexus/graph/platform/k8s_snapshot.py`
  * `src/nexus/graph/platform/k8s_target.py`
  * `src/nexus/graph/platform/k8s_telemetry.py`
* **Passive Single-Shot Analysts & Heuristic RCA Rules:**
  * `src/nexus/graph/rca/synthesis_rules.py`
  * `src/nexus/graph/rca/multi_agent_rca.py`
  * `src/nexus/graph/rca/log_analyst.py`
  * `src/nexus/graph/rca/metrics_analyst.py`
  * `src/nexus/graph/rca/topology_analyst.py`
  * `src/nexus/graph/rca/baseline.py`
  * `src/nexus/graph/rca/synthesizer.py`
* **Hardcoded Planners & Escalation Ladders:**
  * `src/nexus/graph/nodes/default_planner.py`
  * `src/nexus/graph/nodes/k8s_plan_helpers.py`
  * `src/nexus/graph/nodes/pluggable_planner.py`
  * `src/nexus/graph/nodes/adaptive_recovery.py`
  * `src/nexus/governance/action_ladder.py`
* **Hardcoded Validation, Correlation & Live-State Checkers:**
  * `src/nexus/reasoning/correlator_rules.py`
  * `src/nexus/reasoning/confidence_constants.py`
  * `src/nexus/reasoning/validator/consistency.py`
  * `src/nexus/reasoning/validator/downgrade.py`
  * `src/nexus/governance/live_state/pod_checker.py`
  * `src/nexus/governance/live_state/deployment_checker.py`
  * `src/nexus/learning/kb_patterns.py`
* **Duplicate Legacy K8s Tool Package:**
  * `src/nexus/agents/k8s_tools/` (entire directory and `src/nexus/agents/k8s_tools.py`—consolidate into `src/nexus/tools/k8s/`)

---

#### 2. LAYER-BY-LAYER IMPLEMENTATION & CODE SPECIFICATIONS

##### A. Open Symptom Ingestion (`src/nexus/bus/events/enums.py` & `src/nexus/bus/events/event.py`)
Replace failure root-cause enums with open symptom envelopes so any AWS service or Kubernetes resource anomaly can enter the bus without schema rejection.

```python
# src/nexus/bus/events/enums.py
from enum import Enum


class SignalSource(str, Enum):
    AWS_CLOUDWATCH = "aws_cloudwatch"
    AWS_EVENTBRIDGE = "aws_eventbridge"
    KUBERNETES_API = "kubernetes_api"
    PROMETHEUS = "prometheus"
    OTEL_SDK = "otel_sdk"
    WEBHOOK = "webhook"


class SignalKind(str, Enum):
    METRIC_ANOMALY = "metric_anomaly"
    ERROR_LOG_BURST = "error_log_burst"
    STATE_TRANSITION = "state_transition"
    SLO_BURN_ALERT = "slo_burn_alert"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
```

```python
# src/nexus/bus/events/event.py
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field
from src.nexus.bus.events.enums import Severity, SignalKind, SignalSource


class SymptomSignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: f"sig-{uuid4().hex[:12]}")
    source: SignalSource
    kind: SignalKind
    provider: str = Field(description="Cloud/platform provider, e.g., 'aws', 'kubernetes'")
    resource_urn: str = Field(
        description="Universal resource identifier, e.g., ARN 'arn:aws:rds:us-east-1:123456789012:db:prod-pg' or 'k8s://prod/apps/v1/StatefulSet/redis'"
    )
    region_or_namespace: str = Field(default="default")
    symptom_summary: str = Field(description="Natural-language alert or anomaly summary")
    raw_payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw CloudWatch/EventBridge/K8s/OTel JSON payload",
    )
    severity: Severity = Severity.HIGH
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

* **Update `src/nexus/telemetry/log_parser.py`**: Instead of regex-matching hardcoded failure names, detect any log record with `level in ("ERROR", "CRITICAL", "FATAL")` or HTTP status `>= 500` or unhandled exception tracebacks, group bursts by `resource_urn`, and emit a `SymptomSignal(kind=SignalKind.ERROR_LOG_BURST)` with the raw log lines in `raw_payload`.
* **Update `src/nexus/agents/k8s/agent.py`**: Replace `pod_checker.py` and `deployment_checker.py` with a single generic K8s event watcher that watches `v1.Event` where `type == "Warning"` across **all** `involvedObject.kind` types (`Pod`, `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `Node`, `PersistentVolumeClaim`, `Ingress`, CRDs) and emits a `SymptomSignal(kind=SignalKind.STATE_TRANSITION)`.

---

##### B. Universal AWS Tool Bridge (`src/nexus/tools/aws/universal.py`)
Replace narrow AWS tools with three schema-validated, service-agnostic Boto3 tools capable of inspecting, querying telemetry, and mutating **any** AWS service (Lambda, RDS, ECS, EKS, SQS, SNS, DynamoDB, ALB/ELB, API Gateway, S3, ElastiCache, Step Functions, EC2, AutoScaling).

```python
# src/nexus/tools/aws/universal.py
from __future__ import annotations
import json
import time
from typing import Any, Literal
import boto3
from botocore.exceptions import ClientError, ParamValidationError
from pydantic import BaseModel, Field

ALLOWED_READ_PREFIXES = ("describe_", "list_", "get_", "filter_", "lookup_", "batch_get_")
DENIED_READ_OPERATIONS = {
    "get_secret_value",
    "get_parameter",
    "get_parameters",
    "get_parameters_by_path",
    "get_federation_token",
    "get_session_token",
}
DENIED_MUTATE_SERVICES = {"iam", "kms", "organizations", "sts", "billing", "ce"}


class AWSInspectInput(BaseModel):
    service: str = Field(
        description="Boto3 service name, e.g., 'lambda', 'rds', 'ecs', 'sqs', 'dynamodb', 'elbv2', 'apigateway', 'elasticache', 'ec2', 's3', 'stepfunctions'"
    )
    operation: str = Field(
        description="Read-only boto3 client method starting with describe_, list_, get_, filter_, or lookup_ (e.g., 'describe_db_instances', 'get_function_configuration', 'get_queue_attributes')"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Exact boto3 keyword arguments for the operation (e.g., {'DBInstanceIdentifier': 'prod-db'})",
    )
    region: str = Field(default="us-east-1", description="AWS region")


class AWSQueryTelemetryInput(BaseModel):
    telemetry_type: Literal["cloudwatch_metrics", "cloudwatch_logs_insights", "cloudtrail_events"]
    region: str = Field(default="us-east-1")
    parameters: dict[str, Any] = Field(
        description=(
            "For 'cloudwatch_metrics': kwargs for cloudwatch.get_metric_statistics or get_metric_data. "
            "For 'cloudwatch_logs_insights': {'logGroupName': str, 'queryString': str, 'startTime': int, 'endTime': int}. "
            "For 'cloudtrail_events': kwargs for cloudtrail.lookup_events (e.g., {'LookupAttributes': [{'AttributeKey': 'ResourceName', 'AttributeValue': '...'}]})."
        )
    )


class AWSMutateInput(BaseModel):
    service: str = Field(
        description="Boto3 service name, e.g., 'lambda', 'rds', 'ecs', 'dynamodb', 'application-autoscaling', 'sqs'"
    )
    operation: str = Field(
        description="Mutating boto3 client method, e.g., 'put_function_concurrency', 'update_service', 'modify_db_instance', 'update_table'"
    )
    parameters: dict[str, Any] = Field(description="Exact boto3 kwargs for the mutation")
    target_arn: str = Field(description="Target AWS resource ARN or identifier")
    region: str = Field(default="us-east-1")


def execute_aws_inspect(args: AWSInspectInput) -> str:
    if not args.operation.startswith(ALLOWED_READ_PREFIXES) or args.operation in DENIED_READ_OPERATIONS:
        return json.dumps({
            "error": f"Operation '{args.operation}' is not a permitted read-only inspection method. Allowed prefixes: {ALLOWED_READ_PREFIXES}"
        })
    try:
        client = boto3.client(args.service, region_name=args.region)
        if not hasattr(client, args.operation):
            available = [m for m in dir(client) if m.startswith(ALLOWED_READ_PREFIXES)][:40]
            return json.dumps({
                "error": f"Service '{args.service}' has no operation '{args.operation}'.",
                "hint_available_read_operations": available,
            })
        method = getattr(client, args.operation)
        response = method(**args.parameters)
        response.pop("ResponseMetadata", None)
        return json.dumps(response, default=str)[:12000]
    except (ParamValidationError, ClientError, TypeError) as exc:
        return json.dumps({
            "error": str(exc),
            "self_correction_hint": f"Check parameter names and types for {args.service}.{args.operation} and retry.",
        })


def execute_aws_query_telemetry(args: AWSQueryTelemetryInput) -> str:
    try:
        if args.telemetry_type == "cloudwatch_metrics":
            cw = boto3.client("cloudwatch", region_name=args.region)
            op = "get_metric_data" if "MetricDataQueries" in args.parameters else "get_metric_statistics"
            res = getattr(cw, op)(**args.parameters)
            res.pop("ResponseMetadata", None)
            return json.dumps(res, default=str)[:12000]

        if args.telemetry_type == "cloudwatch_logs_insights":
            logs = boto3.client("logs", region_name=args.region)
            start_res = logs.start_query(**args.parameters)
            query_id = start_res["queryId"]
            for _ in range(10):
                res = logs.get_query_results(queryId=query_id)
                if res.get("status") in ("Complete", "Failed", "Cancelled"):
                    return json.dumps({"status": res.get("status"), "results": res.get("results", [])[:50]}, default=str)
                time.sleep(0.5)
            return json.dumps({"status": "Running", "queryId": query_id})

        if args.telemetry_type == "cloudtrail_events":
            ct = boto3.client("cloudtrail", region_name=args.region)
            res = ct.lookup_events(**args.parameters)
            res.pop("ResponseMetadata", None)
            return json.dumps(res, default=str)[:12000]

        return json.dumps({"error": f"Unsupported telemetry_type: {args.telemetry_type}"})
    except (ParamValidationError, ClientError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


def execute_aws_mutate(args: AWSMutateInput) -> str:
    if args.service.lower() in DENIED_MUTATE_SERVICES:
        raise PermissionError(f"Mutations on security/account service '{args.service}' are prohibited.")
    client = boto3.client(args.service, region_name=args.region)
    method = getattr(client, args.operation)
    response = method(**args.parameters)
    response.pop("ResponseMetadata", None)
    return json.dumps(response, default=str)[:8000]
```

---

##### C. Universal Kubernetes Dynamic Tool Bridge (`src/nexus/tools/k8s/universal.py`)
Replace Pod/Deployment-only K8s tools with Kubernetes `DynamicClient` tools that can inspect, fetch logs/events, patch, scale, or restart **any** Kubernetes resource (`Pod`, `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `Service`, `Ingress`, `ConfigMap`, `HorizontalPodAutoscaler`, `PersistentVolumeClaim`, or custom CRDs).

```python
# src/nexus/tools/k8s/universal.py
from __future__ import annotations
from datetime import datetime, timezone
import json
from typing import Any, Literal
from kubernetes import client
from kubernetes.dynamic import DynamicClient
from pydantic import BaseModel, Field


class K8sInspectResourceInput(BaseModel):
    api_version: str = Field(
        default="v1",
        description="e.g., 'v1', 'apps/v1', 'autoscaling/v2', 'networking.k8s.io/v1', 'batch/v1'",
    )
    kind: str = Field(
        description="e.g., 'Pod', 'Deployment', 'StatefulSet', 'DaemonSet', 'Service', 'Ingress', 'ConfigMap', 'Event', 'Node', 'PersistentVolumeClaim'"
    )
    name: str | None = Field(
        default=None,
        description="Specific resource name, or None to list resources in the namespace",
    )
    namespace: str = Field(default="default")
    label_selector: str | None = Field(
        default=None, description="Optional label selector, e.g., 'app=shop-backend'"
    )


class K8sGetLogsInput(BaseModel):
    pod_name: str
    namespace: str = Field(default="default")
    container: str | None = Field(default=None)
    tail_lines: int = Field(default=100, ge=10, le=500)
    previous: bool = Field(
        default=False,
        description="Set True to fetch logs from previous crashed container instance",
    )


class K8sMutateResourceInput(BaseModel):
    api_version: str = Field(default="apps/v1", description="e.g., 'v1', 'apps/v1', 'autoscaling/v2'")
    kind: str = Field(
        description="e.g., 'Deployment', 'StatefulSet', 'DaemonSet', 'ConfigMap', 'Service', 'Pod'"
    )
    name: str
    namespace: str = Field(default="default")
    mutation_type: Literal[
        "strategic_merge_patch", "scale_replicas", "rollout_restart", "delete_pod"
    ]
    patch_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Strategic merge patch dict, or {'replicas': int} for scale_replicas",
    )


def execute_k8s_inspect(dyn_client: DynamicClient, args: K8sInspectResourceInput) -> str:
    resource_api = dyn_client.resources.get(api_version=args.api_version, kind=args.kind)
    if args.name:
        obj = resource_api.get(name=args.name, namespace=args.namespace)
        return json.dumps(obj.to_dict(), default=str)[:12000]
    items = resource_api.get(namespace=args.namespace, label_selector=args.label_selector)
    return json.dumps(items.to_dict(), default=str)[:12000]


def execute_k8s_get_logs(core_v1: client.CoreV1Api, args: K8sGetLogsInput) -> str:
    logs = core_v1.read_namespaced_pod_log(
        name=args.pod_name,
        namespace=args.namespace,
        container=args.container,
        tail_lines=args.tail_lines,
        previous=args.previous,
    )
    return logs[-12000:]


def execute_k8s_mutate(dyn_client: DynamicClient, args: K8sMutateResourceInput) -> str:
    resource_api = dyn_client.resources.get(api_version=args.api_version, kind=args.kind)
    if args.mutation_type == "delete_pod" and args.kind == "Pod":
        res = resource_api.delete(name=args.name, namespace=args.namespace)
        return json.dumps(res.to_dict(), default=str)[:6000]

    if args.mutation_type == "scale_replicas":
        replicas = int(args.patch_body["replicas"])
        patch = {"spec": {"replicas": replicas}}
        res = resource_api.patch(
            name=args.name,
            namespace=args.namespace,
            body=patch,
            content_type="application/merge-patch+json",
        )
        return json.dumps(res.to_dict(), default=str)[:6000]

    if args.mutation_type == "rollout_restart":
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }
        res = resource_api.patch(
            name=args.name,
            namespace=args.namespace,
            body=patch,
            content_type="application/strategic-merge-patch+json",
        )
        return json.dumps(res.to_dict(), default=str)[:6000]

    res = resource_api.patch(
        name=args.name,
        namespace=args.namespace,
        body=args.patch_body,
        content_type="application/strategic-merge-patch+json",
    )
    return json.dumps(res.to_dict(), default=str)[:6000]
```

---

##### D. Unified Tool Registry with Read/Write Separation (`src/nexus/tools/registry.py`)
Refactor `ToolRegistry` so every tool has explicit metadata (`is_mutating: bool`, `provider: str`) and expose:
* `registry.get_read_only_tools_as_langchain() -> list[StructuredTool]` (Bound exclusively to the Investigator Agent in `diagnose.py` and Verifier Agent in `verify.py`).
* `registry.get_mutating_tool_schemas() -> list[dict]` (Schemas passed to the Planner Agent in `plan.py`; executed only by `execute.py` and `rollback.py` after OPA + Human Approval).
* Add an optional MCP loader method `registry.load_mcp_tools(mcp_servers_config)` so external AWS/Kubernetes/GitHub/Postgres MCP servers can be plugged in without writing Python wrappers.

---

##### E. Hypothesis-Driven ReAct Investigator Loop (`src/nexus/graph/rca/result.py` & `src/nexus/graph/nodes/diagnose.py`)
Replace the static `collect -> multi_agent_rca` pipeline with an iterative ReAct loop where the LLM calls read-only tools until it proves the root cause.

```python
# src/nexus/graph/rca/result.py
from typing import Any
from pydantic import BaseModel, Field


class EvidenceCitation(BaseModel):
    tool_name: str = Field(description="Exact read-only tool called during investigation")
    tool_arguments: dict[str, Any] = Field(description="Arguments passed to the tool")
    key_finding: str = Field(
        description="Concrete metric value, log line, or config state returned by the tool"
    )


class RCAReport(BaseModel):
    root_cause_summary: str = Field(description="Precise technical explanation of the root cause")
    affected_resource_urn: str = Field(description="Primary failing resource ARN or K8s URI")
    provider: str = Field(description="'aws' or 'kubernetes'")
    confidence: float = Field(ge=0.0, le=1.0, description="Evidence-backed confidence score")
    hypotheses_evaluated: list[str] = Field(
        description="Hypotheses tested and either confirmed or falsified"
    )
    evidence_chain: list[EvidenceCitation] = Field(
        min_length=1, description="Verified tool outputs proving the root cause"
    )
    pre_incident_resource_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Current live configuration parameters of the failing resource needed to construct a safe rollback",
    )
    requires_remediation: bool = Field(default=True)
```

```python
# src/nexus/graph/nodes/diagnose.py
from __future__ import annotations
import json
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from src.nexus.graph.rca.result import RCAReport
from src.nexus.graph.state import IncidentState
from src.nexus.tools.registry import ToolRegistry

RCA_SYSTEM_PROMPT = """You are the NEXUS Autonomous SRE Investigator.
You are given a raw `SymptomSignal` (an alert, metric anomaly, log burst, or state transition).
Do NOT guess or assume the root cause from the symptom title alone.

Follow this strict scientific ReAct investigation protocol:
1. OBSERVE: Call read-only inspection and telemetry tools (`aws_inspect_resource`, `aws_query_telemetry`, `k8s_inspect_resource`, `k8s_get_logs`, `promql_query`) on the target resource.
2. HYPOTHESIZE: Formulate 2-3 specific technical hypotheses (e.g., upstream dependency failure, recent config/deployment change in CloudTrail/K8s events, capacity/quota exhaustion, bad environment variable/secret).
3. TEST & FALSIFY: Call read-only tools to gather evidence that proves one hypothesis and falsifies the others. Capture the resource's current configuration state so a rollback can be constructed later.
4. CONCLUDE: Once your tool outputs provide concrete proof, call the `RCAReport` structured output tool.
Never invent tool outputs. Every claim in `evidence_chain` must come from an actual tool call in this session."""

MAX_RCA_STEPS = 8


async def rca_reasoner_node(state: IncidentState, llm, registry: ToolRegistry) -> dict:
    read_tools = registry.get_read_only_tools_as_langchain()
    llm_with_tools = llm.bind_tools([*read_tools, RCAReport])

    messages = state.get("rca_messages") or [
        SystemMessage(content=RCA_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Investigate this SymptomSignal and determine the root cause:\n{json.dumps(state['symptom_signal'], default=str, indent=2)}"
        ),
    ]

    response: AIMessage = await llm_with_tools.ainvoke(messages)
    steps = state.get("rca_steps", 0) + 1

    for tool_call in getattr(response, "tool_calls", []):
        if tool_call["name"] == "RCAReport":
            report = RCAReport.model_validate(tool_call["args"])
            return {
                "rca_messages": [*messages, response],
                "rca_report": report.model_dump(),
                "rca_steps": steps,
                "rca_complete": True,
            }

    return {
        "rca_messages": [*messages, response],
        "rca_steps": steps,
        "rca_complete": steps >= MAX_RCA_STEPS or not getattr(response, "tool_calls", None),
    }


async def rca_tool_executor_node(state: IncidentState, registry: ToolRegistry) -> dict:
    messages = list(state["rca_messages"])
    last_ai_message: AIMessage = messages[-1]
    trace = list(state.get("investigation_trace", []))

    for tool_call in getattr(last_ai_message, "tool_calls", []):
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]
        if tool_name == "RCAReport":
            continue
        observation = await registry.execute_read_only(tool_name, tool_args)
        trace.append({"tool_name": tool_name, "tool_args": tool_args, "output": observation})
        messages.append(
            ToolMessage(content=str(observation), tool_call_id=tool_call["id"], name=tool_name)
        )

    return {"rca_messages": messages, "investigation_trace": trace}
```

---

##### F. Dynamic Schema-Driven Remediation Planner (`src/nexus/graph/nodes/plan.py`)
Delete all hardcoded `incident_type` planners. Have the Planner LLM synthesize a `RemediationPlan` directly from the `RCAReport`, `investigation_trace`, and registered mutating/read tool schemas.

```python
# src/nexus/graph/nodes/plan.py
from __future__ import annotations
import json
from typing import Any, Literal
from pydantic import BaseModel, Field
from src.nexus.graph.state import IncidentState
from src.nexus.tools.registry import ToolRegistry


class PlannedToolAction(BaseModel):
    step_number: int
    tool_name: str = Field(
        description="Registered mutating tool name, e.g., 'aws_mutate_resource' or 'k8s_mutate_resource'"
    )
    tool_arguments: dict[str, Any] = Field(
        description="Exact arguments matching the tool's Pydantic schema"
    )
    target_resource_urn: str
    rationale: str
    action_category: Literal[
        "restart", "scale", "config_patch", "rollback", "traffic_shift", "destructive"
    ]
    blast_radius: Literal["pod_or_instance", "service", "cluster_or_vpc", "stateful_data"]
    reversible: bool
    rollback_tool_name: str = Field(
        description="Mutating tool name to undo this action if verification fails"
    )
    rollback_arguments: dict[str, Any] = Field(
        description="Exact arguments to restore the pre-incident state captured during RCA"
    )


class VerificationCheck(BaseModel):
    tool_name: str = Field(description="Read-only tool to execute after remediation stabilizes")
    tool_arguments: dict[str, Any]
    expected_healthy_condition: str = Field(
        description="Concrete condition in the read tool output that proves the incident is resolved"
    )


class RemediationPlan(BaseModel):
    summary: str
    actions: list[PlannedToolAction]
    verification_checks: list[VerificationCheck] = Field(min_length=1)
    estimated_cost_delta_pct: float = Field(default=0.0)
    stabilization_wait_seconds: int = Field(default=30, ge=5, le=300)


PLANNER_PROMPT = """You are the NEXUS Autonomous SRE Remediation Planner.
Given the verified `RCAReport`, the `investigation_trace` (containing the resource's pre-incident configuration), and the available tool schemas:
1. Compose the minimal, safest sequence of mutating tool calls (`actions`) to resolve the root cause.
2. For EVERY mutating action, populate `rollback_tool_name` and `rollback_arguments` using the exact pre-incident values observed during RCA so NEXUS can automatically revert if the fix fails.
3. Define 1-3 `verification_checks` using ONLY read-only tools (`aws_inspect_resource`, `aws_query_telemetry`, `k8s_inspect_resource`, `promql_query`) that will be executed after `stabilization_wait_seconds` to prove recovery."""


async def plan_node(state: IncidentState, llm, registry: ToolRegistry) -> dict:
    planner_llm = llm.with_structured_output(RemediationPlan)
    mutating_schemas = registry.get_mutating_tool_schemas()
    readonly_schemas = registry.get_read_only_tool_schemas()

    prompt = (
        f"{PLANNER_PROMPT}\n\n"
        f"## RCA Report\n{json.dumps(state['rca_report'], indent=2)}\n\n"
        f"## Investigation Trace (Pre-Incident State)\n{json.dumps(state.get('investigation_trace', []), indent=2)}\n\n"
        f"## Available Mutating Tools\n{json.dumps(mutating_schemas, indent=2)}\n\n"
        f"## Available Read-Only Verification Tools\n{json.dumps(readonly_schemas, indent=2)}"
    )
    plan: RemediationPlan = await planner_llm.ainvoke(prompt)
    return {"remediation_plan": plan.model_dump()}
```

---

##### G. Attribute-Based OPA Governance (`src/nexus/governance/policy_engine.py` & `deploy/opa/nexus_policies.rego`)
Replace tool-name checks with a **two-layer defense**:
1. **Deterministic Python Risk Classifier (`policy_engine.py`)**: Inspects the raw `tool_arguments` (service name, operation verb, K8s kind) so the LLM cannot spoof a destructive operation as "low risk".
2. **Attribute-Based Rego Policy (`deploy/opa/nexus_policies.rego`)**: Evaluates the risk envelope without referencing any hardcoded tool or incident names.

```python
# src/nexus/governance/policy_engine.py
DESTRUCTIVE_VERBS = ("delete", "terminate", "drop", "purge", "remove", "destroy", "deregister")
RESTRICTED_AWS_SERVICES = {"iam", "kms", "organizations", "sts", "secretsmanager", "ssm", "billing"}
STATEFUL_KINDS_OR_SERVICES = {
    "rds",
    "dynamodb",
    "s3",
    "elasticache",
    "StatefulSet",
    "PersistentVolumeClaim",
    "PersistentVolume",
}


def build_opa_input_for_action(
    action: dict,
    rca_report: dict,
    cooldown_active: bool,
    circuit_breaker_tripped: bool,
    cost_delta_pct: float,
) -> dict:
    args = action.get("tool_arguments", {})
    op = str(args.get("operation", "") or args.get("mutation_type", "")).lower()
    service_or_kind = str(args.get("service", "") or args.get("kind", ""))

    # Deterministic overrides so the LLM cannot under-report risk
    is_destructive_verb = any(v in op for v in DESTRUCTIVE_VERBS) and op != "delete_pod"
    is_restricted_service = service_or_kind.lower() in RESTRICTED_AWS_SERVICES
    is_stateful = (
        service_or_kind in STATEFUL_KINDS_OR_SERVICES
        or action.get("blast_radius") == "stateful_data"
    )

    return {
        "tool_name": action["tool_name"],
        "service_or_kind": service_or_kind,
        "operation": op,
        "action_category": "destructive"
        if is_destructive_verb
        else action.get("action_category", "config_patch"),
        "blast_radius": "stateful_data"
        if is_stateful
        else action.get("blast_radius", "service"),
        "reversible": False if is_destructive_verb else bool(action.get("reversible", False)),
        "has_rollback_plan": bool(
            action.get("rollback_tool_name") and action.get("rollback_arguments")
        ),
        "restricted_security_service": is_restricted_service,
        "rca_confidence": float(rca_report.get("confidence", 0.0)),
        "cooldown_active": cooldown_active,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "estimated_cost_delta_pct": cost_delta_pct,
    }
```

```rego
# deploy/opa/nexus_policies.rego
package nexus.governance

import future.keywords.if
import future.keywords.in

default decision := "DENY"

deny_reasons contains "Mutations on security/identity services (IAM/KMS/STS/Secrets) are strictly prohibited" if {
    input.restricted_security_service == true
}

deny_reasons contains "Destructive or irreversible mutations on stateful data stores are prohibited" if {
    input.blast_radius == "stateful_data"
    input.action_category == "destructive"
}

deny_reasons contains "Mutating action is missing an explicit rollback tool call" if {
    input.has_rollback_plan == false
}

deny_reasons contains "Circuit breaker is tripped for target resource" if {
    input.circuit_breaker_tripped == true
}

# Autonomous execution tier: zero deny reasons, reversible, pod/instance blast radius, high confidence
decision := "ALLOW_AUTO" if {
    count(deny_reasons) == 0
    input.reversible == true
    input.action_category in {"restart", "scale"}
    input.blast_radius == "pod_or_instance"
    input.rca_confidence >= 0.85
    input.cooldown_active == false
    input.estimated_cost_delta_pct <= 25.0
}

# Human-in-the-loop tier: safe to propose, but requires explicit operator approval via Slack/Console
decision := "REQUIRE_APPROVAL" if {
    count(deny_reasons) == 0
    not decision == "ALLOW_AUTO"
}
```

---

##### H. LangGraph Workflow Wiring, Human Approval Interrupt, Execution & Closed-Loop Verification (`src/nexus/graph/`)

```python
# src/nexus/graph/builder.py
from langgraph.graph import END, StateGraph
from src.nexus.graph.state import IncidentState


def route_after_rca_reasoner(state: IncidentState) -> str:
    if state.get("rca_complete"):
        rca = state.get("rca_report") or {}
        if not rca.get("requires_remediation", True):
            return "learn"
        return "plan"
    return "rca_tool_executor"


def route_after_govern(state: IncidentState) -> str:
    decision = state.get("governance_decision", "DENY")
    if decision == "DENY":
        return "escalate"
    if decision == "ALLOW_AUTO":
        return "execute"
    return "approval"


def route_after_approval(state: IncidentState) -> str:
    if state.get("human_approved") is True:
        return "execute"
    return "escalate"


def route_after_verify(state: IncidentState) -> str:
    if state.get("verification_passed") is True:
        return "learn"
    return "rollback"


def build_nexus_agent_graph(nodes: dict, checkpointer=None):
    graph = StateGraph(IncidentState)

    graph.add_node("triage", nodes["triage"])
    graph.add_node("rca_reasoner", nodes["rca_reasoner"])
    graph.add_node("rca_tool_executor", nodes["rca_tool_executor"])
    graph.add_node("plan", nodes["plan"])
    graph.add_node("govern", nodes["govern"])
    graph.add_node("approval", nodes["approval"])
    graph.add_node("execute", nodes["execute"])
    graph.add_node("verify", nodes["verify"])
    graph.add_node("rollback", nodes["rollback"])
    graph.add_node("learn", nodes["learn"])
    graph.add_node("escalate", nodes["escalate"])

    graph.set_entry_point("triage")
    graph.add_edge("triage", "rca_reasoner")
    graph.add_conditional_edges(
        "rca_reasoner",
        route_after_rca_reasoner,
        {
            "rca_tool_executor": "rca_tool_executor",
            "plan": "plan",
            "learn": "learn",
        },
    )
    graph.add_edge("rca_tool_executor", "rca_reasoner")
    graph.add_edge("plan", "govern")
    graph.add_conditional_edges(
        "govern",
        route_after_govern,
        {
            "execute": "execute",
            "approval": "approval",
            "escalate": "escalate",
        },
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "execute": "execute",
            "escalate": "escalate",
        },
    )
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "learn": "learn",
            "rollback": "rollback",
        },
    )
    graph.add_edge("rollback", "escalate")
    graph.add_edge("learn", END)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer)
```

* **`src/nexus/graph/nodes/approval.py`**:
  * Sends an interactive notification via `notifier.py` (Slack Block Kit + Console UI) containing the `RCAReport` evidence chain, the `RemediationPlan` tool calls, and the rollback guarantee.
  * Calls LangGraph `interrupt({"incident_id": state["incident_id"], "rca_report": state["rca_report"], "remediation_plan": state["remediation_plan"]})` to pause execution until the human clicks Approve or Reject.
* **`src/nexus/graph/nodes/execute.py`**:
  * Registers each `PlannedToolAction.rollback_tool_name` and `rollback_arguments` in `rollback_registry.py`.
  * Executes each `PlannedToolAction.tool_name` with `tool_arguments` via `ToolRegistry.execute_mutating()`.
* **`src/nexus/graph/nodes/verify.py`**:
  * Waits `stabilization_wait_seconds` (mockable in tests), runs every read-only `VerificationCheck` in `remediation_plan["verification_checks"]`, and evaluates whether `expected_healthy_condition` is satisfied. If `False`, routes to `rollback.py` to execute the inverse tool calls automatically.

---

#### 3. VERIFICATION & TEST REQUIREMENTS

1. Update `tests/unit/nexus/governance/test_zero_trust_opa.py` and `tests/unit/nexus/test_policy_engine.py` to test the attribute-based Rego rules and Python verb overrides (`delete_db_instance` -> `DENY`, `modify_db_instance` -> `REQUIRE_APPROVAL`, `k8s_mutate_resource` pod restart -> `ALLOW_AUTO`).
2. Update `tests/unit/nexus/graph/test_multi_agent_rca.py` to test the multi-turn `rca_reasoner <-> rca_tool_executor` loop across both AWS (`aws_inspect_resource`, `aws_query_telemetry`) and Kubernetes (`k8s_inspect_resource`, `k8s_get_logs`), including self-correction when Boto3 raises `ParamValidationError`.
3. Update `tests/unit/nexus/graph/test_plan_and_verify.py` and `tests/unit/nexus/graph/test_approval_interrupt.py` to verify dynamic `RemediationPlan` synthesis, `interrupt()` approval pause/resume, verification check execution, and automatic rollback when verification fails.
4. Update `tests/integration/test_nexus_cross_provider_pipeline.py` to test arbitrary AWS incidents (e.g., RDS connection exhaustion, SQS DLQ spike, Lambda concurrency throttle) and Kubernetes incidents (e.g., StatefulSet PVC full, Bad ConfigMap rollout) end-to-end without a single hardcoded failure enum.
5. Run `pytest` and `python scripts/check_loc.py` and ensure 100% of unit and integration tests pass with zero line-count violations.

### PROMPT END