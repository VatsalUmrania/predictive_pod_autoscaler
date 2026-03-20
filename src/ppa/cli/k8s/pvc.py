"""PVC operations."""

from kubernetes.client import ApiException, V1PersistentVolumeClaim

from .client import get_core_v1


def ensure_exists(
    name: str,
    namespace: str = "default",
    size: str = "1Gi",
) -> bool:
    """Create PVC if it doesn't exist. Returns True if created."""
    core = get_core_v1()

    try:
        core.read_namespaced_persistent_volume_claim(name, namespace)
        return False
    except ApiException as e:
        if e.status != 404:
            raise
        pvc = V1PersistentVolumeClaim(
            api_version="v1",
            kind="PersistentVolumeClaim",
            metadata={"name": name, "namespace": namespace},
            spec={
                "access_modes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": size}},
            },
        )
        core.create_namespaced_persistent_volume_claim(namespace, pvc)
        return True
