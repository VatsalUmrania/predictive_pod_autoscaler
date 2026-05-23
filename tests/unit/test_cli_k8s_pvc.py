"""Unit tests for PVC helper functions."""

from kubernetes.client import ApiException

from ppa.cli.k8s import pvc


def test_ensure_exists_creates_pvc_with_access_mode(monkeypatch):
    created: list = []

    class FakeCoreV1:
        def read_namespaced_persistent_volume_claim(self, name, namespace):
            raise ApiException(status=404)

        def create_namespaced_persistent_volume_claim(self, namespace, body):
            created.append((namespace, body))

    monkeypatch.setattr(pvc, "get_core_v1", lambda: FakeCoreV1())

    created_now = pvc.ensure_exists("ppa-models", "default", "2Gi")

    assert created_now is True
    assert len(created) == 1
    namespace, body = created[0]
    assert namespace == "default"
    assert body.metadata.name == "ppa-models"
    assert body.spec.access_modes == ["ReadWriteOnce"]
    assert body.spec.resources.requests == {"storage": "2Gi"}
