import * as React from "react"

import { PageHeader } from "@/components/page-header"
import { PredictionsTable } from "@/components/dashboard/predictions-table"
import { MetricPanel } from "@/components/dashboard/metric-panel"
import { DEMO_PREDICTIONS, DEMO_STATUS } from "@/lib/demo-data"
import { formatNumber, formatPercent } from "@/lib/format"

export default function DemoPredictionsPage() {
  const prescaler = DEMO_STATUS.prescaler

  return (
    <div className="space-y-6">
      <PageHeader
        title="Sample predictive scaling"
        description="A static set of load forecasts rendered through the same table the live feed uses."
      />

      <MetricPanel
        title="Sample predictive autoscaler"
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
        ]}
      />

      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {DEMO_PREDICTIONS.length} sample scaling decision
        {DEMO_PREDICTIONS.length === 1 ? "" : "s"}
      </p>
      <PredictionsTable predictions={DEMO_PREDICTIONS} />
    </div>
  )
}
