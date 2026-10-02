"""
NEXUS Git Agent Facade
======================
Backward-compatibility facade for GitAgent and related helpers.
"""

from nexus.agents.git import (
    EnvContractValidator,
    GitAgent,
    extract_env_keys_js,
    extract_env_keys_python,
    scan_diff_for_secrets,
)

__all__ = [
    "GitAgent",
    "EnvContractValidator",
    "scan_diff_for_secrets",
    "extract_env_keys_python",
    "extract_env_keys_js",
]
