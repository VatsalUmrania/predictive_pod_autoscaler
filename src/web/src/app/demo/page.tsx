import * as React from "react"
import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import { ArrowRight01Icon } from "@hugeicons/core-free-icons"

import { EmptyState } from "@/components/empty-state"
import { KpiTile } from "@/components/kpi-tile"
import { PageHeader } from "@/components/page-header"
import { MetricPanel } from "@/components/dashboard/metric-panel"
import { StatusBadge } from "@/components/dashboard/status-badge"
import {
  DEMO_INCIDENTS,
  DEMO_PENDING_APPROVALS,
  DEMO_STATUS,
} from "@/lib/demo-data"
import { formatNumber, formatPercent } from "@/lib/format"

export default function DemoOverviewPage() {
  const workflow = DEMO_STATUS.workflow
  const prescaler = DEMO_STATUS.prescaler
  const learning = DEMO_STATUS.learning

  return (
    <div className="space-y-6">
      <PageHeader
        title="Sample overview"
        description="A static snapshot of what the control plane looks like when the NEXUS engine has incidents to work on. Nothing on this page is live."
        actions={
          <Link
            href="/demo/incidents"
            className="inline-flex items-center gap-1 font-mono text-[11px] text-chart-1 underline-offset-4 hover:underline"
          >
            Sample incidents
            <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
          </Link>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <KpiTile
          label="Active incidents"
          value={workflow.active_incidents}
          hint="Currently moving through the graph"
          href="/demo/incidents"
        />
        <KpiTile
          label="Pending approvals"
          value={workflow.pending_approvals}
          hint="Paused at the approval node"
          href="/demo/approvals"
          tone="attention"
        />
        <KpiTile
          label="Incidents processed"
          value={workflow.incidents_processed}
          hint="Handled since the engine started"
        />
        <KpiTile
          label="Prediction precision"
          value={formatPercent(prescaler.precision)}
          hint="Scored against observed outcomes"
          href="/demo/predictions"
        />
        <KpiTile
          label="Learning cycles"
          value={learning.cycles_run}
          hint="Feedback loop iterations completed"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="rounded-lg border border-border bg-card">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold tracking-tight">
              Sample incident feed
            </h2>
          </div>
          <ul className="divide-y divide-border">
            {DEMO_INCIDENTS.map((incident) => (
              <li key={incident.incident_id} className="px-4 py-3">
                <Link
                  href={`/demo/incidents/${encodeURIComponent(incident.incident_id)}`}
                  className="flex items-start justify-between gap-3"
                >
                  <span className="min-w-0">
                    <span className="block font-mono text-xs font-semibold">
                      {incident.incident_id}
                    </span>
                    <span className="mt-0.5 block truncate font-mono text-[11px] text-muted-foreground">
                      {incident.target_resource}
                    </span>
                  </span>
                  <StatusBadge
                    status={incident.status}
                    isInterrupted={incident.is_interrupted}
                    className="mt-0.5"
                  />
                </Link>
              </li>
            ))}
          </ul>
        </section>

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
      </div>

      {DEMO_PENDING_APPROVALS.length === 0 ? (
        <EmptyState
          title="Nothing waiting on an operator"
          description="No sample incident is paused at the approval node."
        />
      ) : null}
    </div>
  )
}
