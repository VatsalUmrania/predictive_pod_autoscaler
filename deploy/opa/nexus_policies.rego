package nexus.governance

import rego.v1

default decision := "REQUIRE_APPROVAL"
default allow := false
default require_human_approval := false
default valid_input := false

valid_input if {
    is_object(input)

    is_boolean(input.restricted_security_service)
    is_boolean(input.is_destructive)
    is_boolean(input.reversible)
    is_boolean(input.cooldown_active)
    is_boolean(input.circuit_breaker_tripped)
    is_boolean(input.has_rollback_plan)

    is_string(input.action_category)
    is_string(input.blast_radius)

    is_number(input.rca_confidence)
    is_number(input.estimated_cost_delta_pct)
}

deny_reasons contains "Missing or invalid governance input" if {
    not valid_input
}

deny_reasons contains "Mutations on security/identity services (IAM/KMS/STS/Secrets) are strictly prohibited" if {
    valid_input
    input.restricted_security_service == true
}

deny_reasons contains "Destructive or irreversible mutations on stateful data stores are prohibited" if {
    input.blast_radius == "stateful_data"
    input.action_category == "destructive"
}

deny_reasons contains "Tool is destructive — blocked by zero-trust governance" if {
    input.action_category == "destructive"
}

deny_reasons contains "Tool is destructive — blocked by zero-trust governance" if {
    input.is_destructive == true
}

deny_reasons contains "Tool has account_wide blast radius" if {
    input.blast_radius == "account_wide"
}

deny_reasons contains "Mutating action is missing an explicit rollback tool call" if {
    input.has_rollback_plan == false
    input.allows_unbacked_rollback != true
}

deny_reasons contains "Circuit breaker is tripped for target resource" if {
    input.circuit_breaker_tripped == true
}

deny_reasons contains "Resource is currently in remediation cooldown window" if {
    input.cooldown_active == true
}

# AST path inspection rules (Plane 4)
deny_reasons contains "Scale to zero replicas is prohibited" if {
    some path in input.mutated_paths
    startswith(path, "/spec/replicas")
    input.patch_payload.spec.replicas == 0
}

deny_reasons contains "Mutating system/immutable labels is prohibited" if {
    some path in input.mutated_paths
    startswith(path, "/metadata/labels/")
}

# Auto-approval conditions
allow_auto_conditions_met if {
    input.allow_auto == true
    input.blast_radius == "pod"
    input.rca_confidence >= 0.95
    input.is_destructive == false
}

# Strict Precedence: DENY > ALLOW_AUTO > REQUIRE_APPROVAL
decision := "DENY" if {
    count(deny_reasons) > 0
} else := "ALLOW_AUTO" if {
    allow_auto_conditions_met
} else := "REQUIRE_APPROVAL"

allow if {
    decision in ["REQUIRE_APPROVAL", "ALLOW_AUTO"]
}

require_human_approval if {
    decision == "REQUIRE_APPROVAL"
}
