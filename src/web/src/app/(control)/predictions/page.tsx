import * as React from "react"
import { Suspense } from "react"

import { ApiError } from "@/components/api-error"
import { PageHeader } from "@/components/page-header"
import { PredictionsTable } from "@/components/dashboard/predictions-table"
import { MetricPanel } from "@/components/dashboard/metric-panel"
import { PrescalerModeSwitch } from "@/components/prescaler-mode-switch"
import { PanelSkeleton, TableSkeleton } from "@/components/skeletons"
import { getPredictions, getStatus } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"
import { formatNumber, formatPercent } from "@/lib/format"

export default function PredictionsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Predictive scaling"
        description="Load forecasts published by the predictive autoscaler and the scoring state of the model behind them."
      />

      <Suspense fallback={<PanelSkeleton rows={6} />}>
        <PrescalerPanel />
      </Suspense>

      <Suspense fallback={<TableSkeleton rows={6} columns={6} />}>
        <PredictionFeed />
      </Suspense>
    </div>
  )
}

async function PrescalerPanel() {
  const [status, predictions] = await Promise.allSettled([
    getStatus(),
    getPredictions(),
  ])

  if (status.status === "rejected") {
    return (
      <ApiError
        error={describeApiError(status.reason)}
        title="Prescaler status unavailable"
      />
    )
  }

  const prescaler = status.value.prescaler
  const mode =
    prescaler?.mode ??
    (predictions.status === "fulfilled"
      ? predictions.value[0]?.mode
      : undefined)

  return (
    <div className="space-y-4">
      {prescaler ? (
        <MetricPanel
          title="Predictive autoscaler"
          items={[
            { label: "Mode", value: prescaler.mode },
            { label: "Decisions made", value: formatNumber(prescaler.decisions_made) },
            { label: "Events received", value: formatNumber(prescaler.events_received) },
            { label: "Precision", value: formatPercent(prescaler.precision) },
            { label: "SMAPE", value: prescaler.smape ?? "-" },
            {
              label: "Ready for advisory",
              value: prescaler.ready_for_advisory ? "yes" : "no",
              tone: prescaler.ready_for_advisory ? "success" : "default",
            },
            { label: "Skipped on confidence", value: prescaler.skipped_confidence },
            { label: "Skipped on threshold", value: prescaler.skipped_threshold },
            { label: "Skipped on cooldown", value: prescaler.skipped_cooldown },
          ]}
          columns={3}
        />
      ) : null}

      <PrescalerModeSwitch currentMode={mode} />
    </div>
  )
}

async function PredictionFeed() {
  let predictions
  try {
    predictions = await getPredictions()
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Predictions unavailable" />
  }

  return (
    <div className="space-y-3">
      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {predictions.length} scaling decision
        {predictions.length === 1 ? "" : "s"}
      </p>
      <PredictionsTable predictions={predictions} />
    </div>
  )
}
