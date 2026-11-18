"""
NEXUS Pure, Platform-Agnostic OTLP Ingestion Webhook & Telemetry Extractor
===========================================================================
Plane 1 / Plane 2 Bridge: Extracts explicit OTLP attributes without regex
guessing, blocking K8s API calls, or fake 'default'/'unknown-service' fallbacks.
Leaves missing identity fields as None so the ReAct diagnose_node discovers
the true topology via live observation tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nexus.bus.events.enums import Severity, SignalKind, SignalSource
from nexus.bus.events.event import SymptomSignal
from nexus.bus.nats.client import NATSClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])
ingest_router = router


def _attrs_list_to_dict(attr_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Converts OTLP KeyValue list into a standard Python dict."""
    out: dict[str, Any] = {}
    for item in attr_list or []:
        key = item.get("key")
        val_obj = item.get("value") or {}
        if not key:
            continue
        for val_type in (
            "stringValue",
            "intValue",
            "doubleValue",
            "boolValue",
            "kvlistValue",
        ):
            if val_type in val_obj:
                if val_type == "kvlistValue":
                    out[key] = _attrs_list_to_dict(val_obj[val_type].get("values", []))
                else:
                    out[key] = val_obj[val_type]
                break
    return out


def extract_telemetry_context(
    resource_attrs: dict[str, Any],
    log_attrs: dict[str, Any],
    body: Any,
) -> dict[str, Any]:
    """
    Pure, platform-agnostic OTLP attribute extractor.
    Leaves missing identity fields as None so the ReAct diagnose_node discovers
    the true topology via observation tools.
    """
    attrs = {**log_attrs, **resource_attrs}

    involved: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    if isinstance(body, dict):
        obj = body.get("object") if isinstance(body.get("object"), dict) else body
        if isinstance(obj, dict):
            raw_involved = obj.get("involvedObject")
            involved = raw_involved if isinstance(raw_involved, dict) else {}
            raw_metadata = obj.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}

    platform: str | None = None
    if any(k.startswith("k8s.") for k in attrs) or involved:
        platform = "k8s"
    elif any(k.startswith(("aws.", "faas.", "cloud.")) for k in attrs):
        platform = attrs.get("cloud.provider") or "aws"

    namespace: str | None = (
        attrs.get("k8s.namespace.name")
        or involved.get("namespace")
        or metadata.get("namespace")
        or attrs.get("cloud.region")
    )

    controller_name: str | None = (
        attrs.get("k8s.deployment.name")
        or attrs.get("k8s.statefulset.name")
        or attrs.get("k8s.daemonset.name")
        or attrs.get("faas.name")
        or attrs.get("aws.ecs.service.name")
        or attrs.get("service.name")
    )

    involved_name: str | None = (
        attrs.get("k8s.involved.name")
        or attrs.get("k8s.pod.name")
        or involved.get("name")
    )
    involved_kind: str | None = attrs.get("k8s.involved.kind") or involved.get("kind")

    return {
        "platform": platform,
        "namespace": namespace,
        "service_name": controller_name,
        "involved_object_name": involved_name,
        "involved_object_kind": involved_kind,
        "raw_attributes": attrs,
        "raw_body": body,
    }


