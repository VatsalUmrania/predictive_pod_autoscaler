"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import { Incident, GRAPH_NODES, GraphNodeId } from "@/types/incident"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  CheckmarkCircle02Icon,
  Alert02Icon,
  Loading03Icon,
  PauseIcon,
} from "@hugeicons/core-free-icons"

interface PipelineStepperProps {
  incident: Incident
  className?: string
}

type NodeStatus = "visited" | "active" | "interrupted" | "skipped" | "failed" | "pending"

export function PipelineStepper({ incident, className }: PipelineStepperProps) {
  const nodeIds = GRAPH_NODES.map((n) => n.id)
  const activeIdx = nodeIds.indexOf(incident.active_node as GraphNodeId)

  const getNodeStatus = (nodeId: GraphNodeId): NodeStatus => {
    const idx = nodeIds.indexOf(nodeId)

    if (nodeId === "APPROVAL") {
      if (incident.approval.is_interrupted) return "interrupted"
      if (incident.approval.approval_status === "REJECTED") return "failed"
      if (incident.approval.approval_status === "AUTO_APPROVED") return "skipped"
      if (incident.approval.approval_status === "APPROVED") return "visited"
    }

    if (incident.status === "RESOLVED") {
      return "visited"
    }

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
        if (incident.approval.approval_status === "APPROVED") return "visited"
        if (incident.approval.approval_status === "AUTO_APPROVED") return "skipped"
        if (incident.approval.approval_status === "REJECTED") return "failed"
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
    incident.active_node === "ROLLBACK" || incident.rollback.results.length > 0
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
        <span className="font-medium text-foreground">
          StateGraph Execution Pipeline
        </span>
        <span className="font-mono text-muted-foreground">
          Attempt {incident.execution.remediation_attempts} of{" "}
          {incident.execution.max_remediation_attempts}
          {branchNote}
        </span>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        {GRAPH_NODES.map((node) => {
          const status = getNodeStatus(node.id)

          return (
            <div
              key={node.id}
              className={cn(
                "group relative flex flex-col items-center justify-center rounded-md border p-2 text-center text-xs transition-all",
                status === "visited" &&
                  "border-primary/30 bg-primary/10 text-primary",
                status === "active" &&
                  "border-primary bg-primary/10 font-semibold text-primary shadow-xs ring-1 ring-ring/30",
                status === "interrupted" &&
                  "border-chart-2/40 bg-chart-2/10 font-semibold text-chart-2 animate-pulse",
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
                  <span className="text-[10px] uppercase font-mono text-muted-foreground">
                    Skip
                  </span>
                )}
                {status === "pending" && (
                  <span className="size-1.5 rounded-full bg-muted-foreground/40" />
                )}
              </div>
              <span className="truncate">{node.label}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
