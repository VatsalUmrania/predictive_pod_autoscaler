import type { NexusIncident, NexusPrediction } from "@/lib/nexus/types"

/**
 * Static fixture used by the `/demo` route group only.
 *
 * These records are written in the exact shape the NEXUS API returns, so the demo
 * pages and the live pages render through identical components with no divergence.
 * Nothing here is ever used as a fallback for a live page.
 */

export const DEMO_INCIDENTS: NexusIncident[] = [
  {
    incident_id: "INC-9482",
    updated_at: "2026-11-18T17:18:42Z",
    platform: "k8s",
    namespace: "production",
    resource_kind: "Deployment",
    service_name: "shop-demo",
    target_resource: "production/Deployment/shop-demo",
    involved_object: "Pod/shop-demo-59bd7ddcf9-t2skj",
    involved_object_name: "shop-demo-59bd7ddcf9-t2skj",
    severity: "WARNING",
    status: "AWAITING_APPROVAL",
    active_node: "APPROVAL",
    next_nodes: ["APPROVAL"],
    triage_status: "TRIAGED",
    failure_class: "readiness_probe_exit_zero",
    suspected_cause:
      "Readiness probe on /healthz fails after the revision 14 rollout, leaving no ready pods behind the production ingress.",
    confidence: 0.94,
    min_confidence_threshold: 0.65,
    can_auto_remediate: true,
    recommended_strategy:
      "Roll back Deployment/shop-demo to revision 13 and verify readyReplicas reaches 3 of 3.",
    evidence: [
      "k8s_get_pod_events: Warning Unhealthy pod/shop-demo-59bd7ddcf9-t2skj Readiness probe failed: HTTP probe failed with statuscode: 503",
      "k8s_trace_owner_references: Pod/shop-demo-59bd7ddcf9-t2skj to ReplicaSet/shop-demo-59bd7ddcf9 to Deployment/shop-demo (ns=production)",
      "k8s_describe_deployment: revision=14 image=registry.internal/shop-demo:v2.4.1 readyReplicas=0 desiredReplicas=3",
    ],
    rca_result: {
      failure_type: "readiness_probe_exit_zero",
      confidence: 0.94,
      can_auto_remediate: true,
      recommended_strategy:
        "Roll back Deployment/shop-demo to revision 13 and verify readyReplicas reaches 3 of 3.",
      target: {
        platform: "k8s",
        namespace_or_region: "production",
        resource_kind: "Deployment",
        resource_name: "shop-demo",
      },
    },
    remediation_plan: {
      rationale:
        "Revert Deployment/shop-demo from revision 14 to the last healthy revision 13.",
      steps: [
        {
          step: 1,
          tool_call_id: "call_rb_1",
          tool_name: "k8s_rollback_deployment",
          tool_args: {
            namespace: "production",
            deployment_name: "shop-demo",
            target_revision: 13,
          },
          blast_radius: "service_wide",
          reversible: true,
        },
      ],
    },
    policy_decision: {
      allowed: true,
      requires_approval: true,
      reason:
        "OPA policy: service_wide mutation in the production namespace requires human sign-off.",
    },
    requires_approval: true,
    approval_status: "PENDING",
    approved: null,
    approved_by: null,
    approval_reason: null,
    is_interrupted: true,
    approval: {
      requires_approval: true,
      approval_status: "PENDING",
      approved: null,
      approved_by: null,
      approval_reason: null,
      is_interrupted: true,
    },
    execution_status: "PENDING",
    execution_results: [],
    remediation_attempts: 0,
    max_remediation_attempts: 2,
    execution: {
      execution_status: "PENDING",
      results: [],
      remediation_attempts: 0,
      max_remediation_attempts: 2,
    },
    verification_passed: null,
    verification_feedback: "",
    verification: {
      verification_passed: null,
      verification_feedback: "",
    },
    rollback_available: false,
    rollback_results: [],
    rollback: {
      rollback_available: false,
      rollback_results: [],
    },
    error: null,
    post_incident_report_ready: false,
    target: "production/Deployment/shop-demo",
    outcome: "pending",
    timestamp: "2026-11-18T17:18:42Z",
  },
  {
    incident_id: "INC-9479",
    updated_at: "2026-11-18T16:54:10Z",
    platform: "k8s",
    namespace: "payments",
    resource_kind: "StatefulSet",
    service_name: "ledger-worker",
    target_resource: "payments/StatefulSet/ledger-worker",
    involved_object: "Pod/ledger-worker-1",
    involved_object_name: "ledger-worker-1",
    severity: "WARNING",
    status: "RESOLVED",
    active_node: "LEARN",
    next_nodes: [],
    triage_status: "TRIAGED",
    failure_class: "oom_killed_memory_limit",
    suspected_cause:
      "Container memory limit of 512Mi was exceeded during settlement batch replay. The pod restarted 4 times with exit code 137.",
    confidence: 0.91,
    min_confidence_threshold: 0.65,
    can_auto_remediate: true,
    recommended_strategy:
      "Patch the memory limit to 1Gi on StatefulSet/ledger-worker and verify pod stability for 60s.",
    evidence: [
      "k8s_get_pod_status: container=worker lastState.terminated.reason=OOMKilled exitCode=137",
      "prometheus_query: container_memory_working_set_bytes peaked at 536870912 bytes (100% of limit)",
    ],
    rca_result: {
      failure_type: "oom_killed_memory_limit",
      confidence: 0.91,
      can_auto_remediate: true,
      recommended_strategy:
        "Patch the memory limit to 1Gi on StatefulSet/ledger-worker and verify pod stability for 60s.",
      target: {
        platform: "k8s",
        namespace_or_region: "payments",
        resource_kind: "StatefulSet",
        resource_name: "ledger-worker",
      },
    },
    remediation_plan: {
      rationale:
        "Increase the memory ceiling within the OPA guardrail of 2Gi to stop the OOM crashloop.",
      steps: [
        {
          step: 1,
          tool_call_id: "call_mem_1",
          tool_name: "k8s_patch_resource_limits",
          tool_args: {
            namespace: "payments",
            kind: "StatefulSet",
            name: "ledger-worker",
            memory_limit: "1Gi",
          },
          blast_radius: "pod_level",
          reversible: true,
        },
      ],
    },
    policy_decision: {
      allowed: true,
      requires_approval: false,
      reason:
        "OPA policy: reversible pod_level limit increase within the ceiling was auto-approved.",
    },
    requires_approval: false,
    approval_status: "AUTO_APPROVED",
    approved: true,
    approved_by: "opa-auto",
    approval_reason: "Within the autonomous memory guardrail",
    is_interrupted: false,
    approval: {
      requires_approval: false,
      approval_status: "AUTO_APPROVED",
      approved: true,
      approved_by: "opa-auto",
      approval_reason: "Within the autonomous memory guardrail",
      is_interrupted: false,
    },
    execution_status: "SUCCESS",
    execution_results: [
      {
        step: 1,
        tool_name: "k8s_patch_resource_limits",
        status: "SUCCESS",
        duration_ms: 842,
        output:
          "statefulset.apps/ledger-worker patched (memory limit 512Mi to 1Gi), rollout healthy",
      },
    ],
    remediation_attempts: 1,
    max_remediation_attempts: 2,
    execution: {
      execution_status: "SUCCESS",
      results: [
        {
          step: 1,
          tool_name: "k8s_patch_resource_limits",
          status: "SUCCESS",
          duration_ms: 842,
          output:
            "statefulset.apps/ledger-worker patched (memory limit 512Mi to 1Gi), rollout healthy",
        },
      ],
      remediation_attempts: 1,
      max_remediation_attempts: 2,
    },
    verification_passed: true,
    verification_feedback:
      "All 3 StatefulSet replicas stayed Ready for 90s. Working set stabilized at 612Mi with zero OOM events.",
    verification: {
      verification_passed: true,
      verification_feedback:
        "All 3 StatefulSet replicas stayed Ready for 90s. Working set stabilized at 612Mi with zero OOM events.",
    },
    rollback_available: true,
    rollback_results: [],
    rollback: {
      rollback_available: true,
      rollback_results: [],
    },
    error: null,
    post_incident_report_ready: true,
    target: "payments/StatefulSet/ledger-worker",
    outcome: "success",
    timestamp: "2026-11-18T16:54:10Z",
  },
  {
    incident_id: "INC-9471",
    updated_at: "2026-11-18T16:12:04Z",
    platform: "k8s",
    namespace: "checkout",
    resource_kind: "Deployment",
    service_name: "cart-api",
    target_resource: "checkout/Deployment/cart-api",
    involved_object: "Pod/cart-api-78c4b8f9-9q2mx",
    involved_object_name: "cart-api-78c4b8f9-9q2mx",
    severity: "CRITICAL",
    status: "ROLLED_BACK",
    active_node: "ROLLBACK",
    next_nodes: [],
    triage_status: "TRIAGED",
    failure_class: "upstream_tls_cert_expired",
    suspected_cause:
      "ConfigMap reload did not clear an expired mTLS leaf certificate mounted from the external Vault agent.",
    confidence: 0.78,
    min_confidence_threshold: 0.65,
    can_auto_remediate: true,
    recommended_strategy:
      "Trigger a rolling restart to force Vault sidecar certificate rotation, and roll back if the error rate persists.",
    evidence: [
      "k8s_get_pod_logs: x509: certificate has expired or is not yet valid (issuer=payments-internal-ca)",
    ],
    rca_result: {
      failure_type: "upstream_tls_cert_expired",
      confidence: 0.78,
      can_auto_remediate: true,
      recommended_strategy:
        "Trigger a rolling restart to force Vault sidecar certificate rotation, and roll back if the error rate persists.",
      target: {
        platform: "k8s",
        namespace_or_region: "checkout",
        resource_kind: "Deployment",
        resource_name: "cart-api",
      },
    },
    remediation_plan: {
      rationale: "Restart Deployment/cart-api to refresh the mounted TLS bundle.",
      steps: [
        {
          step: 1,
          tool_call_id: "call_rst_1",
          tool_name: "k8s_rollout_restart",
          tool_args: { namespace: "checkout", deployment_name: "cart-api" },
          blast_radius: "service_wide",
          reversible: true,
        },
      ],
    },
    policy_decision: {
      allowed: true,
      requires_approval: true,
      reason: "OPA policy: production checkout restart approved by the on-call engineer.",
    },
    requires_approval: true,
    approval_status: "APPROVED",
    approved: true,
    approved_by: "sre-oncall",
    approval_reason: "Approved during the incident bridge",
    is_interrupted: false,
    approval: {
      requires_approval: true,
      approval_status: "APPROVED",
      approved: true,
      approved_by: "sre-oncall",
      approval_reason: "Approved during the incident bridge",
      is_interrupted: false,
    },
    execution_status: "SUCCESS",
    execution_results: [
      {
        step: 1,
        tool_name: "k8s_rollout_restart",
        status: "SUCCESS",
        duration_ms: 1410,
        output: "deployment.apps/cart-api restarted",
      },
    ],
    remediation_attempts: 2,
    max_remediation_attempts: 2,
    execution: {
      execution_status: "SUCCESS",
      results: [
        {
          step: 1,
          tool_name: "k8s_rollout_restart",
          status: "SUCCESS",
          duration_ms: 1410,
          output: "deployment.apps/cart-api restarted",
        },
      ],
      remediation_attempts: 2,
      max_remediation_attempts: 2,
    },
    verification_passed: false,
    verification_feedback:
      "HTTP 502 rate remained at 18% after attempt 2 of 2. The automatic deterministic rollback was triggered.",
    verification: {
      verification_passed: false,
      verification_feedback:
        "HTTP 502 rate remained at 18% after attempt 2 of 2. The automatic deterministic rollback was triggered.",
    },
    rollback_available: true,
    rollback_results: [
      {
        tool_name: "k8s_restore_revision",
        status: "SUCCESS",
        output:
          "Restored Deployment/cart-api to the pre-incident annotation snapshot and escalated to the security on-call.",
      },
    ],
    rollback: {
      rollback_available: true,
      rollback_results: [
        {
          tool_name: "k8s_restore_revision",
          status: "SUCCESS",
          output:
            "Restored Deployment/cart-api to the pre-incident annotation snapshot and escalated to the security on-call.",
        },
      ],
    },
    error:
      "Max closed-loop attempts (2 of 2) reached without clearing the x509 expiry. Rolled back and escalated.",
    post_incident_report_ready: true,
    target: "checkout/Deployment/cart-api",
    outcome: "rolled_back",
    timestamp: "2026-11-18T16:12:04Z",
  },
]

