"""
Dynamic context extraction and parameter computation helpers for Kubernetes remediation planning.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def extract_live_replicas(state: dict[str, Any]) -> int:
    """Extract current replica count from live telemetry or configuration."""
    live_cfg = state.get("telemetry", {}).get("live_config", {})
    if "replicas" in live_cfg and isinstance(live_cfg["replicas"], int):
        return max(1, live_cfg["replicas"])

    metrics = state.get("telemetry", {}).get("metrics", {})
    if "current_replicas" in metrics and isinstance(metrics["current_replicas"], int):
        return max(1, metrics["current_replicas"])

    desc = live_cfg.get("describe", "")
    if desc:
        m = re.search(r"Replicas:\s*(\d+)", desc)
        if m:
            return max(1, int(m.group(1)))

    return 1


def calculate_scaled_replicas(
    current_replicas: int,
    metrics: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> int:
    """Dynamically compute target scaled replicas based on load ratio or safe step."""
    metrics = metrics or {}
    events = events or []

    ratio = float(metrics.get("traffic_ratio", 0.0) or metrics.get("load_ratio", 0.0))
    if ratio > 1.0:
        scaled = int(current_replicas * ratio)
    else:
        has_heavy_surge = any(
            e.get("signal_type") in ("traffic_spike", "load_surge")
            or "spike" in str(e.get("message", "")).lower()
            for e in events
        )
        scaled = (
            max(current_replicas + 2, current_replicas * 2)
            if has_heavy_surge
            else max(current_replicas + 1, current_replicas * 2)
        )

    # Bound safely within [1, 10]
    return min(10, max(1, scaled))


def extract_live_memory_limit(state: dict[str, Any]) -> str | None:
    """Extract current container memory limit from telemetry."""
    live_cfg = state.get("telemetry", {}).get("live_config", {})
    if "memory_limit" in live_cfg and live_cfg["memory_limit"]:
        return str(live_cfg["memory_limit"])

    desc = live_cfg.get("describe", "")
    if desc:
        m = re.search(r"(?:Limits:.*?|Requests:.*?)?memory:\s*([0-9]+(?:Mi|Gi|Ki|M|G|K))", desc, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)

    return None


def calculate_next_memory_limit(current_mem: str | None) -> str:
    """Scale memory limit dynamically (e.g. 256Mi -> 512Mi -> 1Gi)."""
    if not current_mem:
        return "512Mi"

    m = re.search(r"^(\d+)\s*([a-zA-Z]+)?$", current_mem.strip())
    if not m:
        return "512Mi"

    val = int(m.group(1))
    unit = (m.group(2) or "Mi").lower()

    if "gi" in unit or unit == "g":
        return f"{min(8, max(val + 1, val * 2))}Gi"

    # Default Mi
    if val <= 128:
        return "256Mi"
    if val <= 256:
        return "512Mi"
    if val <= 512:
        return "1Gi"
    return f"{min(8, (val // 1024 + 1) * 2)}Gi"


def detect_faulty_command(state: dict[str, Any]) -> tuple[bool, str | None]:
    """Detect entrypoint or command override failures."""
    live_cfg = state.get("telemetry", {}).get("live_config", {})
    recent_logs = " ".join(state.get("telemetry", {}).get("recent_logs", [])).lower()
    desc = live_cfg.get("describe", "").lower()

    err_signals = ("exec format error", "command not found", "executable file not found")
    if any(sig in recent_logs or sig in desc for sig in err_signals) or live_cfg.get("exit_code") == 127:
        container = live_cfg.get("container_name")
        return True, container

    return False, None


def detect_oom_issue(state: dict[str, Any], diagnosis: dict[str, Any]) -> bool:
    """Detect if incident is caused by OOMKilled / memory exhaustion."""
    root_cause = diagnosis.get("root_cause", "").lower()
    if any(sig in root_cause for sig in ("oom", "memory limit", "out of memory", "137")):
        return True

    telemetry = state.get("telemetry", {})
    if telemetry.get("health_status") == "oomkilled":
        return True

    live_cfg = telemetry.get("live_config", {})
    if live_cfg.get("exit_code") == 137:
        return True

    recent_logs = " ".join(telemetry.get("recent_logs", [])).lower()
    desc = live_cfg.get("describe", "").lower()
    if any(sig in recent_logs or sig in desc for sig in ("oomkilled", "exit code 137", "killed process")):
        return True

    events = state.get("events", [])
    return any(e.get("signal_type") in ("pod_oomkilled", "oom_event", "oom_signal") for e in events)


def detect_traffic_spike(state: dict[str, Any], diagnosis: dict[str, Any]) -> bool:
    """Detect if incident is caused by traffic surge or cascading load."""
    if diagnosis.get("failure_class") == "cascading_failure":
        return True

    root_cause = diagnosis.get("root_cause", "").lower()
    if any(sig in root_cause for sig in ("traffic", "spike", "surge", "high error rate", "overload")):
        return True

    events = state.get("events", [])
    if any(e.get("signal_type") in ("traffic_spike", "load_surge", "elevated_error_rate", "high_error_rate") for e in events):
        return True

    metrics = state.get("telemetry", {}).get("metrics", {})
    return float(metrics.get("traffic_ratio", 0.0) or 0.0) > 1.2 or float(metrics.get("error_rate", 0.0) or 0.0) > 0.05
