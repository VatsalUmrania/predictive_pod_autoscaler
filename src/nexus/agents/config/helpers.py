"""
Config Normalization and Checksum Helpers
"""

from __future__ import annotations

import hashlib
import json


def compute_checksum(data: dict) -> str:
    """Stable SHA-256 of a dict (sorted keys for determinism)."""
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def normalise_deployment(dep_dict: dict) -> dict:
    """
    Strip K8s dynamic fields before drift comparison.
    We only care about user-controlled spec fields.
    """
    spec = dep_dict.get("spec", {})
    template = spec.get("template", {}).get("spec", {})
    return {
        "replicas": spec.get("replicas"),
        "image": [c.get("image") for c in template.get("containers", [])],
        "env": [
            {e.get("name"): e.get("value")}
            for c in template.get("containers", [])
            for e in c.get("env", [])
            if "value" in e
        ],
        "resources": [c.get("resources") for c in template.get("containers", [])],
    }
