from nexus.engine.workflow.state import IncidentState


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
    if state.get("approved") is True or state.get("approval_status") in (
        "APPROVED",
        "AUTO_APPROVED",
    ):
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
