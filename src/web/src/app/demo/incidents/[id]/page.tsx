import * as React from "react"
import { Suspense } from "react"
import Link from "next/link"
import { notFound } from "next/navigation"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Alert02Icon,
  Brain02Icon,
  ScrollIcon,
  ToolsIcon,
} from "@hugeicons/core-free-icons"

import { AgentActivity } from "@/components/agent-activity"
import { EmptyState } from "@/components/empty-state"
import { IncidentTerminal } from "@/components/dashboard/incident-terminal"
import { PipelineStepper } from "@/components/dashboard/pipeline-stepper"
import { RemediationPlan } from "@/components/dashboard/remediation-plan"
import { StatusBadge, SeverityBadge } from "@/components/dashboard/status-badge"
import { DetailHeaderSkeleton } from "@/components/skeletons"
import { ThinkingTrace } from "@/components/dashboard/thinking-trace"
import { ToolExecution } from "@/components/dashboard/tool-execution"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import {
  DEMO_AGENT_MESSAGES,
  DEMO_AGENT_RUNS,
  DEMO_AUDIT_RECORDS,
  DEMO_INCIDENTS,
} from "@/lib/demo-data"
import { formatPercent, formatTimestamp } from "@/lib/format"

export default function DemoIncidentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  return (
    <div className="space-y-4">
      <Suspense fallback={<DetailHeaderSkeleton />}>
        <DemoIncidentView params={params} />
      </Suspense>
    </div>
  )
}

async function DemoIncidentView({
  params,
}: {
  params: Promise<{ id: string }>
}) {
  const { id } = await params
  const incident = DEMO_INCIDENTS.find((i) => i.incident_id === id)

  if (!incident) notFound()

  const audit = DEMO_AUDIT_RECORDS.filter((r) => r.incident_id === id)

  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-xs">
        <Link
          href="/demo/incidents"
          className="text-muted-foreground transition-colors hover:text-foreground"
        >
          Sample incidents
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

      {incident.is_interrupted ? (
        <div className="rounded-lg border border-chart-2/40 bg-chart-2/10 p-4 text-xs">
          <p className="font-semibold text-foreground">
            Operator sign-off required
          </p>
          <p className="mt-1.5 leading-relaxed text-muted-foreground">
            {incident.policy_decision.reason}
          </p>
          <p className="mt-2 font-mono text-[11px] text-muted-foreground">
            Approve and reject are disabled on sample pages. Open the live control
            plane to act on a real approval.
          </p>
        </div>
      ) : null}

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
          <AgentActivity runs={DEMO_AGENT_RUNS} messages={DEMO_AGENT_MESSAGES} />
        </TabsContent>
      </Tabs>

      {audit.length > 0 ? (
        <EmptyState
          title="Sample audit records"
          description={`${audit.length} governance record${audit.length === 1 ? "" : "s"} exist for this incident in the sample data.`}
        />
      ) : null}
    </div>
  )
}