export const DEMO_PREDICTIONS: NexusPrediction[] = [
  {
    deployment: "shop-demo",
    namespace: "production",
    current_rps: 420,
    predicted_rps: 980,
    multiplier: 2.33,
    confidence: 0.91,
    mode: "autonomous",
    outcome: "success",
    decided_at: "2026-11-18T17:17:00Z",
    description:
      "Traffic on shop-demo is forecast to rise from 420 to 980 requests per second over the next 15 minutes.",
  },
  {
    deployment: "checkout-api",
    namespace: "checkout",
    current_rps: 185,
    predicted_rps: 310,
    multiplier: 1.68,
    confidence: 0.74,
    mode: "advisory",
    outcome: "pending",
    decided_at: "2026-11-18T17:15:30Z",
    description:
      "Traffic on checkout-api is forecast to rise from 185 to 310 requests per second over the next 15 minutes.",
  },
  {
    deployment: "ledger-worker",
    namespace: "payments",
    current_rps: 95,
    predicted_rps: 102,
    multiplier: 1.07,
    confidence: 0.58,
    mode: "shadow",
    outcome: "pending",
    decided_at: "2026-11-18T17:12:00Z",
    description:
      "Traffic on ledger-worker is forecast to rise from 95 to 102 requests per second over the next 15 minutes.",
  },
]

