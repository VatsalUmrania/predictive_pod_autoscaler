#!/usr/bin/env python3
"""
FIX (PR#16): Auto-retraining controller for concept drift recovery.

This module can be run as a Kubernetes CronJob to automatically trigger
retraining when model drift is detected and sustained.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("ppa.retraining")

# Configuration from environment/config
DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "50.0"))
DRIFT_DURATION_MINUTES = int(os.getenv("DRIFT_DURATION_MINUTES", "60"))
NAMESPACE = os.getenv("PPA_NAMESPACE", "default")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")


def check_active_drift() -> list[dict]:
    """
    Query Prometheus for CRs with sustained severe drift.

    Returns list of CRs that need retraining.
    """
    try:
        # Query for severe drift detected
        query = "ppa_concept_drift_detected == 1"
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=10
        )
        resp.raise_for_status()
        result = resp.json()

        crs_needing_retraining = []
        for item in result.get("data", {}).get("result", []):
            metric = item.get("metric", {})
            cr_name = metric.get("cr_name")
            namespace = metric.get("namespace")

            # Check error percentage
            error_query = f'ppa_prediction_error_pct{{cr_name="{cr_name}",namespace="{namespace}"}}'
            error_resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": error_query},
                timeout=10,
            )
            error_resp.raise_for_status()
            error_result = error_resp.json()

            if error_result.get("data", {}).get("result"):
                error_pct = float(error_result["data"]["result"][0]["value"][1])
                if error_pct > DRIFT_THRESHOLD:
                    crs_needing_retraining.append(
                        {
                            "cr_name": cr_name,
                            "namespace": namespace,
                            "error_pct": error_pct,
                            "detected_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )

        return crs_needing_retraining
    except Exception as exc:
        logger.error(f"Failed to check drift: {exc}")
        return []


def create_retraining_job(cr_info: dict) -> bool:
    """
    Create a Kubernetes Job to retrain the model for a specific CR.

    Args:
        cr_info: Dict with cr_name, namespace, error_pct

    Returns:
        True if job created successfully
    """
    from kubernetes import client
    from kubernetes import config as k8s_config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()

    batch_v1 = client.BatchV1Api()

    job_name = f"ppa-retrain-{cr_info['cr_name']}-{int(time.time())}"
    job_name = job_name.replace("_", "-").lower()[:63]  # DNS-1123 compliance

    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": cr_info["namespace"],
            "labels": {
                "app": "ppa-retraining",
                "cr-name": cr_info["cr_name"],
                "trigger": "auto-drift",
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 86400,  # Clean up after 24h
            "backoffLimit": 2,
            "template": {
                "spec": {
                    "restartPolicy": "OnFailure",
                    "containers": [
                        {
                            "name": "retraining",
                            "image": os.getenv(
                                "RETRAINING_IMAGE", "ppa-ml-pipeline:latest"
                            ),
                            "command": ["python", "-m", "model.train"],
                            "args": [
                                "--app",
                                cr_info["cr_name"],
                                "--horizon",
                                "rps_t3m",
                                "--evaluate-before-promote",
                                "true",
                                "--rollback-on-failure",
                                "true",
                            ],
                            "env": [
                                {"name": "PROMETHEUS_URL", "value": PROMETHEUS_URL},
                                {"name": "MODEL_OUTPUT_PATH", "value": "/models"},
                            ],
                            "resources": {
                                "requests": {"memory": "2Gi", "cpu": "1000m"},
                                "limits": {"memory": "4Gi", "cpu": "2000m"},
                            },
                        }
                    ],
                }
            },
        },
    }

    try:
        batch_v1.create_namespaced_job(
            namespace=cr_info["namespace"], body=job_manifest
        )
        logger.info(f"Created retraining job {job_name} for {cr_info['cr_name']}")
        return True
    except Exception as exc:
        logger.error(f"Failed to create retraining job: {exc}")
        return False


def main():
    """Main entry point for retraining controller."""
    logger.info(
        f"Starting retraining controller (threshold: {DRIFT_THRESHOLD}%, duration: {DRIFT_DURATION_MINUTES}min)"
    )

    # Check which CRs have active severe drift
    crs_to_retrain = check_active_drift()

    if not crs_to_retrain:
        logger.info("No CRs with sustained severe drift detected")
        return 0

    logger.info(f"Found {len(crs_to_retrain)} CR(s) needing retraining")

    # Trigger retraining for each
    success_count = 0
    for cr in crs_to_retrain:
        logger.info(
            f"Triggering retraining for {cr['cr_name']} (error: {cr['error_pct']:.1f}%)"
        )
        if create_retraining_job(cr):
            success_count += 1

    logger.info(
        f"Successfully triggered {success_count}/{len(crs_to_retrain)} retraining jobs"
    )
    return 0 if success_count == len(crs_to_retrain) else 1


if __name__ == "__main__":
    sys.exit(main())
