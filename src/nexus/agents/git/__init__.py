"""
NEXUS Git Agent Package
"""

from nexus.agents.git.agent import GitAgent
from nexus.agents.git.env_contract import EnvContractValidator
from nexus.agents.git.env_scanner import (
    extract_env_keys_js,
    extract_env_keys_python,
)
from nexus.agents.git.secrets import scan_diff_for_secrets

__all__ = [
    "GitAgent",
    "EnvContractValidator",
    "scan_diff_for_secrets",
    "extract_env_keys_python",
    "extract_env_keys_js",
]
