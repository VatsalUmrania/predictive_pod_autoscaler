# NEXUS — OPA Policy Definitions
# ================================
# Rego policy that governs all autonomous healing actions in NEXUS.
#
# This file is the authoritative policy source. The Python fallback in
# policy_engine.py mirrors these rules exactly and is used when OPA
# is unreachable.
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

# ── Default deny ──────────────────────────────────────────────────────────────

default allow_action = false
default requires_human_approval = false

# ── Action type allowlists per healing level ──────────────────────────────────

l0_actions = {
    "emit_alert",
    "patch_annotation"
}

l1_actions = l0_actions | {
    "restart_pod",
    "flush_coredns_cache"
}

l2_actions = l1_actions | {
    "scale_deployment"
}

l3_actions = l2_actions | {
    "kubectl_rollout_undo",
    "http_webhook"
}

# ── Helper rules ──────────────────────────────────────────────────────────────

# Action type is in the allowlist for this level
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
}

blast_radius_ok {
    input.action.blast_radius == "cluster_wide"
    input.action.override_blast_radius == true
}

# ── L3 human approval ─────────────────────────────────────────────────────────

requires_human_approval {
    input.action.level == 3
    input.action.confidence < 0.85
    not input.action.human_approved
}

# ── Main allow rules ──────────────────────────────────────────────────────────

# L0: always allowed (zero blast radius — alert + annotate only)
allow_action {
    input.action.level == 0
    allowed_action_type
}

# L1: allowed when not in cooldown and governance CB is closed
allow_action {
    input.action.level == 1
    allowed_action_type
    not input.action.in_cooldown
    not input.action.governance_cb_open
}

# L2: L1 conditions + blast-radius check
allow_action {
    input.action.level == 2
    allowed_action_type
    not input.action.in_cooldown
    not input.action.governance_cb_open
    blast_radius_ok
}

# L3: L2 conditions + confidence >= 0.85 (no human approval needed)
allow_action {
    input.action.level == 3
    allowed_action_type
    not input.action.in_cooldown
    not input.action.governance_cb_open
    blast_radius_ok
    input.action.confidence >= 0.85
}

# L3 bypass: explicit human approval overrides the confidence gate
allow_action {
    input.action.level == 3
    allowed_action_type
    not input.action.in_cooldown
    not input.action.governance_cb_open
    blast_radius_ok
    input.action.human_approved == true
}

# ── Deny reasons (for audit trail and debugging) ──────────────────────────────

deny_reasons[reason] {
    not allowed_action_type
    reason = "action_type_not_in_allowlist_for_this_healing_level"
}

deny_reasons[reason] {
    input.action.in_cooldown
    reason = "action_in_cooldown"
}

deny_reasons[reason] {
    input.action.governance_cb_open
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
