package nexus.governance

import rego.v1

default decision := "DENY"
default allow := false
default require_human_approval := false
default valid_input := false
default auto_allowed := false

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

deny_reasons contains "Circuit breaker is tripped for target resource" if {
    input.circuit_breaker_tripped == true
}

auto_allowed if {
    count(deny_reasons) == 0
    valid_input

    input.reversible == true
    input.action_category in {"restart", "scale"}
    input.blast_radius == "pod_or_instance"

    input.rca_confidence >= 0.85
    input.cooldown_active == false
    input.estimated_cost_delta_pct <= 25.0

    input.has_rollback_plan == true
}

decision := "ALLOW_AUTO" if {
    auto_allowed
}

decision := "REQUIRE_APPROVAL" if {
    count(deny_reasons) == 0
    not auto_allowed
}

allow if {
    decision in {"ALLOW_AUTO", "REQUIRE_APPROVAL"}
}

require_human_approval if {
    decision == "REQUIRE_APPROVAL"
}
