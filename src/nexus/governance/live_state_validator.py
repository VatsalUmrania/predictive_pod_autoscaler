"""NEXUS Live State Validator (Facade).

Backward-compatible entry point re-exporting check_live_state and models.
"""

from nexus.governance.live_state import (
    _ALWAYS_PROCEED,
    _ALWAYS_PROCEED_UNVERIFIABLE,
    LiveStateReport,
    check_live_state,
)

__all__ = [
    "LiveStateReport",
    "_ALWAYS_PROCEED",
    "_ALWAYS_PROCEED_UNVERIFIABLE",
    "check_live_state",
]
