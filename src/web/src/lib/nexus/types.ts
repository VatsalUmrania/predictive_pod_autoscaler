/**
 * Types that mirror the NEXUS API responses exactly.
 *
 * Every field here is returned by the backend. Nothing is invented, and no field
 * carries a default that would fabricate a value the API did not send.
 *
 * Sources:
 *   GET /developer/incidents        -> nexus.services.observability.dashboard.dashboard_incidents.serialize_incident_for_dashboard
 *   GET /developer/predictions      -> nexus.services.observability.dashboard.dashboard_predictions.get_predictions
 *   GET /status, GET /health         -> nexus.services.api.routes.status_routes
 *   GET /approvals/pending           -> nexus.services.api.routes.status_governance
 *   GET /audit/tail                  -> nexus.governance.audit.audit_trail
 */

/**
 * One step of a remediation plan. The API returns free-form dictionaries here, so the
 * known keys are typed and anything else is preserved.
 */
export interface NexusRemediationStep {
  step?: number
  tool_call_id?: string
  tool_name?: string
  tool_args?: Record<string, unknown>
  parameters?: Record<string, unknown>
  blast_radius?: string
  reversible?: boolean
  [key: string]: unknown
}

export interface NexusExecutionResult {
  step?: number
  tool_name?: string
  status?: string
  duration_ms?: number
  output?: string
  [key: string]: unknown
}

export interface NexusRollbackResult {
  tool_name?: string
  original_tool?: string
  rollback_tool?: string
  rollback_args?: Record<string, unknown>
  status?: string
  output?: string
  [key: string]: unknown
}

export interface NexusIncident {
  incident_id: string
  updated_at: string | null
  platform: string
  namespace: string | null
  resource_kind: string | null
  service_name: string | null
  target_resource: string
  involved_object: string | null
  involved_object_name: string | null
  severity: string
  status: string
  active_node: string
  next_nodes: string[]
  triage_status: string
  failure_class: string
  suspected_cause: string
  confidence: number
  min_confidence_threshold: number
  can_auto_remediate: boolean
  recommended_strategy: string
  evidence: Array<string | Record<string, unknown>>
  rca_result: Record<string, unknown>
  remediation_plan: {
    rationale: string
    steps: NexusRemediationStep[]
  }
  policy_decision: {
    allowed: boolean
    requires_approval: boolean
    reason: string
  }
  requires_approval: boolean
  approval_status: string
  approved: boolean | null
  approved_by: string | null
  approval_reason: string | null
  is_interrupted: boolean
  approval: {
    requires_approval: boolean
    approval_status: string
    approved: boolean | null
    approved_by: string | null
    approval_reason: string | null
    is_interrupted: boolean
  }
  execution_status: string
  execution_results: NexusExecutionResult[]
  remediation_attempts: number
  max_remediation_attempts: number
  execution: {
    execution_status: string
    results: NexusExecutionResult[]
    remediation_attempts: number
    max_remediation_attempts: number
  }
  verification_passed: boolean | null
  verification_feedback: string
  verification: {
    verification_passed: boolean | null
    verification_feedback: string
  }
  rollback_available: boolean
  rollback_results: NexusRollbackResult[]
  rollback: {
    rollback_available: boolean
    rollback_results: NexusRollbackResult[]
  }
  error: string | null
  post_incident_report_ready: boolean
  target: string
  outcome: string
  timestamp: string
}

export interface NexusPrediction {
  deployment: string
  namespace: string
  current_rps: number
  predicted_rps: number
  multiplier: number
  confidence: number
  mode: string
  outcome: string
  decided_at: string
  description: string
}

export interface NexusWorkflowStatus {
  mode: string
  active_incidents: number
  pending_approvals: number
  incidents_processed: number
  last_rca_count: number
}

export interface NexusPrescalerStatus {
  mode: string
  events_received: number
  decisions_made: number
  skipped_confidence: number
  skipped_threshold: number
  skipped_cooldown: number
  precision: number
  smape: number | null
  ready_for_advisory: boolean
}