def _decode_otlp_body(body: Any) -> tuple[Any, str]:
    """Decode OTLP logRecord.body into a Python object (dict/str) and human-readable text."""
    if isinstance(body, dict):
        if "stringValue" in body:
            text = str(body["stringValue"])
            if text.strip().startswith("{"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed, text
                except Exception:
                    pass
            return text, text
        if "kvlistValue" in body:
            parsed = _attrs_list_to_dict(body["kvlistValue"].get("values", []))
            return parsed, json.dumps(parsed, default=str)
        return body, json.dumps(body, default=str)
    if isinstance(body, str):
        if body.strip().startswith("{"):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    return parsed, body
            except Exception:
                pass
        return body, body
    return body, str(body)


def extract_symptom_signals_from_otlp(payload: dict[str, Any]) -> list[SymptomSignal]:
    """Parse OTLP JSON logs/events payload into normalized SymptomSignals without fabricated defaults."""
    signals: list[SymptomSignal] = []

    # 1. Direct SymptomSignal representation
    if "symptom_summary" in payload and (
        "resource_urn" in payload or "involved_object_name" in payload
    ):
        try:
            signals.append(
                SymptomSignal(
                    source=SignalSource.KUBERNETES_API,
                    kind=SignalKind.ERROR_LOG_BURST,
                    provider=payload.get("provider"),
                    resource_urn=payload.get("resource_urn"),
                    region_or_namespace=payload.get("region_or_namespace"),
                    service_name=payload.get("service_name"),
                    involved_object_name=payload.get("involved_object_name"),
                    involved_object_kind=payload.get("involved_object_kind"),
                    symptom_summary=payload["symptom_summary"],
                    raw_payload=payload.get("raw_payload", payload),
                    severity=Severity(payload.get("severity", "high")),
                )
            )
            return signals
        except Exception:
            pass

    # 2. OTLP resourceLogs standard schema
    resource_logs = payload.get("resourceLogs", [])
    if isinstance(resource_logs, list):
        for res_log in resource_logs:
            res_attrs = _attrs_list_to_dict(
                (res_log.get("resource") or {}).get("attributes", [])
            )
            for scope_log in res_log.get("scopeLogs", []):
                for record in scope_log.get("logRecords", []):
                    log_attrs = _attrs_list_to_dict(record.get("attributes", []))
                    decoded_body, body_text = _decode_otlp_body(record.get("body", {}))
                    ctx = extract_telemetry_context(res_attrs, log_attrs, decoded_body)

                    body_obj = (
                        decoded_body.get("object")
                        if isinstance(decoded_body, dict)
                        and isinstance(decoded_body.get("object"), dict)
                        else decoded_body
                    )
                    reason = (
                        (body_obj.get("reason") if isinstance(body_obj, dict) else None)
                        or log_attrs.get("k8s.event.reason")
                        or "AnomalyEvent"
                    )
                    msg = (
                        (
                            body_obj.get("message")
                            if isinstance(body_obj, dict)
                            else None
                        )
                        or log_attrs.get("k8s.event.message")
                        or body_text
                        or reason
                    )
                    summary = f"{reason}: {msg}".strip()

                    sev_text = str(
                        record.get("severityText")
                        or (
                            body_obj.get("type") if isinstance(body_obj, dict) else None
                        )
                        or log_attrs.get("k8s.event.type")
                        or "high"
                    ).lower()
                    sev = (
                        Severity.CRITICAL
                        if "crit" in sev_text or "fatal" in sev_text
                        else (
                            Severity.HIGH
                            if "warn" in sev_text or "error" in sev_text
                            else Severity.MEDIUM
                        )
                    )

                    urn = ctx["raw_attributes"].get("k8s.resource.urn")
                    if (
                        not urn
                        and ctx["namespace"]
                        and (ctx["service_name"] or ctx["involved_object_name"])
                    ):
                        target_kind = (
                            "Deployment"
                            if ctx["raw_attributes"].get("k8s.deployment.name")
                            else (ctx["involved_object_kind"] or "Pod")
                        )
                        target_name = ctx["service_name"] or ctx["involved_object_name"]
                        urn = f"k8s://{ctx['namespace']}/apps/v1/{target_kind}/{target_name}"

                    sig = SymptomSignal(
                        source=SignalSource.KUBERNETES_API,
                        kind=SignalKind.ERROR_LOG_BURST,
                        provider=ctx["platform"],
                        resource_urn=urn,
                        region_or_namespace=ctx["namespace"],
                        service_name=ctx["service_name"],
                        involved_object_name=ctx["involved_object_name"],
                        involved_object_kind=ctx["involved_object_kind"],
                        symptom_summary=summary,
                        raw_payload={
                            **ctx,
                            "record": record,
                        },
                        severity=sev,
                    )
                    signals.append(sig)

    # 3. Fallback: generic event lists
    if not signals and "events" in payload:
        for ev in payload["events"]:
            if isinstance(ev, dict):
                signals.extend(extract_symptom_signals_from_otlp(ev))

    return signals


@router.post("/otlp")
@router.post("/otlp/v1/logs")
@router.post("/otlp/events")
@router.post("/otlp/events/v1/logs")
@router.post("/v1/logs")
async def ingest_otlp_events(request: Request) -> dict[str, Any]:
    """Ingest OTLP JSON or Protobuf events, compute deterministic hash, and publish to JetStream."""
    raw_body = await request.body()
    content_encoding = request.headers.get("content-encoding", "").lower()
    if "gzip" in content_encoding or raw_body.startswith(b"\x1f\x8b"):
        import gzip

        try:
            raw_body = gzip.decompress(raw_body)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to decompress gzip body: {exc}"
            ) from exc

    content_type = request.headers.get("content-type", "").lower()
    payload: dict[str, Any] = {}
    if "json" in content_type:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON payload: {exc}"
            ) from exc
    else:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            try:
                from google.protobuf.json_format import MessageToDict
                from opentelemetry.proto.logs.v1.logs_pb2 import LogsData

                ld = LogsData()
                ld.ParseFromString(raw_body)
                payload = MessageToDict(ld)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Failed to parse OTLP body: {exc}"
                ) from exc

    signals = extract_symptom_signals_from_otlp(payload)
    if not signals:
        return {"status": "accepted", "signals_processed": 0}

    nats = getattr(request.app.state, "nats_client", None) or NATSClient.get_instance()

    for signal in signals:
        identity = (
            signal.resource_urn
            or f"{signal.region_or_namespace}:{signal.service_name or signal.involved_object_name}"
        )
        incident_hash = hashlib.sha256(
            f"{identity}:{signal.symptom_summary}".encode()
        ).hexdigest()
        incident_event = signal.to_incident_event()

        try:
            if hasattr(nats, "publish"):
                await nats.publish(
                    subject="nexus.incidents.raw",
                    payload=incident_event.to_nats_payload(),
                    headers={"Nats-Msg-Id": incident_hash},
                )
            elif hasattr(nats, "publish_raw"):
                await nats.publish_raw(
                    "nexus.incidents.raw",
                    incident_event.model_dump(),
                    headers={"Nats-Msg-Id": incident_hash},
                )
        except Exception as e:
            logger.error("[Ingest] Failed to publish event to NATS: %s", e)
            raise HTTPException(
                status_code=500, detail="Failed to persist event to broker"
            ) from e

    return {"status": "accepted", "signals_processed": len(signals)}


__all__ = [
    "router",
    "ingest_router",
    "_attrs_list_to_dict",
    "extract_telemetry_context",
    "extract_symptom_signals_from_otlp",
    "ingest_otlp_events",
]
