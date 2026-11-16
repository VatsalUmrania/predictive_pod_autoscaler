"""
NEXUS Reactive OTLP Ingestion Webhook & Event Normalizer
=========================================================
Plane 1 / Plane 2 Bridge: Receives OpenTelemetry OTLP JSON events,
normalizes them into typed SymptomSignal models, and enforces deterministic
deduplication via NATS JetStream headers.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from nexus.bus.events.enums import Severity, SignalKind, SignalSource
from nexus.bus.events.event import SymptomSignal
from nexus.bus.nats_client import NatsClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])
ingest_router = router


def _extract_attr_val(attr: dict[str, Any]) -> Any:
    val = attr.get("value", {})
    if isinstance(val, dict):
        return (
            val.get("stringValue")
            or val.get("intValue")
            or val.get("boolValue")
            or val.get("doubleValue")
            or val.get("arrayValue")
            or str(val)
        )
    return val


def extract_symptom_signals_from_otlp(payload: dict[str, Any]) -> list[SymptomSignal]:
    """Parse OTLP JSON logs/events payload into normalized SymptomSignals."""
    signals: list[SymptomSignal] = []

    # 1. Direct SymptomSignal representation
    if "resource_urn" in payload and "symptom_summary" in payload:
        try:
            signals.append(
                SymptomSignal(
                    source=SignalSource.KUBERNETES_API,
                    kind=SignalKind.ERROR_LOG_BURST,
                    provider=payload.get("provider", "kubernetes"),
                    resource_urn=payload["resource_urn"],
                    region_or_namespace=payload.get("region_or_namespace", "default"),
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
            res_attrs_raw = res_log.get("resource", {}).get("attributes", [])
            res_attrs = {}
            for a in res_attrs_raw:
                if isinstance(a, dict) and "key" in a:
                    res_attrs[a["key"]] = _extract_attr_val(a)

            ns = (
                res_attrs.get("k8s.namespace.name")
                or res_attrs.get("service.namespace")
                or "default"
            )
            service = (
                res_attrs.get("service.name")
                or res_attrs.get("k8s.deployment.name")
                or res_attrs.get("k8s.pod.name")
                or "unknown-service"
            )
            kind = "Deployment" if "k8s.deployment.name" in res_attrs else "Pod"
            urn = (
                res_attrs.get("k8s.resource.urn")
                or f"k8s://{ns}/apps/v1/{kind}/{service}"
            )
            provider = res_attrs.get("cloud.provider") or "kubernetes"

            for scope_log in res_log.get("scopeLogs", []):
                for record in scope_log.get("logRecords", []):
                    rec_attrs = {}
                    for a in record.get("attributes", []):
                        if isinstance(a, dict) and "key" in a:
                            rec_attrs[a["key"]] = _extract_attr_val(a)

                    body = record.get("body", {})
                    body_dict: dict[str, Any] = {}
                    body_text = ""
                    if isinstance(body, dict):
                        if "stringValue" in body:
                            body_text = str(body["stringValue"])
                            if body_text.strip().startswith("{"):
                                try:
                                    parsed = json.loads(body_text)
                                    if isinstance(parsed, dict):
                                        body_dict = parsed
                                except Exception:
                                    pass
                        elif "mapValue" in body:
                            body_dict = _extract_attr_val(body)
                        else:
                            body_dict = body
                            body_text = str(body)
                    elif isinstance(body, str):
                        body_text = body
                        if body_text.strip().startswith("{"):
                            try:
                                parsed = json.loads(body)
                                if isinstance(parsed, dict):
                                    body_dict = parsed
                            except Exception:
                                pass
                    else:
                        body_text = str(body)

                    inv_obj = (
                        body_dict.get("involvedObject", {})
                        if isinstance(body_dict, dict)
                        else {}
                    )
                    ev_kind = (
                        inv_obj.get("kind")
                        or rec_attrs.get("k8s.object.kind")
                        or kind
                    )
                    ev_name = (
                        inv_obj.get("name")
                        or rec_attrs.get("k8s.object.name")
                        or service
                    )
                    ev_ns = inv_obj.get("namespace") or ns
                    ev_urn = (
                        f"k8s://{ev_ns}/apps/v1/{ev_kind}/{ev_name}"
                        if ev_name != "unknown-service"
                        else urn
                    )

                    reason = (
                        body_dict.get("reason")
                        or rec_attrs.get("k8s.event.reason")
                        or "AnomalyEvent"
                    ) if isinstance(body_dict, dict) else rec_attrs.get("k8s.event.reason", "AnomalyEvent")
                    msg = (
                        body_dict.get("message")
                        or rec_attrs.get("k8s.event.message")
                        or body_text
                        or reason
                    ) if isinstance(body_dict, dict) else (rec_attrs.get("k8s.event.message") or body_text or reason)
                    summary = f"{reason}: {msg}".strip()

                    sev_text = str(
                        record.get("severityText")
                        or body_dict.get("type")
                        or rec_attrs.get("k8s.event.type")
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

                    sig = SymptomSignal(
                        source=SignalSource.KUBERNETES_API,
                        kind=SignalKind.ERROR_LOG_BURST,
                        provider=provider,
                        resource_urn=ev_urn,
                        region_or_namespace=ev_ns,
                        symptom_summary=summary,
                        raw_payload=record,
                        severity=sev,
                    )
                    signals.append(sig)

    # 3. Fallback: generic event lists
    if not signals and "events" in payload:
        for ev in payload["events"]:
            if isinstance(ev, dict):
                signals.extend(extract_symptom_signals_from_otlp(ev))

    return signals


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

    nats = getattr(request.app.state, "nats_client", None) or NatsClient.get_instance()

    for signal in signals:
        incident_hash = hashlib.sha256(
            f"{signal.resource_urn}:{signal.symptom_summary}".encode()
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
    "extract_symptom_signals_from_otlp",
    "ingest_otlp_events",
]
