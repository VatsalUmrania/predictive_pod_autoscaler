import * as React from "react"
import Link from "next/link"
import { Suspense } from "react"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowRight01Icon,
  Brain02Icon,
  ChartLineData02Icon,
  CpuIcon,
  Layers01Icon,
  Shield02Icon,
} from "@hugeicons/core-free-icons"

import { ApiError } from "@/components/api-error"
import { EmptyState } from "@/components/empty-state"
import { KpiTile } from "@/components/kpi-tile"
import { PageHeader } from "@/components/page-header"
import { MetricPanel } from "@/components/dashboard/metric-panel"
import { StatusBadge } from "@/components/dashboard/status-badge"
import {
  KpiRowSkeleton,
  ListSkeleton,
  PanelSkeleton,
} from "@/components/skeletons"
import {
  getIncidents,
  getPendingApprovals,
  getRcaResults,
  getStatus,
} from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"
import { formatNumber, formatPercent, formatTimestamp } from "@/lib/format"

export default function OverviewPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Live state of the NEXUS autonomous SRE engine: workflow counters, pending operator approvals, recent incidents, and predictive scaling health."
      />

      <Suspense fallback={<KpiRowSkeleton />}>
        <KpiSection />
      </Suspense>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Suspense fallback={<ListSkeleton rows={3} />}>
          <PendingSection />
        </Suspense>
        <Suspense fallback={<ListSkeleton rows={3} />}>
          <RecentIncidentsSection />
        </Suspense>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Suspense fallback={<PanelSkeleton rows={6} />}>
          <PrescalerSection />
        </Suspense>
        <Suspense fallback={<PanelSkeleton rows={6} />}>
          <LearningSection />
        </Suspense>
      </div>

      <Suspense fallback={<ListSkeleton rows={3} />}>
        <RcaSection />
      </Suspense>
    </div>
  )
}

async function KpiSection() {
  let status
  try {
    status = await getStatus()
  } catch (error) {
    return <ApiError error={describeApiError(error)} />
  }

  const workflow = status.workflow
  const prescaler = status.prescaler
  const learning = status.learning

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      <KpiTile
        label="Active incidents"
        value={workflow?.active_incidents ?? 0}
        hint="Currently moving through the graph"
        href="/incidents"
        icon={Layers01Icon}
      />
      <KpiTile
        label="Pending approvals"
        value={workflow?.pending_approvals ?? 0}
        hint="Paused at the approval node"
        href="/approvals"
        icon={Shield02Icon}
        tone={workflow?.pending_approvals ? "attention" : "default"}
      />
      <KpiTile
        label="Incidents processed"
        value={workflow?.incidents_processed ?? 0}
        hint="Handled since the engine started"
        icon={CpuIcon}
      />
      <KpiTile
        label="Prediction precision"
        value={prescaler ? formatPercent(prescaler.precision) : "-"}
        hint="Scored against observed outcomes"
        href="/predictions"
        icon={ChartLineData02Icon}
      />
      <KpiTile
        label="Learning cycles"
        value={learning?.cycles_run ?? 0}
        hint="Feedback loop iterations completed"
        icon={Brain02Icon}
      />
    </div>
  )
}

