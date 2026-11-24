"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import { GRAPH_NODES, type NexusIncident } from "@/lib/nexus/types"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  CheckmarkCircle02Icon,
  Alert02Icon,
  Loading03Icon,
  PauseIcon,
} from "@hugeicons/core-free-icons"

const NODE_LABELS: Record<string, string> = {
  TRIAGE: "Triage",
  DIAGNOSE: "Diagnose",
  PLAN: "Plan",
  GOVERN: "Govern",
  APPROVAL: "Approval",
  EXECUTE: "Execute",
  VERIFY: "Verify",
  LEARN: "Learn",
  ROLLBACK: "Rollback",
  ESCALATE: "Escalate",
}

interface PipelineStepperProps {
  incident: NexusIncident
  className?: string
}

type NodeStatus =
  | "visited"
  | "active"
  | "interrupted"
  | "skipped"
  | "failed"
  | "pending"

export function PipelineStepper({ incident, className }: PipelineStepperProps) {
  const nodeIds = GRAPH_NODES as readonly string[]
  const activeIdx = nodeIds.indexOf(incident.active_node)

  const getNodeStatus = (nodeId: string): NodeStatus => {
    const idx = nodeIds.indexOf(nodeId)

    if (nodeId === "APPROVAL") {
      if (incident.is_interrupted) return "interrupted"
      if (incident.approval_status === "REJECTED") return "failed"
      if (incident.approval_status === "AUTO_APPROVED") return "skipped"
      if (incident.approval_status === "APPROVED") return "visited"
    }

    if (incident.status === "RESOLVED") return "visited"

    if (incident.active_node === "ROLLBACK" || incident.active_node === "ESCALATE") {
      if (nodeId === "TRIAGE") return "visited"
      if (nodeId === "DIAGNOSE")
        return incident.confidence > 0 || incident.failure_class !== "investigating"
          ? "visited"
          : "failed"
      if (nodeId === "PLAN")
        return incident.remediation_plan.steps.length > 0 ? "visited" : "failed"
      if (nodeId === "GOVERN")
        return incident.policy_decision.allowed === false ? "failed" : "visited"
      if (nodeId === "APPROVAL") {
        if (incident.approval_status === "APPROVED") return "visited"
        if (incident.approval_status === "AUTO_APPROVED") return "skipped"
        if (incident.approval_status === "REJECTED") return "failed"
        return "pending"
      }
      if (nodeId === "EXECUTE") {
        return incident.execution.results.length > 0 ? "visited" : "pending"
      }
      if (nodeId === "VERIFY") {
        if (incident.verification.verification_passed === false) return "failed"
        if (incident.verification.verification_passed === true) return "visited"
        return "pending"
      }
      return "pending"
    }

    if (activeIdx !== -1) {
      if (idx < activeIdx) return "visited"
      if (idx === activeIdx) {
        if (
          ["EXECUTION_FAILED", "VERIFICATION_FAILED", "POLICY_DENIED"].includes(
            incident.status
          )
        ) {
          return "failed"
        }
        return "active"
      }
    }

    return "pending"
  }

  const branchNote =
    incident.active_node === "ROLLBACK" || incident.rollback.rollback_results.length > 0
      ? " · Branch: Rollback"
      : incident.active_node === "ESCALATE"
        ? " · Branch: Escalated"
        : ""

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card p-4 transition-colors",
        className
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3 text-xs">
        <span className="font-medium text-foreground">StateGraph execution pipeline</span>
        <span className="font-mono text-muted-foreground">
          Attempt {incident.remediation_attempts} of {incident.max_remediation_attempts}
          {branchNote}
        </span>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        {nodeIds.map((nodeId) => {
          const status = getNodeStatus(nodeId)

          return (
            <div
              key={nodeId}
              className={cn(
                "group relative flex flex-col items-center justify-center rounded-md border p-2 text-center text-xs transition-all",
                status === "visited" && "border-primary/30 bg-primary/10 text-primary",
                status === "active" &&
                  "border-primary bg-primary/10 font-semibold text-primary shadow-xs ring-1 ring-ring/30",
                status === "interrupted" &&
                  "border-chart-2/40 bg-chart-2/10 font-semibold text-chart-2",
                status === "failed" &&
                  "border-destructive/30 bg-destructive/10 font-semibold text-destructive",
                status === "skipped" &&
                  "border-border bg-muted/40 text-muted-foreground",
                status === "pending" &&
                  "border-border bg-card/60 text-muted-foreground/60"
              )}
            >
              <div className="mb-1 flex size-5 items-center justify-center">
                {status === "visited" && (
                  <HugeiconsIcon
                    icon={CheckmarkCircle02Icon}
                    className="size-4 text-primary"
                  />
                )}
                {status === "active" && (
                  <HugeiconsIcon
                    icon={Loading03Icon}
                    className="size-4 animate-spin text-primary"
                  />
                )}
                {status === "interrupted" && (
                  <HugeiconsIcon
                    icon={PauseIcon}
                    className="size-4 text-chart-2"
                  />
                )}
                {status === "failed" && (
                  <HugeiconsIcon
                    icon={Alert02Icon}
                    className="size-4 text-destructive"
                  />
                )}
                {status === "skipped" && (
                  <span className="font-mono text-[10px] uppercase text-muted-foreground">
                    Skip
                  </span>
                )}
                {status === "pending" && (
                  <span className="size-1.5 rounded-full bg-muted-foreground/40" />
                )}
              </div>
              <span className="truncate">{NODE_LABELS[nodeId] ?? nodeId}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
