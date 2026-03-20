"""Pod lifecycle management."""

import time
import uuid

from kubernetes.client import (
    ApiException,
    V1Container,
    V1ObjectMeta,
    V1PersistentVolumeClaimVolumeSource,
    V1Pod,
    V1PodSpec,
    V1Volume,
    V1VolumeMount,
)

from .client import get_core_v1


def unique_pod_name(prefix: str = "ppa-loader") -> str:
    """Generate unique pod name."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def create_loader_pod(
    name: str,
    image: str,
    pvc_name: str,
    namespace: str = "default",
) -> None:
    """Create loader pod using proper V1PodSpec."""
    pod = V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=V1ObjectMeta(name=name, namespace=namespace),
        spec=V1PodSpec(
            restart_policy="Never",
            containers=[
                V1Container(
                    name="loader",
                    image=image,
                    image_pull_policy="Never",
                    command=["sleep", "300"],
                    volume_mounts=[V1VolumeMount(name="models", mount_path="/models")],
                )
            ],
            volumes=[
                V1Volume(
                    name="models",
                    persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                        claim_name=pvc_name
                    ),
                )
            ],
        ),
    )
    get_core_v1().create_namespaced_pod(namespace, pod)


def wait_for_ready(
    name: str,
    namespace: str = "default",
    timeout: int = 90,
) -> bool:
    """Wait for pod Running + Ready."""
    core = get_core_v1()
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            pod = core.read_namespaced_pod(name, namespace)

            if pod.status and pod.status.phase == "Running":
                conditions = pod.status.conditions or []
                if any(c.type == "Ready" and c.status == "True" for c in conditions):
                    return True

        except ApiException as e:
            if e.status != 404:
                raise

        time.sleep(2)

    return False


def delete_pod(name: str, namespace: str = "default") -> None:
    """Delete pod, ignore not-found."""
    try:
        get_core_v1().delete_namespaced_pod(name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
