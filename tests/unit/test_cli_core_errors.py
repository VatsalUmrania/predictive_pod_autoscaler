"""Tests for ppa.cli.core.errors module."""

import pytest

from ppa.cli.core.errors import (
    ConfigError,
    KubernetesError,
    PPAError,
    PrometheusError,
    ValidationError,
)


def test_ppa_error_with_message_only():
    """Test PPAError with just a message."""
    error = PPAError("Something went wrong")
    assert "Something went wrong" in str(error)


def test_ppa_error_with_context():
    """Test PPAError with context."""
    error = PPAError(
        "Config file invalid",
        context={"file": "config.yaml", "line": "42"},
    )
    error_str = str(error)
    assert "Config file invalid" in error_str
    assert "file=config.yaml" in error_str
    assert "line=42" in error_str


def test_ppa_error_with_suggestion():
    """Test PPAError with actionable suggestion."""
    error = PPAError(
        "Kubernetes not found",
        suggestion="Install kubectl or check PATH",
    )
    error_str = str(error)
    assert "Kubernetes not found" in error_str
    assert "Install kubectl" in error_str


def test_ppa_error_with_all_fields():
    """Test PPAError with message, context, and suggestion."""
    error = PPAError(
        "Deployment failed",
        context={"app": "my-app", "namespace": "default"},
        suggestion="Check pod logs with kubectl logs <pod>",
    )
    error_str = str(error)
    assert "Deployment failed" in error_str
    assert "app=my-app" in error_str
    assert "namespace=default" in error_str
    assert "Check pod logs" in error_str


def test_validation_error_inheritance():
    """Test ValidationError is a PPAError."""
    error = ValidationError("Invalid app name")
    assert isinstance(error, PPAError)
    assert "Invalid app name" in str(error)


def test_config_error_inheritance():
    """Test ConfigError is a PPAError."""
    error = ConfigError("Config not found")
    assert isinstance(error, PPAError)


def test_kubernetes_error_inheritance():
    """Test KubernetesError is a PPAError."""
    error = KubernetesError("Cluster not accessible")
    assert isinstance(error, PPAError)


def test_prometheus_error_inheritance():
    """Test PrometheusError is a PPAError."""
    error = PrometheusError("Prometheus connection timeout")
    assert isinstance(error, PPAError)


def test_error_exception_interface():
    """Test that errors work as exceptions."""
    with pytest.raises(PPAError) as exc_info:
        raise PPAError("Test error")
    assert "Test error" in str(exc_info.value)
