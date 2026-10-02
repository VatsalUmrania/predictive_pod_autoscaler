"""
Confidence Scorer Calibration Constants
"""

from __future__ import annotations

# Additive adjustments by failure class
_CLASS_ADJUSTMENTS = {
    "config_error": +0.05,
    "bad_deploy": 0.00,
    "resource_exhaustion": 0.00,
    "dependency_failure": -0.05,
    "cascading_failure": -0.10,
    "unknown": -0.20,
}

# Healing level caps without quorum
_MAX_CONFIDENCE_WITHOUT_QUORUM = {
    0: 1.0,
    1: 0.90,
    2: 0.80,
    3: 0.75,
}
