# NEXUS — OPA Policy Definitions
# ================================
# Rego policy that governs all autonomous healing actions in NEXUS.
#
# Deploy:
#   docker run -d --name nexus-opa -p 8181:8181 \
#     -v $(pwd)/deploy/opa:/policies openpolicyagent/opa:latest \
#     run --server /policies
#
# Test:
#   echo '{"input": {"action": {"type":"restart_pod","level":1,"blast_radius":"single_pod","in_cooldown":false,"governance_cb_open":false,"confidence":0.9,"human_approved":false}}}' \
#   | curl -s -X POST localhost:8181/v1/data/nexus/allow_action -H "Content-Type: application/json" -d @-
#
# Input schema (all fields required):
#   input.action.type               — action type key (string)
#   input.action.level              — healing level 0-3 (int)
#   input.action.blast_radius       — blast radius label (string)
#   input.action.in_cooldown        — true if in cooldown window (bool)
#   input.action.governance_cb_open — true if governance CB is tripped (bool)
#   input.action.confidence         — orchestrator confidence 0.0-1.0 (float)
#   input.action.human_approved     — true if a human has approved this L3 action (bool)
#   input.action.override_blast_radius — true to bypass cluster_wide blast radius block (bool)

package nexus

import future.keywords.in

# ── Default deny ──────────────────────────────────────────────────────────────

default allow_action = false
default requires_human_approval = false
default allow = false
default require_human_approval = false

# ── Action type allowlists per healing level ──────────────────────────────────

# Two execution vocabularies share one ladder:
#   - runbook executor actions (restart_pod, scale_deployment, kubectl_rollout_undo …)
#   - LLM-proposed tools       (restart_deployment, scale_resource, cordon_node …)
# Every LLM tool is mapped onto the level it requires so a routed-through-governance
# LLM proposal isn't denied at the allowlist gate. (The level mapping itself
# lives in policy_engine.py LLM_TOOL_LEVEL — input shaping, not policy.)
l0_actions = {
    "emit_alert",
    "patch_annotation"
}

l1_actions = l0_actions | {
    "restart_pod",
    "flush_coredns_cache",
    "restart_deployment"
}

l2_actions = l1_actions | {
    "scale_deployment",
    "scale_resource"
}

l3_actions = l2_actions | {
    "kubectl_rollout_undo",
    "http_webhook",
    "rollback_deployment",
    "patch_configmap",
    "cordon_node",
    "drain_node"
}

# Check if action is an autonomous registered tool (k8s_ or aws_ or has tool_name)
is_autonomous_tool {
    input.action.tool_name != ""
}

is_autonomous_tool {
    startswith(input.action.type, "k8s_")
}

is_autonomous_tool {
    startswith(input.action.type, "aws_")
}

# Action type is in the allowlist for this level or is a registered autonomous tool
allowed_action_type {
    is_autonomous_tool
}

allowed_action_type {
    input.action.level == 0
    input.action.type in l0_actions
}

allowed_action_type {
    input.action.level == 1
    input.action.type in l1_actions
}

allowed_action_type {
    input.action.level == 2
    input.action.type in l2_actions
}

allowed_action_type {
    input.action.level == 3
    input.action.type in l3_actions
}

# Blast radius is within acceptable bounds
blast_radius_ok {
    input.action.blast_radius != "cluster_wide"
    input.action.blast_radius != "account_wide"
}

blast_radius_ok {
    input.action.blast_radius == "cluster_wide"
    input.action.override_blast_radius == true
}

# ── Zero-Trust Safety Checks ──────────────────────────────────────────────────

not_destructive {
    not input.action.is_destructive
}

cb_closed {
    not input.action.governance_cb_open
    not input.context.circuit_breaker_open
}

not_in_cooldown {
    not input.action.in_cooldown
    not input.context.cooldown_active
}

# ── Human approval rules ──────────────────────────────────────────────────────

# Legacy L3 approval
requires_human_approval {
    input.action.level == 3
    input.action.confidence < 0.85
    not input.action.human_approved
}

# Production environment with service-wide blast radius requires approval
requires_human_approval {
    input.context.environment == "production"
    input.action.blast_radius == "service_wide"
    not input.action.human_approved
    not input.context.operator_confirmed
}

# Missing rollback when unbacked rollback is not allowed requires approval
requires_human_approval {
    input.action.allows_unbacked_rollback == false
    not input.action.has_rollback
    not input.action.human_approved
    not input.context.operator_confirmed
}

require_human_approval {
    requires_human_approval
}

# ── Main allow rules ──────────────────────────────────────────────────────────

# Autonomous tools: allowed when not destructive, blast radius ok, not in cooldown, and CB closed
allow_action {
    is_autonomous_tool
    not_destructive
    blast_radius_ok
    not_in_cooldown
    cb_closed
}

# L0: always allowed (zero blast radius — alert + annotate only)
allow_action {
    input.action.level == 0
    allowed_action_type
    not_destructive
}

# L1: allowed when not in cooldown and governance CB is closed
allow_action {
    input.action.level == 1
    allowed_action_type
    not_destructive
    not_in_cooldown
    cb_closed
}

# L2: L1 conditions + blast-radius check
allow_action {
    input.action.level == 2
    allowed_action_type
    not_destructive
    not_in_cooldown
    cb_closed
    blast_radius_ok
}

# L3: L2 conditions + confidence >= 0.85 (no human approval needed)
allow_action {
    input.action.level == 3
    allowed_action_type
    not_destructive
    not_in_cooldown
    cb_closed
    blast_radius_ok
    input.action.confidence >= 0.85
}

# L3 bypass: explicit human approval overrides the confidence gate
allow_action {
    input.action.level == 3
    allowed_action_type
    not_destructive
    not_in_cooldown
    cb_closed
    blast_radius_ok
    input.action.human_approved == true
}

allow {
    allow_action
}

# ── Deny reasons (for audit trail and debugging) ──────────────────────────────

deny_reasons[reason] {
    input.action.is_destructive == true
    reason = "action_is_destructive_blocked"
}

deny_reasons[reason] {
    input.action.blast_radius == "account_wide"
    reason = "blast_radius_account_wide_blocked"
}

deny_reasons[reason] {
    not allowed_action_type
    reason = "action_type_not_in_allowlist_for_this_healing_level"
}

deny_reasons[reason] {
    input.action.in_cooldown
    reason = "action_in_cooldown"
}

deny_reasons[reason] {
    input.context.cooldown_active
    reason = "action_in_cooldown"
}

deny_reasons[reason] {
    input.action.governance_cb_open
    reason = "governance_circuit_breaker_open_stop_autonomous_healing"
}

deny_reasons[reason] {
    input.context.circuit_breaker_open
    reason = "governance_circuit_breaker_open_stop_autonomous_healing"
}

deny_reasons[reason] {
    input.action.level >= 2
    input.action.blast_radius == "cluster_wide"
    not input.action.override_blast_radius
    reason = "blast_radius_cluster_wide_not_allowed_without_override"
}

deny_reasons[reason] {
    input.action.level == 3
    input.action.confidence < 0.85
    not input.action.human_approved
    reason = "l3_action_requires_confidence_0.85_or_human_approval"
}

# ── Composite Governance Output ───────────────────────────────────────────────

governance = {
    "allow": allow,
    "require_human_approval": require_human_approval,
    "deny_reasons": deny_reasons
}
