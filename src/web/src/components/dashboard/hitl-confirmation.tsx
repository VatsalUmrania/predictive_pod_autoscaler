"use client"

import * as React from "react"
import { Incident } from "@/types/incident"
import { useIncidents } from "@/context/incident-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
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
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Shield02Icon,
  CheckmarkCircle02Icon,
  Cancel01Icon,
  Loading03Icon,
} from "@hugeicons/core-free-icons"

interface HitlConfirmationProps {
  incident: Incident
  className?: string
  onActionComplete?: () => void
}

export function HitlConfirmation({
  incident,
  className,
  onActionComplete,
}: HitlConfirmationProps) {
  const { approveIncident, rejectIncident } = useIncidents()
  const [operatorNote, setOperatorNote] = React.useState("")
  const [isSubmitting, setIsSubmitting] = React.useState(false)
  const [approveOpen, setApproveOpen] = React.useState(false)
  const [rejectOpen, setRejectOpen] = React.useState(false)

  if (!incident.approval.is_interrupted) {
    return null
  }

  const handleApprove = async () => {
    setIsSubmitting(true)
    try {
      await approveIncident(
        incident.incident_id,
        operatorNote.trim() || "Approved from NEXUS control plane"
      )
      setApproveOpen(false)
      onActionComplete?.()
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleReject = async () => {
    setIsSubmitting(true)
    try {
      await rejectIncident(
        incident.incident_id,
        operatorNote.trim() || "Rejected and escalated from NEXUS control plane"
      )
      setRejectOpen(false)
      onActionComplete?.()
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div
      className={cn(
        "rounded-lg border border-chart-2/40 bg-chart-2/10 p-4 text-foreground shadow-xs transition-colors",
        className
      )}
    >
      <div className="flex flex-wrap items-center gap-2 pb-2">
        <HugeiconsIcon icon={Shield02Icon} className="size-5 text-chart-2" />
        <h3 className="font-semibold text-sm text-foreground">
          Human-in-the-Loop Sign-Off Required (approval_node)
        </h3>
        <span className="rounded border border-chart-2/40 bg-chart-2/20 px-2 py-0.5 font-mono text-[11px] font-semibold text-chart-2">
          PAUSED
        </span>
      </div>

      <p className="mt-1 text-xs text-muted-foreground leading-relaxed">
        {incident.policy_decision.reason ||
          "OPA policy requires verified human sign-off before executing mutation tools against production."}
      </p>

      <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex-1 min-w-0">
          <label htmlFor="operator-note" className="sr-only">
            Operator note or change ticket
          </label>
          <Input
            id="operator-note"
            type="text"
            placeholder="Operator change ticket or reasoning note (e.g. CHG-9482 approved)..."
            value={operatorNote}
            onChange={(e) => setOperatorNote(e.target.value)}
            disabled={isSubmitting}
            className="h-9 text-xs font-mono"
          />
        </div>

        <div className="flex items-center gap-2">
          {/* Approve Dialog */}
          <AlertDialog open={approveOpen} onOpenChange={setApproveOpen}>
            <AlertDialogTrigger
              render={
                <Button
                  variant="default"
                  size="sm"
                  disabled={isSubmitting}
                  className="gap-1.5"
                />
              }
            >
              {isSubmitting ? (
                <HugeiconsIcon
                  icon={Loading03Icon}
                  className="size-4 animate-spin"
                />
              ) : (
                <HugeiconsIcon
                  icon={CheckmarkCircle02Icon}
                  className="size-4"
                />
              )}
              <span>Approve &amp; Resume</span>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Confirm Remediation Execution</AlertDialogTitle>
                <AlertDialogDescription>
                  Are you sure you want to approve execution for{" "}
                  <strong>{incident.incident_id}</strong> on{" "}
                  <strong>{incident.target_resource}</strong>?
                  <br />
                  <br />
                  This will resume the LangGraph execution pipeline, run the
                  bound mutation tools (
                  <code>
                    {incident.remediation_plan.steps
                      .map((s) => s.tool_name)
                      .join(", ") || "remediation tools"}
                  </code>
                  ), and perform closed-loop verification.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={isSubmitting}>
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleApprove}
                  disabled={isSubmitting}
                  className="bg-primary text-primary-foreground hover:bg-primary/90"
                >
                  {isSubmitting ? "Executing..." : "Confirm & Resume"}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          {/* Reject Dialog */}
          <AlertDialog open={rejectOpen} onOpenChange={setRejectOpen}>
            <AlertDialogTrigger
              render={
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={isSubmitting}
                  className="gap-1.5"
                />
              }
            >
              <HugeiconsIcon icon={Cancel01Icon} className="size-4" />
              <span>Reject &amp; Escalate</span>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Reject &amp; Escalate Thread?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will abort autonomous remediation for{" "}
                  <strong>{incident.incident_id}</strong>, mark the thread as{" "}
                  <strong>ESCALATED</strong>, and alert on-call engineering.
                  <br />
                  <br />
                  {operatorNote ? (
                    <span className="italic">Note: &ldquo;{operatorNote}&rdquo;</span>
                  ) : (
                    "No operator note provided."
                  )}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={isSubmitting}>
                  Cancel
                </AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleReject}
                  disabled={isSubmitting}
                  variant="destructive"
                >
                  {isSubmitting ? "Submitting..." : "Reject & Escalate"}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>
    </div>
  )
}
