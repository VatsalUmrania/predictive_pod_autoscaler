"use client"

import * as React from "react"
import { Incident } from "@/types/incident"
import { cn } from "@/lib/utils"
import {
  Trace,
  TraceLine,
  TraceIcon,
  TraceTitle,
  TraceDescription,
  TraceCollapsible,
  TraceCollapsibleTrigger,
  TraceCollapsibleContent,
} from "@/components/ui/trace"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Brain02Icon,
  Search01Icon,
  CheckmarkCircle02Icon,
  Alert02Icon,
} from "@hugeicons/core-free-icons"

interface ThinkingTraceProps {
  incident: Incident
  className?: string
}

export function ThinkingTrace({ incident, className }: ThinkingTraceProps) {
  const confPct = Math.round((incident.confidence || 0) * 100)
  const threshPct = Math.round((incident.min_confidence_threshold || 0.65) * 100)
  const isConfidenceAdequate = confPct >= threshPct

  return (
    <Card className={cn("border-border bg-card", className)}>
      <CardHeader className="flex flex-row items-center justify-between gap-4 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <HugeiconsIcon
            icon={Brain02Icon}
            className="size-5 text-primary"
            strokeWidth={2}
          />
          <div>
            <CardTitle className="text-base font-semibold text-foreground">
              ReAct Investigator (diagnose_node)
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              Autonomous reasoning &amp; evidence synthesis
            </p>
          </div>
        </div>
        <Badge
          variant={incident.can_auto_remediate ? "default" : "destructive"}
          className="text-xs"
        >
          {incident.can_auto_remediate
            ? "Auto-remediable"
            : "Manual intervention required"}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-6 pt-4">
        {/* RCA Confidence Progress */}
        <div className="space-y-2 rounded-md border border-border bg-muted/20 p-3">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium text-muted-foreground">
              Root Cause Confidence (RCA)
            </span>
            <span className="font-mono tabular-nums text-foreground">
              <strong className={cn(isConfidenceAdequate ? "text-primary" : "text-destructive")}>
                {confPct}%
              </strong>{" "}
              / {threshPct}% threshold
            </span>
          </div>
          <Progress value={Math.min(100, confPct)} className="h-2 bg-muted" />
          <p className="text-[11px] text-muted-foreground">
            {isConfidenceAdequate ? (
              <span className="flex items-center gap-1 text-primary">
                <HugeiconsIcon icon={CheckmarkCircle02Icon} className="size-3.5 inline" />
                Confidence exceeds decision threshold. Safe to proceed with plan.
              </span>
            ) : (
              <span className="flex items-center gap-1 text-destructive">
                <HugeiconsIcon icon={Alert02Icon} className="size-3.5 inline" />
                Confidence below decision threshold. Requires secondary validation.
              </span>
            )}
          </p>
        </div>

        {/* Synthesized Root Cause */}
        <div className="space-y-1.5">
          <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Synthesized Root Cause
          </span>
          <p className="rounded-md border border-border bg-background p-3 text-sm leading-relaxed text-foreground">
            {incident.suspected_cause}
          </p>
        </div>

        {/* Recommended Strategy */}
        {incident.recommended_strategy && (
          <div className="space-y-1.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Recommended Strategy
            </span>
            <p className="rounded-md border border-border bg-muted/30 p-3 text-xs leading-relaxed text-foreground">
              {incident.recommended_strategy}
            </p>
          </div>
        )}

        {/* Tool Evidence using shadcn Trace primitives */}
        {incident.evidence && incident.evidence.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Investigative Tool Evidence ({incident.evidence.length})
              </span>
              <span className="text-[11px] text-muted-foreground">
                Click trace line to expand details
              </span>
            </div>

            <div className="rounded-md border border-border bg-background p-3">
              <Trace>
                {incident.evidence.map((ev, idx) => {
                  const text =
                    typeof ev === "string" ? ev : JSON.stringify(ev, null, 2)
                  const colonIdx = text.indexOf(":")
                  const title =
                    colonIdx !== -1 ? text.slice(0, colonIdx) : `Evidence ${idx + 1}`
                  const body =
                    colonIdx !== -1 ? text.slice(colonIdx + 1).trim() : text

                  return (
                    <TraceCollapsible key={idx}>
                      <TraceCollapsibleTrigger className="w-full justify-between py-1 hover:bg-muted/30 rounded px-1.5 transition-colors">
                        <TraceLine className="truncate">
                          <TraceIcon>
                            <HugeiconsIcon icon={Search01Icon} className="size-3.5 text-muted-foreground" />
                          </TraceIcon>
                          <TraceTitle className="font-mono text-xs text-foreground">
                            {title}
                          </TraceTitle>
                        </TraceLine>
                        <TraceDescription className="text-[10px]">
                          Item {idx + 1}
                        </TraceDescription>
                      </TraceCollapsibleTrigger>
                      <TraceCollapsibleContent>
                        <pre className="overflow-x-auto rounded border border-border bg-muted/40 p-2.5 font-mono text-[11px] leading-relaxed text-foreground whitespace-pre-wrap">
                          {body}
                        </pre>
                      </TraceCollapsibleContent>
                    </TraceCollapsible>
                  )
                })}
              </Trace>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