async function PendingSection() {
  let approvals
  try {
    approvals = await getPendingApprovals()
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Approval queue unavailable" />
  }

  const pending = approvals.filter((entry) => entry.incident_id).slice(0, 5)

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">
          Awaiting operator approval
        </h2>
        <Link
          href="/approvals"
          className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-primary"
        >
          Open queue
          <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
        </Link>
      </div>

      {pending.length === 0 ? (
        <div className="p-4">
          <EmptyState
            title="Nothing waiting on an operator"
            description="No incident is currently paused at the approval node."
          />
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {pending.map((entry, index) => (
            <li key={entry.approval_id ?? index} className="px-4 py-3">
              <Link
                href={`/incidents/${encodeURIComponent(entry.incident_id as string)}`}
                className="flex items-center justify-between gap-3"
              >
                <span className="min-w-0">
                  <span className="block font-mono text-xs font-semibold">
                    {entry.incident_id}
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[11px] text-muted-foreground">
                    {entry.target ?? entry.runbook_id ?? "awaiting sign-off"}
                  </span>
                </span>
                <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
                  {entry.confidence !== undefined
                    ? formatPercent(entry.confidence)
                    : ""}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

async function RecentIncidentsSection() {
  let incidents
  try {
    incidents = await getIncidents(5)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Incident feed unavailable" />
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">Recent incidents</h2>
        <Link
          href="/incidents"
          className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-primary"
        >
          All incidents
          <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
        </Link>
      </div>

      {incidents.length === 0 ? (
        <div className="p-4">
          <EmptyState
            title="No incidents reported"
            description="The NEXUS engine has not observed a failing workload yet."
          />
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {incidents.map((incident) => (
            <li key={incident.incident_id} className="px-4 py-3">
              <Link
                href={`/incidents/${encodeURIComponent(incident.incident_id)}`}
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
      )}
    </section>
  )
}

async function PrescalerSection() {
  let status
  try {
    status = await getStatus()
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Prescaler status unavailable" />
  }

  const prescaler = status.prescaler
  if (!prescaler) {
    return (
      <MetricPanel
        title="Predictive autoscaler"
        icon={ChartLineData02Icon}
        items={[
          { label: "State", value: "not connected" },
          { label: "Mode", value: "unknown" },
        ]}
      />
    )
  }

  return (
    <MetricPanel
      title="Predictive autoscaler"
      icon={ChartLineData02Icon}
      items={[
        { label: "Mode", value: prescaler.mode },
        { label: "Decisions made", value: formatNumber(prescaler.decisions_made) },
        { label: "Events received", value: formatNumber(prescaler.events_received) },
        { label: "Precision", value: formatPercent(prescaler.precision) },
        { label: "SMAPE", value: prescaler.smape ?? "-" },
        {
          label: "Skipped",
          value: `${prescaler.skipped_confidence + prescaler.skipped_threshold + prescaler.skipped_cooldown}`,
        },
        {
          label: "Ready for advisory",
          value: prescaler.ready_for_advisory ? "yes" : "no",
          tone: prescaler.ready_for_advisory ? "success" : "default",
        },
        {
          label: "Skipped detail",
          value: `c${prescaler.skipped_confidence} t${prescaler.skipped_threshold} k${prescaler.skipped_cooldown}`,
        },
      ]}
    />
  )
}

async function LearningSection() {
  let status
  try {
    status = await getStatus()
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Learning status unavailable" />
  }

  const learning = status.learning
  if (!learning) {
    return (
      <MetricPanel
        title="Learning loop"
        icon={Brain02Icon}
        items={[
          { label: "State", value: "not connected" },
          { label: "Cycles run", value: "-" },
        ]}
      />
    )
  }

  const kpiEntries = Object.entries(learning.last_kpis ?? {})

  return (
    <MetricPanel
      title="Learning loop"
      icon={Brain02Icon}
      items={[
        { label: "Cycles run", value: formatNumber(learning.cycles_run) },
        { label: "Recommendations", value: formatNumber(learning.last_recommendations) },
        { label: "Interval", value: `${learning.interval_s}s` },
        { label: "Window", value: `${learning.window_days}d` },
        ...kpiEntries.map(([key, value]) => ({
          label: key.replace(/_/g, " "),
          value: typeof value === "number" ? formatNumber(value, 2) : String(value),
        })),
      ]}
    />
  )
}

async function RcaSection() {
  let results
  try {
    results = await getRcaResults(5)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="RCA feed unavailable" />
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">Recent root cause analyses</h2>
      </div>

      {results.length === 0 ? (
        <div className="p-4">
          <EmptyState
            title="No root cause analyses yet"
            description="The engine has not produced an RCA result yet."
          />
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {results.map((result, index) => {
            const nestedTarget = result.target as
              | { resource_name?: string; namespace_or_region?: string; resource_kind?: string }
              | undefined
            const target =
              typeof result.target === "string"
                ? result.target
                : typeof result.target_resource === "string"
                  ? result.target_resource
                  : nestedTarget && typeof nestedTarget === "object"
                    ? [
                        nestedTarget.namespace_or_region,
                        nestedTarget.resource_kind,
                        nestedTarget.resource_name,
                      ]
                        .filter(Boolean)
                        .join("/") || null
                    : null
            const summary =
              typeof result.root_cause_summary === "string"
                ? result.root_cause_summary
                : typeof result.suspected_cause === "string"
                  ? result.suspected_cause
                  : null
            const confidence =
              typeof result.confidence === "number" ? result.confidence : null
            const updatedAt =
              typeof result.updated_at === "string"
                ? result.updated_at
                : typeof result.timestamp === "string"
                  ? result.timestamp
                  : typeof result.decided_at === "string"
                    ? result.decided_at
                    : null

            return (
              <li key={index} className="px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  {target ? (
                    <span className="font-mono text-xs font-semibold">{target}</span>
                  ) : (
                    <span className="font-mono text-xs text-muted-foreground">
                      RCA {index + 1}
                    </span>
                  )}
                  {confidence !== null ? (
                    <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                      {formatPercent(confidence)} confidence
                    </span>
                  ) : null}
                </div>
                {summary ? (
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                    {summary}
                  </p>
                ) : (
                  <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                    {Object.keys(result).join(", ") || "no fields"}
                  </p>
                )}
                {updatedAt ? (
                  <p className="mt-1 font-mono text-[11px] tabular-nums text-muted-foreground/80">
                    {formatTimestamp(updatedAt)}
                  </p>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
