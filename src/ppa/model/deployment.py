"""Kubernetes deployment operations for model promotion.

Handles updating PredictiveAutoscaler resources with new model artifacts.
"""

import json
import subprocess


def patch_predictiveautoscaler_paths(
    cr_name: str,
    cr_namespace: str,
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None = None,
) -> tuple[bool, str]:
    """Update PredictiveAutoscaler resource with new model artifact paths.

    Args:
        cr_name: PredictiveAutoscaler custom resource name
        cr_namespace: Kubernetes namespace
        model_path: Path to TFLite model
        scaler_path: Path to scaler pickle
        target_scaler_path: Optional target scaler path

    Returns:
        Tuple of (success: bool, message: str)
    """
    spec: dict[str, str] = {
        "modelPath": model_path,
        "scalerPath": scaler_path,
    }
    if target_scaler_path:
        spec["targetScalerPath"] = target_scaler_path

    patch_payload = json.dumps({"spec": spec})
    cmd = [
        "kubectl",
        "-n",
        cr_namespace,
        "patch",
        "predictiveautoscaler",
        cr_name,
        "--type",
        "merge",
        "-p",
        patch_payload,
    ]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, result.stdout.strip() or "patched"
    except FileNotFoundError:
        return False, "kubectl not found"
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        return False, stderr or stdout or f"kubectl patch failed: code {exc.returncode}"


__all__ = ["patch_predictiveautoscaler_paths"]
