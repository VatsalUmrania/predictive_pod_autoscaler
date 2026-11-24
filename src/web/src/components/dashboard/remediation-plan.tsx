"use client"

import * as React from "react"
import { NexusIncident } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Shield02Icon,
} from "@hugeicons/core-free-icons"

interface RemediationPlanProps {
  incident: NexusIncident
  className?: string
}

export function RemediationPlan({ incident, className }: RemediationPlanProps) {
  const { policy_decision, remediation_plan, approval } = incident

  return (
    <Card className={cn("border-border bg-card", className)}>
      <CardHeader className="flex flex-row items-center justify-between gap-4 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <HugeiconsIcon
            icon={Shield02Icon}
            className="size-5 text-primary"
            strokeWidth={2}
          />
          <div>
            <CardTitle className="text-base font-semibold text-foreground">
              Schema-Bound Plan &amp; OPA Policy (plan_node)
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              Deterministic guardrails &amp; blast-radius assessment
            </p>
          </div>
        </div>
        <Badge
          variant={
            policy_decision.allowed
              ? policy_decision.requires_approval
                ? "secondary"
                : "default"
              : "destructive"
          }
          className="text-xs"
        >
          {policy_decision.allowed
            ? policy_decision.requires_approval
              ? "HITL sign-off required"
              : "Auto-approved"
            : "Policy denied"}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-6 pt-4">
        {/* Policy Verdict and Governance properties */}
        <div className="grid grid-cols-1 gap-3 rounded-md border border-border bg-muted/20 p-3 sm:grid-cols-2 text-xs">
          <div>
            <span className="font-medium text-muted-foreground">Policy Verdict</span>
            <p className="mt-0.5 text-foreground">{policy_decision.reason}</p>
          </div>
          <div>
            <span className="font-medium text-muted-foreground">Governance Decision</span>
            <p className="mt-0.5 text-foreground">
              {approval.approved_by ? (
                <span>
                  {approval.approved_by} ({approval.approval_status})
                </span>
              ) : (
                <span className="text-muted-foreground">Pending operator review</span>
              )}
            </p>
          </div>
          {approval.approval_reason && (
            <div className="sm:col-span-2">
              <span className="font-medium text-muted-foreground">Operator Note</span>
              <p className="mt-0.5 italic text-foreground">{approval.approval_reason}</p>
            </div>
          )}
        </div>

        {/* Plan Rationale */}
        <div className="space-y-1.5">
          <span className="text-[11px] font-medium text-muted-foreground">
            Remediation rationale
          </span>
          <p className="rounded-md border border-border bg-background p-3 text-sm text-foreground">
            {remediation_plan.rationale}
          </p>
        </div>

        {/* Remediation Steps */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              Planned steps ({remediation_plan.steps.length})
            </span>
          </div>

          {remediation_plan.steps.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No mutation tool calls bound for this incident.
            </p>
          ) : (
            <div className="space-y-3">
              {remediation_plan.steps.map((step, idx) => {
                const isReversible = step.reversible !== false
                const blastRadius = step.blast_radius ?? "unspecified"
                const toolName = step.tool_name ?? "unnamed_tool"

                return (
                  <div
                    key={idx}
                    className="overflow-hidden rounded-md border border-border bg-background"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/30 px-3 py-2 text-xs">
                      <div className="flex items-center gap-2">
                        <span className="flex size-5 items-center justify-center rounded-full bg-primary/10 font-mono text-[11px] font-bold text-primary">
                          {step.step ?? idx + 1}
                        </span>
                        <code className="rounded bg-muted px-1.5 py-0.5 font-mono font-medium text-foreground">
                          {toolName}
                        </code>
                      </div>
                      <div className="flex items-center gap-2 font-mono text-[11px]">
                        <span
                          className={cn(
                            "rounded px-1.5 py-0.5",
                            blastRadius === "service_wide"
                              ? "bg-chart-2/10 text-chart-2"
                              : "bg-muted text-muted-foreground"
                          )}
                        >
                          blast: {blastRadius}
                        </span>
                        <span
                          className={cn(
                            "rounded px-1.5 py-0.5",
                            isReversible
                              ? "bg-primary/10 text-primary"
                              : "bg-destructive/10 text-destructive"
                          )}
                        >
                          {isReversible ? "reversible" : "irreversible"}
                        </span>
                      </div>
                    </div>
                    <div className="p-3">
                      <span className="block mb-1 text-[11px] font-medium text-muted-foreground">
                        Tool Arguments:
                      </span>
                      <pre className="overflow-x-auto rounded border border-border bg-muted/40 p-2 font-mono text-xs text-foreground">
                        {JSON.stringify(
                          step.tool_args || step.parameters || {},
                          null,
                          2
                        )}
                      </pre>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
