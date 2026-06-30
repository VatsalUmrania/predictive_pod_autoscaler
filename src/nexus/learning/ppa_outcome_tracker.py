"""PPA outcome tracker — closes the feedback loop for predictive autoscaling.

Responsibilities:
    1. Receive ppa.predictions.* events — store predicted_rps + resolution time
    2. After horizon_minutes elapse — fetch actual RPS, compute verdict + SMAPE
    3. Publish verdict to ppa.outcomes.{deployment} (live feedback to PPA)
    4. Nightly batch write to /data/ppa_outcomes.jsonl (PPA retraining pipeline)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _compute_smape(predicted: float, actual: float) -> float:
    """Symmetric Mean Absolute Percentage Error — safe for zero values."""
    denominator = (abs(predicted) + abs(actual)) / 2.0
    if denominator == 0.0:
        return 0.0
    return 100.0 * abs(predicted - actual) / denominator


@dataclass
class PendingPrediction:
    decision_id: str
    deployment: str
    namespace: str
    predicted_rps: float
    current_rps: float
    confidence: float
    horizon_minutes: int
    model_version: str
    raw_features: dict[str, float]
    expected_resolution_time: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expected_resolution_time


class PpaOutcomeTracker:
    def __init__(
        self,
        nats_client,
        batch_output_path: Path | None = None,
        prometheus_url: str | None = None,
    ):
        self._nats: object = nats_client
        self._pending: dict[str, PendingPrediction] = {}
        self._recent_outcomes: list[dict] = []
        self._batch_out: Path = batch_output_path or Path("data/ppa_outcomes.jsonl")
        self._prom_url: str | None = prometheus_url
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background poll loop (non-blocking)."""
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="ppa-outcome-tracker"
        )
        logger.info("[PpaOutcomeTracker] Started")

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("[PpaOutcomeTracker] Stopped")

    async def _poll_loop(self) -> None:
        """Poll every 30 seconds for expired predictions."""
        while True:
            try:
                await asyncio.sleep(30)
                await self._check_and_emit_outcome()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[PpaOutcomeTracker] Poll error: {exc}", exc_info=True)

    async def on_prediction(
        self,
        deployment: str,
        namespace: str,
        predicted_rps: float,
        current_rps: float,
        confidence: float,
        horizon_minutes: int,
        model_version: str,
        raw_features: dict[str, float],
        event_timestamp: str,
    ) -> None:
        """Called when a ppa.predictions.* event arrives — store for later verdict."""
        decision_id = str(uuid.uuid4())[:8].upper()
        now = datetime.now(timezone.utc)
        expected_resolution = now + timedelta(minutes=horizon_minutes)

        pending = PendingPrediction(
            decision_id=decision_id,
            deployment=deployment,
            namespace=namespace,
            predicted_rps=predicted_rps,
            current_rps=current_rps,
            confidence=confidence,
            horizon_minutes=horizon_minutes,
            model_version=model_version,
            raw_features=raw_features,
            expected_resolution_time=expected_resolution,
        )
        self._pending[decision_id] = pending
        logger.debug(
            f"[PpaOutcomeTracker] Stored {decision_id} for {deployment}, "
            f"resolves at {expected_resolution.isoformat()}"
        )

    async def _fetch_actual_rps(self, deployment: str) -> float:
        """
        Fetch current RPS for a deployment from Prometheus.
        Returns 0.0 on error (safe fallback).

        Resolution order:
            1. The URL explicitly injected at construction (NexusServer.create)
            2. $PROMETHEUS_URL env var
            3. ppa.config.get_prometheus_url() (legacy fallback)
        """
        url = self._prom_url or os.environ.get("PROMETHEUS_URL")
        if url is None:
            try:
                from ppa.config import get_prometheus_url

                url = get_prometheus_url()
            except Exception:
                url = "http://prometheus:9090"
        try:
            from prometheus_api_client import PrometheusConnect

            prom = PrometheusConnect(url=url)
            query = (
                f"sum(rate(nginx_ingress_requests_total"
                f'{{backend=~"{deployment}.*"}}[1m])) * 60'
            )
            result = prom.custom_query(query=query)
            if result and result[0]["value"][1] != "NaN":
                return float(result[0]["value"][1])
        except Exception as exc:
            logger.warning(
                f"[PpaOutcomeTracker] Failed to fetch actual RPS for {deployment}: {exc}"
            )
        return 0.0

    def _compute_verdict(self, pred: PendingPrediction, actual_rps: float) -> str:
        """Classify a prediction as spike_hit / spike_missed / correct_no_spike."""
        if pred.predicted_rps <= 0:
            return "correct_no_spike"
        # Spike is hit if actual was ≥ 130% of the baseline (current_rps at prediction time)
        spike_threshold_mult = 1.3
        baseline = pred.current_rps * spike_threshold_mult
        return "spike_hit" if actual_rps >= baseline else "spike_missed"

    async def _check_and_emit_outcome(self) -> None:
        """Poll pending predictions; emit outcome when horizon has elapsed."""
        now = datetime.now(timezone.utc)
        for decision_id, pred in list(self._pending.items()):
            if now < pred.expected_resolution_time:
                continue

            actual_rps = await self._fetch_actual_rps(pred.deployment)
            verdict = self._compute_verdict(pred, actual_rps)
            smape = _compute_smape(pred.predicted_rps, actual_rps)

            outcome_event = {
                "decision_id": decision_id,
                "deployment": pred.deployment,
                "namespace": pred.namespace,
                "predicted_rps": pred.predicted_rps,
                "actual_rps": actual_rps,
                "verdict": verdict,
                "smape": round(smape, 3),
                "confidence": pred.confidence,
                "model_version": pred.model_version,
                "resolution_at": now.isoformat(),
            }

            try:
                import json as _json

                subject = f"ppa.outcomes.{pred.deployment}"
                payload = _json.dumps(outcome_event).encode()
                # NATSClient.publish() only accepts IncidentEvent objects.
                # PPA outcome events are raw dicts on a non-NEXUS subject, so we
                # publish directly via the underlying JetStream context.
                js = getattr(self._nats, "_js", None)
                if js is not None:
                    await js.publish(subject, payload)
                else:
                    # Fallback: raw NATS core publish (no JetStream persistence)
                    nc = getattr(self._nats, "_nc", None)
                    if nc is not None:
                        await nc.publish(subject, payload)
                logger.info(
                    f"[PpaOutcomeTracker] Verdict {verdict} for {pred.deployment} "
                    f"(pred={pred.predicted_rps:.0f}, actual={actual_rps:.0f})"
                )
            except Exception as exc:
                logger.error(f"[PpaOutcomeTracker] Failed to publish outcome: {exc}")

            self._recent_outcomes.append(outcome_event)
            del self._pending[decision_id]

    async def run_batch_flush(self) -> None:
        """Write all recent outcomes to JSONL for PPA retraining pipeline."""
        if not self._recent_outcomes:
            logger.debug("[PpaOutcomeTracker] No outcomes to batch-flush")
            return

        self._batch_out.parent.mkdir(parents=True, exist_ok=True)
        with open(self._batch_out, "a") as f:
            for outcome in self._recent_outcomes:
                f.write(json.dumps(outcome) + "\n")

        count = len(self._recent_outcomes)
        self._recent_outcomes.clear()
        logger.info(
            f"[PpaOutcomeTracker] Batch-flushed {count} outcomes to {self._batch_out}"
        )