export const DEMO_PENDING_APPROVALS = [
  {
    approval_id: "appr_inc_9482",
    incident_id: "INC-9482",
    thread_id: "INC-9482",
    runbook_id: "k8s_rollback_deployment",
    action_type: "rollback",
    healing_level: 3,
    confidence: 0.94,
    target: "production/Deployment/shop-demo",
    reasons: [
      "Service-wide mutation in the production namespace",
      "Reversible rollback to the last known healthy revision",
    ],
    suggested_fix:
      "Roll back Deployment/shop-demo to revision 13 and verify readyReplicas reaches 3 of 3.",
    context: {
      platform: "k8s",
      namespace: "production",
      resource_kind: "Deployment",
      service_name: "shop-demo",
      target_resource: "production/Deployment/shop-demo",
      failure_class: "readiness_probe_exit_zero",
      suspected_cause:
        "Readiness probe on /healthz fails after the revision 14 rollout.",
    },
    enqueued_at: "2026-11-18T17:18:42Z",
  },
]

export const DEMO_AUDIT_RECORDS = [
  {
    action_id: "act_9482_rollback",
    incident_id: "INC-9482",
    timestamp: "2026-11-18T17:18:42Z",
    triggered_by: "nexus_workflow",
    runbook_id: "k8s_rollback_deployment",
    healing_level: 3,
    target: "production/Deployment/shop-demo",
    pre_check_results: { policy_allowed: true, requires_approval: true },
    execution_outcome: "pending",
    post_check_results: {},
    rollback_triggered: false,
    action_results: [],
  },
  {
    action_id: "act_9479_limit_patch",
    incident_id: "INC-9479",
    timestamp: "2026-11-18T16:54:10Z",
    triggered_by: "opa_auto",
    runbook_id: "k8s_patch_resource_limits",
    healing_level: 1,
    target: "payments/StatefulSet/ledger-worker",
    pre_check_results: { policy_allowed: true, requires_approval: false },
    execution_outcome: "success",
    post_check_results: { verification_passed: true },
    rollback_triggered: false,
    action_results: [],
  },
  {
    action_id: "act_9471_rollback",
    incident_id: "INC-9471",
    timestamp: "2026-11-18T16:12:04Z",
    triggered_by: "sre-oncall",
    runbook_id: "k8s_rollout_restart",
    healing_level: 3,
    target: "checkout/Deployment/cart-api",
    pre_check_results: { policy_allowed: true, requires_approval: true },
    execution_outcome: "rolled_back",
    post_check_results: { verification_passed: false, http_502_rate: 0.18 },
    rollback_triggered: true,
    action_results: [],
  },
]