export interface NexusLearningStatus {
  cycles_run: number
  uptime_seconds: number
  interval_s: number
  window_days: number
  last_kpis: Record<string, unknown> | null
  last_recommendations: number
}

export interface NexusStatus {
  uptime_seconds: number
  timestamp: string
  mode?: string
  workflow?: NexusWorkflowStatus
  orchestrator?: Record<string, unknown>
  prescaler?: NexusPrescalerStatus
  learning?: NexusLearningStatus
}

export interface NexusHealth {
  status: string
  timestamp: string
  uptime_seconds: number
}

/**
 * `/approvals/pending` merges two different record shapes: LangGraph interrupt
 * payloads and HumanApprovalQueue entries. Only `approval_id` and `enqueued_at` are
 * common to both, so everything else is optional.
 */
export interface NexusPendingApproval {
  approval_id?: string
  incident_id?: string
  thread_id?: string
  runbook_id?: string
  action_type?: string
  healing_level?: number
  confidence?: number
  target?: string
  reasons?: string[]
  suggested_fix?: string
  context?: Record<string, unknown>
  enqueued_at?: string
}

export interface NexusAuditRecord {
  action_id: string
  incident_id: string | null
  timestamp: string
  triggered_by: string
  runbook_id: string
  healing_level: number
  target: string | null
  pre_check_results: Record<string, unknown> | null
  execution_outcome: string
  post_check_results: Record<string, unknown> | null
  rollback_triggered: boolean
  action_results: unknown[] | null
}

export interface NexusAgentRun {
  run_id: string
  incident_id: string
  node_name: string | null
  agent_type: string
  model_name: string
  status: string
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  duration_ms: number | null
  error_message: string | null
  created_at: string
  finished_at: string | null
}

export interface NexusAgentMessage {
  message_id: string
  run_id: string
  incident_id: string
  sequence_num: number
  role: string
  content: string | null
  reasoning_content: string | null
  raw_payload: Record<string, unknown> | null
  created_at: string
}

export interface NexusIncidentReport {
  incident_id: string
  target: string
  outcome: string
  generated_at: string
  report: Record<string, unknown>
}

export interface NexusDecisionResponse {
  status: string
  incident_id: string
  approved: boolean
  approved_by: string
  reason: string
  outcome: Record<string, unknown>
}

export interface NexusPrescalerModeResponse {
  status: string
  result: string
}

export interface NexusPolicy {
  [key: string]: unknown
  _overrides_active?: boolean
  _app?: string
}

/** Graph nodes the LangGraph workflow can be parked on. */
export const GRAPH_NODES = [
  "TRIAGE",
  "DIAGNOSE",
  "PLAN",
  "GOVERN",
  "APPROVAL",
  "EXECUTE",
  "VERIFY",
  "LEARN",
  "ROLLBACK",
  "ESCALATE",
] as const

export type GraphNodeId = (typeof GRAPH_NODES)[number]

/** Statuses that mean the incident reached a terminal, failed state. */
export const FAILURE_STATUSES: readonly string[] = [
  "ESCALATED",
  "ROLLED_BACK",
  "EXECUTION_FAILED",
  "POLICY_DENIED",
  "VERIFICATION_FAILED",
  "UNPLANNABLE",
]

/**
 * Statuses that mean the incident was deliberately closed out rather than
 * healed or failed. `IGNORED` is terminal — the triage node drops the incident
 * and the graph ends — so it must not be counted as still in flight.
 */
export const IGNORED_STATUSES: readonly string[] = ["IGNORED"]

/** Statuses that mean the incident is still moving through the graph. */
export const ACTIVE_STATUSES: readonly string[] = ["ACTIVE", "PENDING"]

/** Every terminal status: resolved, failed, or deliberately ignored. */
export const TERMINAL_STATUSES: readonly string[] = [
  "RESOLVED",
  ...FAILURE_STATUSES,
  ...IGNORED_STATUSES,
]

/** Statuses that mean the incident needs an operator to act. */
export const ATTENTION_STATUSES: readonly string[] = [
  "AWAITING_APPROVAL",
  "PENDING_APPROVAL",
]
