export type IncidentStatus =
  | "RESOLVED"
  | "AWAITING_APPROVAL"
  | "ROLLED_BACK"
  | "ESCALATED"
  | "ACTIVE"
  | "PENDING"
  | "EXECUTION_FAILED"
  | "POLICY_DENIED"
  | "VERIFICATION_FAILED"
  | "UNPLANNABLE"
  | string

export type GraphNodeId =
  | "TRIAGE"
  | "DIAGNOSE"
  | "PLAN"
  | "GOVERN"
  | "APPROVAL"
  | "EXECUTE"
  | "VERIFY"
  | "LEARN"
  | "ROLLBACK"
  | "ESCALATE"

export interface GraphNodeDefinition {
  id: GraphNodeId
  label: string
}

export interface RemediationStep {
  step: number
  tool_call_id?: string
  tool_name: string
  tool_args?: Record<string, unknown>
  parameters?: Record<string, unknown>
  blast_radius?: "pod_level" | "service_wide" | "cluster_wide" | string
  reversible?: boolean
}

export interface PolicyDecision {
  allowed: boolean
  requires_approval: boolean
  reason: string
}

export interface ApprovalState {
  is_interrupted: boolean
  approval_status: "PENDING" | "APPROVED" | "REJECTED" | "AUTO_APPROVED" | string
  approved: boolean | null
  approved_by: string | null
  approval_reason: string | null
}

export interface ExecutionResult {
  step: number
  tool_name: string
  status: "SUCCESS" | "FAILED" | "PENDING" | string
  duration_ms?: number
  output: string
}

export interface VerificationState {
  verification_passed: boolean | null
  verification_feedback: string
}

export interface RollbackResult {
  tool_name: string
  status: "SUCCESS" | "FAILED" | string
  output: string
}

export interface RollbackState {
  rollback_available: boolean
  results: RollbackResult[]
}

export interface Incident {
  incident_id: string
  updated_at: string
  platform: string
  namespace: string
  resource_kind: string
  service_name: string
  target_resource: string
  involved_object: string
  status: IncidentStatus
  active_node: GraphNodeId | string
  triage_status: string
  failure_class: string
  suspected_cause: string
  confidence: number
  min_confidence_threshold: number
  can_auto_remediate: boolean
  recommended_strategy: string
  evidence: (string | Record<string, unknown>)[]
  remediation_plan: {
    rationale: string
    steps: RemediationStep[]
  }
  policy_decision: PolicyDecision
  approval: ApprovalState
  execution: {
    execution_status: string
    results: ExecutionResult[]
    remediation_attempts: number
    max_remediation_attempts: number
  }
  verification: VerificationState
  rollback: RollbackState
  post_incident_report_ready: boolean
  error: string | null
}

export interface PredictionDecision {
  target: string
  namespace: string
  current_rps: number
  predicted_rps: number
  scale_factor: number
  desired_replicas: number
  mode: "autonomous" | "advisory" | "shadow" | string
  timestamp: string
}

export const GRAPH_NODES: GraphNodeDefinition[] = [
  { id: "TRIAGE", label: "Triage" },
  { id: "DIAGNOSE", label: "Diagnose" },
  { id: "PLAN", label: "Plan" },
  { id: "GOVERN", label: "Govern" },
  { id: "APPROVAL", label: "Approval" },
  { id: "EXECUTE", label: "Execute" },
  { id: "VERIFY", label: "Verify" },
  { id: "LEARN", label: "Learn" },
]
