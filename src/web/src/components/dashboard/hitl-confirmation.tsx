"use client"

import * as React from "react"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Alert02Icon,
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
import { decideIncidentAction } from "@/lib/nexus/actions"
import type { NexusDecisionResponse, NexusIncident } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

type Decision = "approve" | "reject"

/**
 * Human-in-the-Loop sign-off for an incident paused at the approval node.
 *
 * The decision is posted to the NEXUS engine and the engine's real response is
 * rendered. Nothing about the outcome is assumed on the client.
 */
export function HitlConfirmation({
  incident,
  className,
}: {
  incident: NexusIncident
  className?: string
}) {
  const [note, setNote] = React.useState("")
  const [isPending, startTransition] = React.useTransition()
  const [result, setResult] = React.useState<
    | { ok: true; data: NexusDecisionResponse }
    | { ok: false; error: { message: string; status: number; path: string } }
    | null
  >(null)

  if (!incident.is_interrupted) return null

  const decide = (decision: Decision) => {
    const reason =
      note.trim() ||
      (decision === "approve"
        ? "Approved from the NEXUS control plane"
        : "Rejected and escalated from the NEXUS control plane")

    startTransition(async () => {
      setResult(
        await decideIncidentAction(incident.incident_id, decision === "approve", reason)
      )
    })
  }

  return (
    <section
      className={cn(
        "rounded-lg border border-chart-2/40 bg-chart-2/10 p-4 text-foreground",
        className
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <HugeiconsIcon icon={Shield02Icon} className="size-5 text-chart-2" />
        <h2 className="text-sm font-semibold">Operator sign-off required</h2>
        <span className="rounded border border-chart-2/40 bg-chart-2/20 px-2 py-0.5 font-mono text-[11px] font-semibold text-chart-2">
          PAUSED AT {incident.active_node}
        </span>
      </div>

      <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
        {incident.policy_decision.reason}
      </p>

      <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <label htmlFor="operator-note" className="sr-only">
            Operator note or change ticket
          </label>
          <Input
            id="operator-note"
            type="text"
            placeholder="Operator change ticket or reasoning note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            disabled={isPending}
            className="h-8 font-mono text-xs"
          />
        </div>

        <div className="flex shrink-0 items-center gap-2">
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
                <AlertDialogTitle>Reject this remediation?</AlertDialogTitle>
                <AlertDialogDescription>
                  The NEXUS engine will escalate incident{" "}
                  {incident.incident_id} instead of executing the plan.
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
                  onClick={() => decide("reject")}
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
              <HugeiconsIcon icon={CheckmarkCircle02Icon} className="size-3.5" />
              Approve
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Approve this remediation?</AlertDialogTitle>
                <AlertDialogDescription>
                  The NEXUS engine will resume incident {incident.incident_id} and
                  execute the remediation plan against {incident.target_resource}.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel className="h-8 text-xs">
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  size="sm"
                  className="h-8 border-chart-2/40 bg-chart-2/20 text-chart-2 hover:bg-chart-2/30"
                  onClick={() => decide("approve")}
                >
                  Approve and execute
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>

      {result ? <DecisionResult result={result} /> : null}
    </section>
  )
}

function DecisionResult({
  result,
}: {
  result:
    | { ok: true; data: NexusDecisionResponse }
    | { ok: false; error: { message: string; status: number; path: string } }
}) {
  if (!result.ok) {
    return (
      <div
        role="alert"
        className="mt-3 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
      >
        <HugeiconsIcon icon={Alert02Icon} className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0">
          <p className="font-semibold">The decision was not recorded</p>
          <p className="text-destructive/90">{result.error.message}</p>
          {result.error.path ? (
            <p className="mt-1 truncate font-mono text-[11px]">
              {result.error.status ? `HTTP ${result.error.status} ` : ""}
              {result.error.path}
            </p>
          ) : null}
        </div>
      </div>
    )
  }

  const { data } = result
  return (
    <div className="mt-3 rounded-md border border-border bg-card p-3 text-xs">
      <p className="font-semibold text-foreground">
        Engine responded: {data.status}
      </p>
      <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1.5 sm:grid-cols-2">
        <div className="flex gap-2">
          <dt className="text-muted-foreground">Approved by</dt>
          <dd className="font-mono">{data.approved_by || "-"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="text-muted-foreground">Incident</dt>
          <dd className="font-mono">{data.incident_id}</dd>
        </div>
        <div className="flex gap-2 sm:col-span-2">
          <dt className="shrink-0 text-muted-foreground">Reason</dt>
          <dd className="font-mono">{data.reason || "-"}</dd>
        </div>
      </dl>
      {data.outcome && Object.keys(data.outcome).length > 0 ? (
        <pre className="mt-2 overflow-x-auto rounded bg-muted p-2 font-mono text-[11px] leading-relaxed">
          {JSON.stringify(data.outcome, null, 2)}
        </pre>
      ) : null}
    </div>
  )
}