export const DEMO_AGENT_RUNS = [
  {
    run_id: "8a1c0f52-1c1b-4f6a-9d4e-2b7a9c0e5d11",
    incident_id: "INC-9482",
    node_name: "DIAGNOSE",
    agent_type: "investigator",
    model_name: "gemini-2.5-pro",
    status: "completed",
    prompt_tokens: 18422,
    completion_tokens: 2317,
    total_tokens: 20739,
    duration_ms: 11480,
    error_message: null,
    created_at: "2026-11-18T17:17:58Z",
    finished_at: "2026-11-18T17:18:10Z",
  },
  {
    run_id: "3e7b9d14-77aa-4f0c-b6a1-9f3d2c8e4b20",
    incident_id: "INC-9482",
    node_name: "PLAN",
    agent_type: "planner",
    model_name: "gemini-2.5-pro",
    status: "completed",
    prompt_tokens: 9260,
    completion_tokens: 1140,
    total_tokens: 10400,
    duration_ms: 6210,
    error_message: null,
    created_at: "2026-11-18T17:18:11Z",
    finished_at: "2026-11-18T17:18:17Z",
  },
]

export const DEMO_AGENT_MESSAGES = [
  {
    message_id: "b2d4f8a0-1e33-4c8a-90f2-6c1e9b7d4a55",
    run_id: "8a1c0f52-1c1b-4f6a-9d4e-2b7a9c0e5d11",
    incident_id: "INC-9482",
    sequence_num: 1,
    role: "assistant",
    content:
      "Readiness probe failures started 4 minutes after the revision 14 rollout. The probe command exits non-zero on /healthz while the container reports running.",
    reasoning_content:
      "Correlating pod events with the Deployment revision history points at the new image rather than the ingress.",
    raw_payload: {},
    created_at: "2026-11-18T17:18:04Z",
  },
  {
    message_id: "c93a1b7e-5d02-4f19-8b33-2a7c6e1f0d88",
    run_id: "8a1c0f52-1c1b-4f6a-9d4e-2b7a9c0e5d11",
    incident_id: "INC-9482",
    sequence_num: 2,
    role: "tool",
    content:
      "k8s_describe_deployment: revision=14 readyReplicas=0 desiredReplicas=3",
    reasoning_content: null,
    raw_payload: {},
    created_at: "2026-11-18T17:18:08Z",
  },
]

export const DEMO_STATUS = {
  uptime_seconds: 7382.4,
  timestamp: "2026-11-18T17:19:04Z",
  mode: "langgraph_agent",
  workflow: {
    mode: "langgraph_agent",
    active_incidents: 1,
    pending_approvals: 1,
    incidents_processed: 214,
    last_rca_count: 37,
  },
  prescaler: {
    mode: "advisory",
    events_received: 12904,
    decisions_made: 812,
    skipped_confidence: 146,
    skipped_threshold: 91,
    skipped_cooldown: 23,
    precision: 0.874,
    smape: 6.42,
    ready_for_advisory: true,
  },
  learning: {
    cycles_run: 48,
    uptime_seconds: 7200,
    interval_s: 150,
    window_days: 30,
    last_kpis: {
      mean_time_to_remediate_s: 94.2,
      remediation_success_rate: 0.91,
      recurrence_rate: 0.07,
    },
    last_recommendations: 6,
  },
}
