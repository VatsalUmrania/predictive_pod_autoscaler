import * as React from "react"
import Link from "next/link"
import { Suspense } from "react"
import { notFound } from "next/navigation"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Alert02Icon,
  Brain02Icon,
  FileCodeIcon,
  ScrollIcon,
  ToolsIcon,
} from "@hugeicons/core-free-icons"

import { AgentActivity } from "@/components/agent-activity"
import { ApiError } from "@/components/api-error"
import { AuditTable } from "@/components/audit-table"
import { EmptyState } from "@/components/empty-state"
import { HitlConfirmation } from "@/components/dashboard/hitl-confirmation"
import { IncidentTerminal } from "@/components/dashboard/incident-terminal"
import { PipelineStepper } from "@/components/dashboard/pipeline-stepper"
import { RemediationPlan } from "@/components/dashboard/remediation-plan"
import { StatusBadge, SeverityBadge } from "@/components/dashboard/status-badge"
import { ThinkingTrace } from "@/components/dashboard/thinking-trace"
import { ToolExecution } from "@/components/dashboard/tool-execution"
import {
  DetailHeaderSkeleton,
  ListSkeleton,
  PanelSkeleton,
} from "@/components/skeletons"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import {
  getAgentMessages,
  getAgentRuns,
  getAuditForIncident,
  getIncidentById,
  getIncidentReport,
} from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"
import { formatPercent, formatTimestamp } from "@/lib/format"

export default function IncidentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  return (
    <div className="space-y-4">
      <Suspense fallback={<DetailHeaderSkeleton />}>
        <IncidentView params={params} />
      </Suspense>
    </div>
  )
}

async function IncidentView({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params

  let incident
  try {
    incident = await getIncidentById(id)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Incident unavailable" />
  }

  if (!incident) notFound()

  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-xs">
        <Link
          href="/incidents"
          className="text-muted-foreground transition-colors hover:text-foreground"
        >
          Incidents
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="font-mono font-semibold text-foreground">{id}</span>
      </nav>

      <section className="rounded-lg border border-border bg-card p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-mono text-lg font-bold tracking-tight text-foreground">
                {incident.incident_id}
              </h1>
              <SeverityBadge severity={incident.severity} />
              <StatusBadge
                status={incident.status}
                isInterrupted={incident.is_interrupted}
              />
            </div>
            <dl className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <div className="flex gap-1.5">
                <dt>platform</dt>
                <dd className="font-mono text-foreground">{incident.platform}</dd>
              </div>
              {incident.namespace ? (
                <div className="flex gap-1.5">
                  <dt>namespace</dt>
                  <dd className="font-mono text-foreground">{incident.namespace}</dd>
                </div>
              ) : null}
              {incident.resource_kind ? (
                <div className="flex gap-1.5">
                  <dt>kind</dt>
                  <dd className="font-mono text-foreground">
                    {incident.resource_kind}
                  </dd>
                </div>
              ) : null}
              <div className="flex gap-1.5">
                <dt>updated</dt>
                <dd className="font-mono tabular-nums text-foreground">
                  {formatTimestamp(incident.updated_at)}
                </dd>
              </div>
            </dl>
            <p className="mt-2 font-mono text-xs text-foreground">
              {incident.target_resource}
            </p>
            {incident.involved_object ? (
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                {incident.involved_object}
              </p>
            ) : null}
          </div>

          <div className="shrink-0 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs">
            <p className="text-muted-foreground">Confidence</p>
            <p className="mt-0.5 font-mono text-lg font-bold tabular-nums">
              {formatPercent(incident.confidence)}
            </p>
            <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
              threshold {formatPercent(incident.min_confidence_threshold)}
            </p>
          </div>
        </div>
      </section>

      <PipelineStepper incident={incident} />

      {incident.error ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
          <HugeiconsIcon icon={Alert02Icon} className="mt-0.5 size-4 shrink-0" />
          <span>{incident.error}</span>
        </div>
      ) : null}

      <HitlConfirmation incident={incident} />

      <Tabs defaultValue="diagnosis" className="gap-3">
        <TabsList variant="line" className="w-full justify-start overflow-x-auto">
          <TabsTrigger value="diagnosis" className="gap-1.5 px-3">
            <HugeiconsIcon icon={Brain02Icon} className="size-3.5" />
            Diagnosis and plan
          </TabsTrigger>
          <TabsTrigger value="execution" className="gap-1.5 px-3">
            <HugeiconsIcon icon={ToolsIcon} className="size-3.5" />
            Execution
            <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
              {incident.execution.results.length}
            </span>
          </TabsTrigger>
          <TabsTrigger value="agents" className="gap-1.5 px-3">
            <HugeiconsIcon icon={ScrollIcon} className="size-3.5" />
            Agent activity
          </TabsTrigger>
          <TabsTrigger value="audit" className="gap-1.5 px-3">
            <HugeiconsIcon icon={FileCodeIcon} className="size-3.5" />
            Audit
          </TabsTrigger>
        </TabsList>

        <TabsContent value="diagnosis">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ThinkingTrace incident={incident} />
            <RemediationPlan incident={incident} />
          </div>
        </TabsContent>

        <TabsContent value="execution">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ToolExecution incident={incident} />
            <IncidentTerminal incident={incident} />
          </div>
        </TabsContent>

        <TabsContent value="agents">
          <Suspense fallback={<ListSkeleton rows={3} />}>
            <AgentActivitySection id={incident.incident_id} />
          </Suspense>
        </TabsContent>

        <TabsContent value="audit">
          <Suspense fallback={<ListSkeleton rows={3} />}>
            <AuditSection id={incident.incident_id} />
          </Suspense>
        </TabsContent>
      </Tabs>

      <Suspense fallback={<PanelSkeleton rows={3} />}>
        <ReportSection id={incident.incident_id} ready={incident.post_incident_report_ready} />
      </Suspense>
    </div>
  )
}

async function AgentActivitySection({ id }: { id: string }) {
  const [runs, messages] = await Promise.allSettled([
    getAgentRuns(id),
    getAgentMessages(id),
  ])

  if (runs.status === "rejected") {
    return <ApiError error={describeApiError(runs.reason)} title="Agent runs unavailable" />
  }
  if (messages.status === "rejected") {
    return (
      <ApiError error={describeApiError(messages.reason)} title="Agent messages unavailable" />
    )
  }

  return <AgentActivity runs={runs.value} messages={messages.value} />
}

async function AuditSection({ id }: { id: string }) {
  let records
  try {
    records = await getAuditForIncident(id)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Audit trail unavailable" />
  }
  return <AuditTable records={records} />
}

async function ReportSection({
  id,
  ready,
}: {
  id: string
  ready: boolean
}) {
  if (!ready) {
    return (
      <EmptyState
        title="No post-incident report yet"
        description="The NEXUS engine generates a post-incident report once an incident reaches a terminal state."
        icon={FileCodeIcon}
      />
    )
  }

  let report
  try {
    report = await getIncidentReport(id)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Report unavailable" />
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">Post-incident report</h2>
        <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
          generated {formatTimestamp(report.generated_at)}
        </span>
      </div>
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 border-b border-border px-4 py-3 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">Outcome</dt>
          <dd className="mt-0.5 font-mono">{report.outcome}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Target</dt>
          <dd className="mt-0.5 truncate font-mono">{report.target}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Incident</dt>
          <dd className="mt-0.5 font-mono">{report.incident_id}</dd>
        </div>
      </dl>
      <pre className="max-h-96 overflow-auto p-4 font-mono text-[11px] leading-relaxed">
        {JSON.stringify(report.report, null, 2)}
      </pre>
    </section>
  )
}
