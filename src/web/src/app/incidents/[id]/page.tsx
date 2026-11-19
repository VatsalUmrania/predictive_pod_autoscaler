"use client"

import * as React from "react"
import Link from "next/link"
import { useParams } from "next/navigation"
import { useIncidents } from "@/context/incident-context"
import { NavigationHeader } from "@/components/dashboard/navigation-header"
import { PipelineStepper } from "@/components/dashboard/pipeline-stepper"
import { ThinkingTrace } from "@/components/dashboard/thinking-trace"
import { RemediationPlan } from "@/components/dashboard/remediation-plan"
import { ToolExecution } from "@/components/dashboard/tool-execution"
import { IncidentTerminal } from "@/components/dashboard/incident-terminal"
import { HitlConfirmation } from "@/components/dashboard/hitl-confirmation"
import { Button } from "@/components/ui/button"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowLeft01Icon,
  FileCodeIcon,
  Alert02Icon,
  Shield02Icon,
  CheckmarkCircle02Icon,
  ToolsIcon,
  Brain02Icon,
} from "@hugeicons/core-free-icons"
import { Spinner } from "@/components/ui/spinner"

function IncidentDetailContent() {
  const params = useParams()
  const { getIncident } = useIncidents()

  const incidentId = typeof params?.id === "string" ? params.id : ""
  const incident = getIncident(incidentId)
  const [activeTab, setActiveTab] = React.useState<"diagnosis" | "execution">("diagnosis")

  if (!incident) {
    return (
      <div className="min-h-screen flex flex-col bg-background text-foreground">
        <NavigationHeader />
        <main className="flex-1 flex flex-col items-center justify-center p-6 text-center">
          <div className="max-w-md space-y-4 rounded-lg border border-border bg-card p-8">
            <HugeiconsIcon icon={Alert02Icon} className="mx-auto size-8 text-destructive" />
            <h2 className="text-lg font-bold text-foreground">Incident Thread Not Found</h2>
            <p className="text-xs text-muted-foreground">
              Thread <code>{incidentId}</code> was not found in the current control plane feed.
            </p>
            <Link href="/">
              <Button variant="outline" size="sm" className="text-xs">
                <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5 mr-1.5" />
                Return to Incident Queue
              </Button>
            </Link>
          </div>
        </main>
      </div>
    )
  }

  const getStatusBadge = () => {
    if (incident.approval.is_interrupted) {
      return (
        <span className="flex items-center gap-1.5 rounded-full border border-chart-2/40 bg-chart-2/10 px-3 py-1 font-mono text-xs font-semibold text-chart-2">
          <HugeiconsIcon icon={Shield02Icon} className="size-3.5" />
          Awaiting Approval
        </span>
      )
    }
    if (incident.status === "RESOLVED") {
      return (
        <span className="flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 font-mono text-xs font-semibold text-primary">
          <HugeiconsIcon icon={CheckmarkCircle02Icon} className="size-3.5" />
          Resolved
        </span>
      )
    }
    if (["ESCALATED", "ROLLED_BACK", "EXECUTION_FAILED", "POLICY_DENIED"].includes(incident.status)) {
      return (
        <span className="flex items-center gap-1.5 rounded-full border border-destructive/30 bg-destructive/10 px-3 py-1 font-mono text-xs font-semibold text-destructive">
          <HugeiconsIcon icon={Alert02Icon} className="size-3.5" />
          {incident.status.replace(/_/g, " ")}
        </span>
      )
    }
    return (
      <span className="flex items-center gap-1.5 rounded-full border border-chart-2/40 bg-chart-2/10 px-3 py-1 font-mono text-xs font-medium text-chart-2">
        {incident.status.replace(/_/g, " ")}
      </span>
    )
  }

  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <NavigationHeader />

      <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-6">
        {/* Breadcrumb & Navigation */}
        <div className="flex items-center justify-between">
          <nav className="flex items-center gap-2 text-xs text-muted-foreground" aria-label="Breadcrumb">
            <Link href="/" className="hover:text-foreground transition-colors">
              Control Panel
            </Link>
            <span>/</span>
            <Link href="/threads" className="hover:text-foreground transition-colors">
              Threads
            </Link>
            <span>/</span>
            <span className="font-mono font-semibold text-foreground">{incident.incident_id}</span>
          </nav>

          <Link href="/threads">
            <Button variant="ghost" size="sm" className="h-7 text-xs">
              <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5 mr-1" />
              Back to Threads Feed
            </Button>
          </Link>
        </div>

        {/* Incident Detail Header */}
        <section className="rounded-lg border border-border bg-card p-5 space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="flex items-center gap-3">
                <h1 className="font-mono text-xl font-bold tracking-tight text-foreground">
                  {incident.incident_id}
                </h1>
                {getStatusBadge()}
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span className="font-mono uppercase">{incident.platform}</span>
                <span>·</span>
                <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">
                  {incident.failure_class}
                </code>
                <span>·</span>
                <span>
                  {incident.involved_object && (
                    <span className="font-mono">{incident.involved_object} &rarr; </span>
                  )}
                  <strong className="font-mono text-primary font-semibold">
                    {incident.target_resource}
                  </strong>
                </span>
              </div>
            </div>

            <div>
              {incident.post_incident_report_ready ? (
                <a
                  href={`/developer/incidents/${encodeURIComponent(incident.incident_id)}/report`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Button variant="outline" size="sm" className="gap-1.5 font-mono text-xs">
                    <HugeiconsIcon icon={FileCodeIcon} className="size-3.5 text-primary" />
                    Post-Incident Report
                  </Button>
                </a>
              ) : (
                <span className="text-xs text-muted-foreground font-mono">
                  Report guarded ({incident.execution.results.length} steps)
                </span>
              )}
            </div>
          </div>
        </section>

        {/* StateGraph Pipeline Stepper */}
        <PipelineStepper incident={incident} />

        {/* Error Strip if present */}
        {incident.error && (
          <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
            <HugeiconsIcon icon={Alert02Icon} className="size-4 shrink-0" />
            <span>{incident.error}</span>
          </div>
        )}

        {/* Human-in-the-Loop Confirmation Pause Card */}
        {incident.approval.is_interrupted && (
          <HitlConfirmation
            incident={incident}
            onActionComplete={() => {
              setActiveTab("execution")
            }}
          />
        )}

        {/* Workspace Detail Tabs */}
        <Tabs value={activeTab} onValueChange={(val) => setActiveTab(val as "diagnosis" | "execution")} className="space-y-4">
          <TabsList className="border border-border bg-card">
            <TabsTrigger value="diagnosis" className="gap-1.5 text-xs font-medium">
              <HugeiconsIcon icon={Brain02Icon} className="size-3.5" />
              <span>Diagnosis &amp; Plan</span>
            </TabsTrigger>
            <TabsTrigger value="execution" className="gap-1.5 text-xs font-medium">
              <HugeiconsIcon icon={ToolsIcon} className="size-3.5" />
              <span>Execution &amp; Terminal</span>
              <span className="ml-1 font-mono text-[11px] text-muted-foreground">
                ({incident.execution.results.length})
              </span>
            </TabsTrigger>
          </TabsList>

          <TabsContent value="diagnosis" className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <ThinkingTrace incident={incident} />
              <RemediationPlan incident={incident} />
            </div>
          </TabsContent>

          <TabsContent value="execution" className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <ToolExecution incident={incident} />
              <IncidentTerminal incident={incident} />
            </div>
          </TabsContent>
        </Tabs>
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        <p>NEXUS Autonomous SRE Control Plane · Thread Inspector</p>
      </footer>
    </div>
  )
}

export default function IncidentDetailPage() {
  return (
    <React.Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-background text-foreground">
          <Spinner className="size-6 text-primary" />
        </div>
      }
    >
      <IncidentDetailContent />
    </React.Suspense>
  )
}
