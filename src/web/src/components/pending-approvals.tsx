"use client"

import * as React from "react"
import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Alert02Icon,
  ArrowRight01Icon,
  Cancel01Icon,
  CheckmarkCircle02Icon,
  Shield02Icon,
} from "@hugeicons/core-free-icons"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { EmptyState } from "@/components/empty-state"
import { decideIncidentAction } from "@/lib/nexus/actions"
import { formatPercent, formatTimestamp } from "@/lib/format"
import type {
  NexusDecisionResponse,
  NexusIncident,
  NexusPendingApproval,
} from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

type Decision = "approve" | "reject"

type DecisionResult =
  | { ok: true; data: NexusDecisionResponse }
  | { ok: false; error: { message: string; status: number; path: string } }

/**
 * The human approval queue.
 *
 * Entries come straight from `GET /approvals/pending`, which merges LangGraph
 * interrupt records with HumanApprovalQueue entries. When the entry names an
 * incident that the incident feed also knows about, the incident supplies the
 * policy reason and remediation plan.
 */
export function PendingApprovals({
  approvals,
  incidents,
  className,
}: {
  approvals: NexusPendingApproval[]
  incidents: NexusIncident[]
  className?: string
}) {
  const [notes, setNotes] = React.useState<Record<string, string>>({})
  const [results, setResults] = React.useState<Record<string, DecisionResult>>({})
  const [isPending, startTransition] = React.useTransition()

  const incidentById = React.useMemo(() => {
    const map = new Map<string, NexusIncident>()
    for (const incident of incidents) map.set(incident.incident_id, incident)
    return map
  }, [incidents])

  const decide = (approval: NexusPendingApproval, decision: Decision) => {
    const incidentId = approval.incident_id
    if (!incidentId) return

    const key = approval.approval_id ?? incidentId
    const note = notes[key]?.trim() ?? ""
    const reason =
      note ||
      (decision === "approve"
        ? "Approved from the NEXUS control plane"
        : "Rejected and escalated from the NEXUS control plane")

    startTransition(async () => {
      const result = await decideIncidentAction(incidentId, decision === "approve", reason)
      setResults((previous) => ({ ...previous, [key]: result }))
    })
  }

  if (approvals.length === 0) {
    return (
      <EmptyState
        title="No approvals are pending"
        description="No incident is currently paused at the approval node. The engine is free to execute its remediation plans."
        icon={Shield02Icon}
        className={className}
      />
    )
  }

  return (
    <div className={cn("space-y-3", className)}>
      {approvals.map((approval, index) => {
        const key = approval.approval_id ?? approval.incident_id ?? String(index)
        const incident = approval.incident_id
          ? incidentById.get(approval.incident_id)
          : undefined
        const context = approval.context ?? {}
        const target =
          approval.target ??
          (typeof context.target_resource === "string"
            ? context.target_resource
            : undefined)
        const serviceName =
          typeof context.service_name === "string" ? context.service_name : undefined
        const namespace =
          typeof context.namespace === "string" ? context.namespace : undefined
        const failureClass =
          typeof context.failure_class === "string" ? context.failure_class : undefined
        const suspectedCause =
          typeof context.suspected_cause === "string"
            ? context.suspected_cause
            : undefined
        const result = results[key]

        return (
          <article
            key={key}
            className="rounded-lg border border-chart-2/40 bg-card p-4"
          >
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  {approval.incident_id ? (
                    <Link
                      href={`/incidents/${encodeURIComponent(approval.incident_id)}`}
                      className="font-mono text-sm font-semibold underline-offset-4 hover:text-primary hover:underline"
                    >
                      {approval.incident_id}
                    </Link>
                  ) : (
                    <span className="font-mono text-sm font-semibold text-muted-foreground">
                      {approval.approval_id ?? "unknown approval"}
                    </span>
                  )}
                  {approval.action_type ? (
                    <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                      {approval.action_type}
                    </span>
                  ) : null}
                  {approval.runbook_id ? (
                    <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                      {approval.runbook_id}
                    </span>
                  ) : null}
                  {approval.healing_level !== undefined ? (
                    <span className="font-mono text-[11px] text-muted-foreground">
                      level {approval.healing_level}
                    </span>
                  ) : null}
                  {approval.confidence !== undefined ? (
                    <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                      {formatPercent(approval.confidence)} confidence
                    </span>
                  ) : null}
                </div>

                {target ? (
                  <p className="mt-2 font-mono text-xs text-foreground">{target}</p>
                ) : null}

                <p className="mt-1 text-xs text-muted-foreground">
                  {[serviceName, namespace, failureClass]
                    .filter(Boolean)
                    .join(" / ")}
                </p>

                {suspectedCause ? (
                  <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                    {suspectedCause}
                  </p>
                ) : null}

                {incident?.policy_decision.reason ? (
                  <p className="mt-2 text-xs leading-relaxed text-chart-2">
                    {incident.policy_decision.reason}
                  </p>
                ) : null}

                {approval.reasons && approval.reasons.length > 0 ? (
                  <ul className="mt-2 space-y-1">
                    {approval.reasons.map((reason) => (
                      <li
                        key={reason}
                        className="flex gap-2 text-xs text-muted-foreground"
                      >
                        <span aria-hidden="true" className="text-chart-2">
                          {reason}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : null}

                {approval.enqueued_at ? (
                  <p className="mt-2 font-mono text-[11px] tabular-nums text-muted-foreground">
                    queued {formatTimestamp(approval.enqueued_at)}
                  </p>
                ) : null}
              </div>

              {approval.incident_id ? (
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row lg:flex-col">
                  <Input
                    aria-label={`Operator note for ${approval.incident_id}`}
                    placeholder="Change ticket or note"
                    value={notes[key] ?? ""}
                    onChange={(event) =>
                      setNotes((previous) => ({
                        ...previous,
                        [key]: event.target.value,
                      }))
                    }
                    disabled={isPending}
                    className="h-8 font-mono text-xs lg:w-52"
                  />
                  <div className="flex items-center gap-2">
                    <AlertDialog>
                      <AlertDialogTrigger
                        render={
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={isPending}
                            className="h-8 border-destructive/40 text-destructive hover:bg-destructive/10"
                          />
                        }
                      >
                        <HugeiconsIcon icon={Cancel01Icon} className="size-3.5" />
                        Reject
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>
                            Reject this remediation?
                          </AlertDialogTitle>
                          <AlertDialogDescription>
                            The NEXUS engine will escalate incident{" "}
                            {approval.incident_id} instead of executing the plan.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel className="h-8 text-xs">
                            Cancel
                          </AlertDialogCancel>
                          <AlertDialogAction
                            variant="destructive"
                            size="sm"
                            className="h-8 text-xs"
                            onClick={() => decide(approval, "reject")}
                          >
                            Reject and escalate
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>

                    <AlertDialog>
                      <AlertDialogTrigger
                        render={
                          <Button
                            size="sm"
                            disabled={isPending}
                            className="h-8 border-chart-2/40 bg-chart-2/20 text-chart-2 hover:bg-chart-2/30"
                          />
                        }
                      >
                        <HugeiconsIcon
                          icon={CheckmarkCircle02Icon}
                          className="size-3.5"
                        />
                        Approve
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>
                            Approve this remediation?
                          </AlertDialogTitle>
                          <AlertDialogDescription>
                            The NEXUS engine will resume incident{" "}
                            {approval.incident_id} and execute its remediation plan.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel className="h-8 text-xs">
                            Cancel
                          </AlertDialogCancel>
                          <AlertDialogAction
                            size="sm"
                            className="h-8 border-chart-2/40 bg-chart-2/20 text-chart-2 hover:bg-chart-2/30"
                            onClick={() => decide(approval, "approve")}
                          >
                            Approve and execute
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </div>
                </div>
              ) : (
                <p className="shrink-0 text-xs text-muted-foreground">
                  This entry has no incident id, so it cannot be decided from the
                  dashboard.
                </p>
              )}
            </div>

            {result ? (
              <div
                role="status"
                className={cn(
                  "mt-3 rounded-md border p-3 text-xs",
                  result.ok
                    ? "border-border bg-muted/40"
                    : "border-destructive/30 bg-destructive/10 text-destructive"
                )}
              >
                {result.ok ? (
                  <>
                    <p className="font-semibold text-foreground">
                      Engine responded: {result.data.status}
                    </p>
                    <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                      approved_by {result.data.approved_by || "-"}
                    </p>
                    {result.data.outcome &&
                    Object.keys(result.data.outcome).length > 0 ? (
                      <pre className="mt-2 overflow-x-auto rounded bg-background p-2 font-mono text-[11px] leading-relaxed">
                        {JSON.stringify(result.data.outcome, null, 2)}
                      </pre>
                    ) : null}
                  </>
                ) : (
                  <div className="flex items-start gap-2">
                    <HugeiconsIcon icon={Alert02Icon} className="mt-0.5 size-4 shrink-0" />
                    <div className="min-w-0">
                      <p className="font-semibold">The decision was not recorded</p>
                      <p>{result.error.message}</p>
                      {result.error.path ? (
                        <p className="mt-1 truncate font-mono text-[11px]">
                          {result.error.status
                            ? `HTTP ${result.error.status} `
                            : ""}
                          {result.error.path}
                        </p>
                      ) : null}
                    </div>
                  </div>
                )}
              </div>
            ) : null}

            {approval.incident_id ? (
              <Link
                href={`/incidents/${encodeURIComponent(approval.incident_id)}`}
                className="mt-3 inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-primary"
              >
                Inspect thread
                <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
              </Link>
            ) : null}
          </article>
        )
      })}
    </div>
  )
}
