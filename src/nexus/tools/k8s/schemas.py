"""
Kubernetes Tool Input Schemas
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator


class GetPodLogsSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    pod_name: str = Field(..., description="Target pod name")
    tail_lines: int = Field(default=50, description="Number of log lines to retrieve")


class DescribeResourceSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    resource_kind: str = Field(
        default="deployment",
        description="Resource kind: pod, deployment, service, configmap",
    )
    resource_name: str = Field(default="", description="Resource name")
    deployment_name: str | None = Field(
        default=None, description="Optional alias for resource_name"
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_name(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("resource_name") and values.get("deployment_name"):
                values["resource_name"] = values["deployment_name"]
        return values


class GetMetricsSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    pod_name: str = Field(..., description="Target pod name")


class RestartDeploymentSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    deployment_name: str = Field(
        default="", description="Deployment name to rollout restart"
    )
    resource_name: str | None = Field(
        default=None, description="Optional alias for deployment_name"
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_name(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("deployment_name") and values.get("resource_name"):
                values["deployment_name"] = values["resource_name"]
            dep = values.get("deployment_name")
            if dep:
                values["deployment_name"] = re.sub(r"-[0-9a-f]{8,10}-[0-9a-z]{5}$", "", str(dep))
        return values


class ScaleResourceSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    resource_kind: str = Field(
        default="deployment", description="Resource kind to scale (deployment)"
    )
    resource_name: str = Field(default="", description="Target resource name")
    deployment_name: str | None = Field(
        default=None, description="Optional alias for resource_name"
    )
    replicas: int = Field(..., description="Target replica count")

    @model_validator(mode="before")
    @classmethod
    def resolve_name(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("resource_name") and values.get("deployment_name"):
                values["resource_name"] = values["deployment_name"]
            res = values.get("resource_name")
            if res:
                values["resource_name"] = re.sub(r"-[0-9a-f]{8,10}-[0-9a-z]{5}$", "", str(res))
        return values


class RollbackDeploymentSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    deployment_name: str = Field(
        default="", description="Deployment name to roll back to previous revision"
    )
    resource_name: str | None = Field(
        default=None, description="Optional alias for deployment_name"
    )
    target_revision: int | None = Field(
        default=None, description="Optional target revision number to roll back to"
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_name(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("deployment_name") and values.get("resource_name"):
                values["deployment_name"] = values["resource_name"]
            dep = values.get("deployment_name")
            if dep:
                values["deployment_name"] = re.sub(r"-[0-9a-f]{8,10}-[0-9a-z]{5}$", "", str(dep))
        return values


class RemoveContainerCommandSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    deployment_name: str = Field(default="", description="Deployment name")
    resource_name: str | None = Field(
        default=None, description="Optional alias for deployment_name"
    )
    container_name: str | None = Field(
        default=None,
        description="Target container name (defaults to primary container)",
    )

    @model_validator(mode="before")
    @classmethod
    def resolve_name(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("deployment_name") and values.get("resource_name"):
                values["deployment_name"] = values["resource_name"]
        return values


class PatchResourceLimitsSchema(BaseModel):
    model_config = {"extra": "ignore"}
    namespace: str = Field(default="default", description="Kubernetes namespace")
    deployment_name: str = Field(..., description="Deployment name to patch")
    container_name: str | None = Field(
        default=None,
        description="Optional container name (defaults to first container)",
    )
    cpu_limit: str | None = Field(
        default=None, description="CPU limit, e.g. '500m' or '1'"
    )
    memory_limit: str | None = Field(
        default=None, description="Memory limit, e.g. '512Mi' or '1Gi'"
    )
