"use client"

import * as React from "react"
import Link from "next/link"
import { useIncidents } from "@/context/incident-context"
import { NavigationHeader } from "@/components/dashboard/navigation-header"
import { TelemetryStrip } from "@/components/dashboard/telemetry-strip"
import { IncidentQueue } from "@/components/dashboard/incident-queue"
import { PredictionsTable } from "@/components/dashboard/predictions-table"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Shield02Icon,
  ArrowRight01Icon,
  CheckmarkCircle02Icon,
  CpuIcon,
  SecurityCheckIcon,
  Alert02Icon,
  Clock01Icon,
} from "@hugeicons/core-free-icons"
import { cn } from "@/lib/utils"

export default function Home() {
  const { incidents, predictions, isOnline, isDemoMode } = useIncidents()
  const awaitingIncidents = incidents.filter((i) => i.approval.is_interrupted)
  const resolvedCount = incidents.filter((i) => i.status === "RESOLVED").length

  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <NavigationHeader />

      <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-6">
        {/* Control Center Page Header */}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between border-b border-border pb-4">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold tracking-tight text-foreground font-heading">
                Control Center
              </h1>
              <span className="rounded-full border border-border bg-muted/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                production-us-east-1
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {awaitingIncidents.length > 0
                ? `${awaitingIncidents.length} workload requires operator sign-off · ${resolvedCount} self-healed · ${predictions.length} proactive scaling actions`
                : incidents.length > 0
                ? `All ${incidents.length} workloads healthy · ${resolvedCount} self-healed · ${predictions.length} proactive scaling actions`
                : !isOnline && !isDemoMode
                ? "Live API offline · Toggle Preview Mode to explore sample telemetry"
                : "All workloads nominal · 0 active incidents"}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-xs font-mono text-muted-foreground">
              <span
                className={cn(
                  "size-2 rounded-full",
                  isDemoMode
                    ? "bg-chart-2"
                    : isOnline
                    ? "bg-primary"
                    : "bg-destructive"
                )}
              />
              <span>
                {isDemoMode
                  ? "Preview Mode"
                  : isOnline
                  ? "Cluster Active"
                  : "Engine Offline"}
              </span>
            </span>
            <span className="rounded-md border border-border bg-muted/40 px-2.5 py-1 text-xs font-mono text-muted-foreground">
              Kubernetes v1.31
            </span>
          </div>
        </div>

        {/* Telemetry KPI Strip */}
        <TelemetryStrip />

        {/* Urgent Human-in-the-Loop Callout Banner if needed */}
        {awaitingIncidents.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-chart-2/40 bg-chart-2/10 p-4 text-xs">
            <div className="flex items-center gap-2.5">
              <div className="flex size-7 shrink-0 items-center justify-center rounded-md border border-chart-2/30 bg-chart-2/20 text-chart-2">
                <HugeiconsIcon icon={Shield02Icon} className="size-4" />
              </div>
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-foreground">
                    Operator Intervention Required ({awaitingIncidents.length} thread)
                  </span>
                  <span className="rounded bg-chart-2/20 px-1.5 py-0.2 font-mono text-[10px] font-semibold text-chart-2">
                    APPROVAL_NODE
                  </span>
                </div>
                <p className="text-muted-foreground">
                  {awaitingIncidents[0]?.policy_decision.reason ||
                    "OPA policy requires manual approval for service-wide production mutations."}
                </p>
              </div>
            </div>

            <Link href="/approvals">
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs font-mono border-chart-2/40 text-chart-2 hover:bg-chart-2/20 gap-1"
              >
                <span>Review &amp; Sign-Off</span>
                <HugeiconsIcon icon={ArrowRight01Icon} className="size-3.5" />
              </Button>
            </Link>
          </div>
        )}

        {/* Monitored Workloads & Pod Topology Status Matrix */}
        <section aria-labelledby="workloads-heading" className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 id="workloads-heading" className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Monitored Kubernetes Deployments
              </h2>
            </div>
            <span className="text-[11px] font-mono text-muted-foreground">
              {incidents.length} Target Workloads
            </span>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {incidents.length === 0 ? (
              <div className="col-span-full rounded-lg border border-dashed border-border bg-card/50 p-8 text-center text-xs text-muted-foreground">
                {!isOnline && !isDemoMode
                  ? "API Unreachable: No live Kubernetes deployments reporting. Connect NEXUS engine or switch to Preview Mode."
                  : "No active Kubernetes deployments currently reporting incidents."}
              </div>
            ) : (
              incidents.map((inc) => {
                const isInterrupted = inc.approval.is_interrupted
                const isResolved = inc.status === "RESOLVED"

                // Topology Pod indicator visualization
                const desiredPods = inc.resource_kind === "StatefulSet" ? 4 : 3
                const readyPods = isInterrupted ? 0 : desiredPods

                return (
                  <Link
                    key={inc.incident_id}
                    href={`/incidents/${encodeURIComponent(inc.incident_id)}`}
                    className={cn(
                      "group flex flex-col justify-between rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/40 hover:bg-muted/10",
                      isInterrupted && "border-chart-2/40 bg-chart-2/5 hover:border-chart-2/60"
                    )}
                  >
                    <div className="space-y-3">
                      {/* Header Row: Target Resource + Status Pill */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <code className="truncate font-mono text-xs font-semibold text-foreground group-hover:text-primary transition-colors">
                            {inc.target_resource}
                          </code>
                        </div>
                        {isInterrupted ? (
                          <span className="shrink-0 rounded border border-chart-2/40 bg-chart-2/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-chart-2">
                            PAUSED (APPROVAL)
                          </span>
                        ) : isResolved ? (
                          <span className="shrink-0 rounded border border-primary/30 bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-primary">
                            SELF-HEALED
                          </span>
                        ) : (
                          <span className="shrink-0 rounded border border-destructive/30 bg-destructive/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-destructive">
                            {inc.status}
                          </span>
                        )}
                      </div>

                      {/* Pod Replica Topology Indicator */}
                      <div className="flex items-center justify-between rounded-md border border-border/60 bg-muted/30 px-2.5 py-1.5 text-xs font-mono">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11px] text-muted-foreground">Pod Replicas:</span>
                          <div className="flex items-center gap-1" aria-hidden="true">
                            {Array.from({ length: desiredPods }).map((_, i) => (
                              <span
                                key={i}
                                className={cn(
                                  "size-2 rounded-full",
                                  i < readyPods
                                    ? "bg-primary"
                                    : isInterrupted
                                    ? "border border-chart-2/60 bg-transparent"
                                    : "bg-destructive"
                                )}
                              />
                            ))}
                          </div>
                        </div>
                        <span className={cn(
                          "text-[11px] font-bold tabular-nums",
                          readyPods === desiredPods ? "text-primary" : "text-chart-2"
                        )}>
                          {readyPods}/{desiredPods} Ready
                        </span>
                      </div>

                      {/* Root Cause / Diagnosis */}
                      <p className="line-clamp-2 text-xs text-muted-foreground leading-relaxed">
                        {inc.suspected_cause}
                      </p>
                    </div>

                    {/* Footer Meta Row */}
                    <div className="mt-4 flex items-center justify-between border-t border-border pt-2.5 text-[11px] font-mono text-muted-foreground">
                      <div className="flex items-center gap-1.5">
                        <HugeiconsIcon
                          icon={isResolved ? CheckmarkCircle02Icon : isInterrupted ? Clock01Icon : Alert02Icon}
                          className={cn(
                            "size-3.5",
                            isResolved ? "text-primary" : isInterrupted ? "text-chart-2" : "text-destructive"
                          )}
                        />
                        <span>Thread {inc.incident_id}</span>
                      </div>
                      <span className="group-hover:text-primary transition-colors flex items-center gap-1">
                        Inspect
                        <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
                      </span>
                    </div>
                  </Link>
                )
              })
            )}
          </div>
        </section>

        {/* 2-Column Split: Recent Incident Activity & Predictive Pod Autoscaler */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          {/* Recent Incident Threads Summary */}
          <section aria-labelledby="recent-threads-heading" className="space-y-3">
            <div className="flex items-center justify-between border-b border-border pb-2">
              <div>
                <h2 id="recent-threads-heading" className="text-sm font-bold tracking-tight text-foreground font-heading">
                  Recent Incident Interventions
                </h2>
                <p className="text-[11px] text-muted-foreground">
                  LangGraph ReAct diagnostic loops &amp; mutation actions
                </p>
              </div>
              <Link href="/threads">
                <Button variant="ghost" size="sm" className="h-7 text-xs text-primary hover:text-primary/90 font-mono gap-1">
                  <span>View All ({incidents.length})</span>
                  <HugeiconsIcon icon={ArrowRight01Icon} className="size-3.5" />
                </Button>
              </Link>
            </div>

            <IncidentQueue limit={3} showFilters={false} />
          </section>

          {/* Predictive Pod Autoscaler Preview */}
          <section aria-labelledby="predictions-preview-heading" className="space-y-3">
            <div className="flex items-center justify-between border-b border-border pb-2">
              <div>
                <h2
                  id="predictions-preview-heading"
                  className="text-sm font-bold tracking-tight text-foreground font-heading"
                >
                  Predictive Pod Autoscaler (PPA)
                </h2>
                <p className="text-[11px] text-muted-foreground">
                  ML workload load forecasting &amp; proactive replica scaling
                </p>
              </div>
              <Link href="/predictions">
                <Button variant="ghost" size="sm" className="h-7 text-xs text-primary hover:text-primary/90 font-mono gap-1">
                  <span>View All ({predictions.length})</span>
                  <HugeiconsIcon icon={ArrowRight01Icon} className="size-3.5" />
                </Button>
              </Link>
            </div>

            <PredictionsTable limit={3} />
          </section>
        </div>

        {/* Governance & Autonomous Safety Guardrails */}
        <section aria-labelledby="guardrails-heading" className="rounded-lg border border-border bg-card p-5 space-y-4">
          <div className="flex items-center gap-2">
            <HugeiconsIcon icon={SecurityCheckIcon} className="size-5 text-primary" />
            <div>
              <h2 id="guardrails-heading" className="text-sm font-bold tracking-tight text-foreground">
                Engine Guardrails &amp; Safety Guarantees
              </h2>
              <p className="text-xs text-muted-foreground">
                Deterministic policy gates enforcing zero unverified production state drift
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 text-xs">
            <div className="rounded-md border border-border bg-background p-3 space-y-1">
              <div className="flex items-center gap-1.5 font-semibold text-foreground">
                <HugeiconsIcon icon={Shield02Icon} className="size-4 text-chart-2" />
                <span>OPA Policy Enforcement</span>
              </div>
              <p className="text-muted-foreground leading-relaxed">
                Service-wide mutations require mandatory operator sign-off before executing in production namespaces.
              </p>
            </div>

            <div className="rounded-md border border-border bg-background p-3 space-y-1">
              <div className="flex items-center gap-1.5 font-semibold text-foreground">
                <HugeiconsIcon icon={CheckmarkCircle02Icon} className="size-4 text-primary" />
                <span>Closed-Loop Verification</span>
              </div>
              <p className="text-muted-foreground leading-relaxed">
                100% of remediation actions are actively verified against live readiness health probes before marking resolved.
              </p>
            </div>

            <div className="rounded-md border border-border bg-background p-3 space-y-1">
              <div className="flex items-center gap-1.5 font-semibold text-foreground">
                <HugeiconsIcon icon={CpuIcon} className="size-4 text-primary" />
                <span>Deterministic Reversibility</span>
              </div>
              <p className="text-muted-foreground leading-relaxed">
                Rollback actions are pre-computed in the remediation plan to immediately revert failures upon probe timeout.
              </p>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        <p>NEXUS Autonomous SRE Control Plane · Powered by Next.js &amp; LangGraph</p>
      </footer>
    </div>
  )
}
