"use client"

import * as React from "react"
import { NexusIncident } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ToolsIcon,
  CheckmarkCircle02Icon,
  Alert02Icon,
  Clock01Icon,
  ArrowTurnBackwardIcon,
} from "@hugeicons/core-free-icons"

interface ToolExecutionProps {
  incident: NexusIncident
  className?: string
}

export function ToolExecution({ incident, className }: ToolExecutionProps) {
  const { execution, verification, rollback } = incident

  return (
    <Card className={cn("border-border bg-card", className)}>
      <CardHeader className="flex flex-row items-center justify-between gap-4 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <HugeiconsIcon
            icon={ToolsIcon}
            className="size-5 text-primary"
            strokeWidth={2}
          />
          <div>
            <CardTitle className="text-base font-semibold text-foreground">
              Tool Execution Stream &amp; Verification
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              Active mutation results &amp; closed-loop health probe feedback
            </p>
          </div>
        </div>
        <Badge
          variant={
            verification.verification_passed === true
              ? "default"
              : verification.verification_passed === false
              ? "destructive"
              : "secondary"
          }
          className="text-xs"
        >
          {verification.verification_passed === true
            ? "Verification: Passed"
            : verification.verification_passed === false
            ? "Verification: Failed"
            : "Verification: Pending"}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-6 pt-4">
        {/* Verification feedback alert */}
        {verification.verification_feedback && (
          <div
            className={cn(
              "rounded-md border p-3 text-xs",
              verification.verification_passed === true
                ? "border-primary/30 bg-primary/10 text-primary"
                : "border-destructive/30 bg-destructive/10 text-destructive"
            )}
          >
            <div className="flex items-center gap-1.5 font-semibold">
              <HugeiconsIcon
                icon={
                  verification.verification_passed === true
                    ? CheckmarkCircle02Icon
                    : Alert02Icon
                }
                className={cn(
                  "size-4",
                  verification.verification_passed === true
                    ? "text-primary"
                    : "text-destructive"
                )}
              />
              <span>Verification Feedback</span>
            </div>
            <p className="mt-1 leading-relaxed">
              {verification.verification_feedback}
            </p>
          </div>
        )}

        {/* Executed Tools List */}
        <div className="space-y-3">
          <span className="text-[11px] font-medium text-muted-foreground">
            Executed mutation calls ({execution.results.length})
          </span>

          {execution.results.length === 0 ? (
            <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
              No mutation tools have executed yet for this incident thread.
            </div>
          ) : (
            <div className="space-y-3">
              {execution.results.map((res, idx) => {
                const isSuccess = res.status === "SUCCESS"
                return (
                  <div
                    key={idx}
                    className="overflow-hidden rounded-md border border-border bg-background"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/30 px-3 py-2 text-xs">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-foreground">
                          Step {res.step ?? idx + 1}:
                        </span>
                        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">
                          {res.tool_name ?? "unnamed_tool"}
                        </code>
                      </div>
                      <div className="flex items-center gap-2 font-mono text-[11px]">
                        {res.duration_ms !== undefined && (
                          <span className="flex items-center gap-1 text-muted-foreground">
                            <HugeiconsIcon
                              icon={Clock01Icon}
                              className="size-3"
                            />
                            {res.duration_ms} ms
                          </span>
                        )}
                        <span
                          className={cn(
                            "rounded px-1.5 py-0.5 font-semibold font-mono text-[11px]",
                            isSuccess
                              ? "border border-primary/30 bg-primary/10 text-primary"
                              : "border border-destructive/30 bg-destructive/10 text-destructive"
                          )}
                        >
                          {res.status ?? "UNKNOWN"}
                        </span>
                      </div>
                    </div>
                    <div className="p-3">
                      <pre className="overflow-x-auto rounded border border-border bg-muted/40 p-2 font-mono text-xs text-foreground whitespace-pre-wrap">
                        {res.output ?? ""}
                      </pre>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Deterministic Rollback Results */}
        {rollback.rollback_results.length > 0 && (
          <div className="space-y-3 border-t border-border pt-4">
            <div className="flex items-center gap-1.5 text-[11px] font-medium text-destructive">
              <HugeiconsIcon
                icon={ArrowTurnBackwardIcon}
                className="size-4 text-destructive"
              />
              <span>Deterministic Rollback Results ({rollback.rollback_results.length})</span>
            </div>

            <div className="space-y-3">
              {rollback.rollback_results.map((rb, idx) => (
                <div
                  key={idx}
                  className="overflow-hidden rounded-md border border-destructive/30 bg-destructive/10"
                >
                  <div className="flex items-center justify-between border-b border-destructive/20 bg-destructive/20 px-3 py-2 text-xs font-mono">
                    <span className="font-semibold text-destructive">
                      Rollback Step {idx + 1}: {rb.tool_name ?? "unnamed_tool"}
                    </span>
                    <span className="text-destructive font-semibold">
                      {rb.status ?? "UNKNOWN"}
                    </span>
                  </div>
                  <div className="p-3">
                    <pre className="overflow-x-auto rounded border border-border bg-background p-2 font-mono text-xs text-foreground whitespace-pre-wrap">
                      {rb.output ?? ""}
                    </pre>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
