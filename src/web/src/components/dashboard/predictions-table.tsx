"use client"

import * as React from "react"
import { useIncidents } from "@/context/incident-context"
import { cn } from "@/lib/utils"
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table"
import { HugeiconsIcon } from "@hugeicons/react"
import { ChartLineData02Icon } from "@hugeicons/core-free-icons"

interface PredictionsTableProps {
  className?: string
  limit?: number
  compact?: boolean
}

export function PredictionsTable({ className, limit, compact }: PredictionsTableProps) {
  const { predictions } = useIncidents()
  const isCompact = compact ?? Boolean(limit)

  const displayedPredictions = limit
    ? predictions.slice(0, limit)
    : predictions

  const getModeBadge = (mode: string) => {
    const m = (mode || "advisory").toLowerCase()
    if (m === "autonomous") {
      return (
        <span className="rounded border border-primary/30 bg-primary/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-primary">
          autonomous
        </span>
      )
    }
    if (m === "advisory") {
      return (
        <span className="rounded border border-chart-2/30 bg-chart-2/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-chart-2">
          advisory
        </span>
      )
    }
    return (
      <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] font-medium text-muted-foreground">
        shadow
      </span>
    )
  }

  if (predictions.length === 0) {
    return (
      <div
        className={cn(
          "rounded-lg border border-dashed border-border bg-card p-8 text-center text-xs text-muted-foreground",
          className
        )}
      >
        No predictive scaling decisions recorded yet.
      </div>
    )
  }

  return (
    <div
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card",
        className
      )}
    >
      <div className="flex items-center justify-between border-b border-border bg-muted/20 px-4 py-3">
        <div className="flex items-center gap-2">
          <HugeiconsIcon
            icon={ChartLineData02Icon}
            className="size-4 text-primary"
          />
          <h3 className="font-semibold text-sm text-foreground">
            Predictive Pod Autoscaler Decisions
          </h3>
        </div>
        <span className="font-mono text-xs text-muted-foreground">
          {predictions.length} decision{predictions.length === 1 ? "" : "s"} recorded
        </span>
      </div>

      {isCompact ? (
        /* Compact 4-column layout engineered to fit dashboard splits without overflow */
        <Table className="w-full">
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              <TableHead className="w-[34%] font-mono text-xs font-semibold text-foreground">
                Workload
              </TableHead>
              <TableHead className="w-[26%] text-right font-mono text-xs font-semibold text-foreground">
                Traffic (RPS)
              </TableHead>
              <TableHead className="w-[20%] text-right font-mono text-xs font-semibold text-foreground">
                Scale
              </TableHead>
              <TableHead className="w-[20%] text-right font-mono text-xs font-semibold text-foreground pr-4">
                Mode
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {displayedPredictions.map((p, idx) => {
              const scaleStr = p.scale_factor
                ? `${Number(p.scale_factor).toFixed(2)}x`
                : "—"
              return (
                <TableRow key={idx} className="border-border">
                  {/* Workload: Target + Namespace */}
                  <TableCell className="py-2.5">
                    <div className="font-mono font-bold text-xs text-foreground truncate max-w-[140px]" title={p.target}>
                      {p.target}
                    </div>
                    <div className="font-mono text-[11px] text-muted-foreground truncate max-w-[140px]" title={p.namespace}>
                      {p.namespace}
                    </div>
                  </TableCell>

                  {/* Traffic: Current -> Predicted */}
                  <TableCell className="text-right py-2.5 font-mono tabular-nums">
                    <div className="flex items-center justify-end gap-1 text-xs">
                      <span className="text-muted-foreground">{p.current_rps ?? "—"}</span>
                      <span className="text-muted-foreground/60 text-[10px]">→</span>
                      <span className="font-bold text-foreground">{p.predicted_rps ?? "—"}</span>
                    </div>
                    <div className="text-[10px] text-muted-foreground">req/sec</div>
                  </TableCell>

                  {/* Scale Factor: Factor + Desired Replicas */}
                  <TableCell className="text-right py-2.5 font-mono tabular-nums">
                    <div className="font-bold text-xs text-primary">
                      {scaleStr}
                    </div>
                    <div className="text-[10px] text-muted-foreground">
                      {p.desired_replicas ? `${p.desired_replicas} pods` : "—"}
                    </div>
                  </TableCell>

                  {/* Autonomy Mode Badge */}
                  <TableCell className="text-right py-2.5 pr-4">
                    <div className="flex justify-end">
                      {getModeBadge(p.mode)}
                    </div>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      ) : (
        /* Full multi-column view for dedicated pages */
        <Table className="w-full">
          <TableHeader>
            <TableRow className="border-border hover:bg-transparent">
              <TableHead className="font-mono text-xs font-semibold text-foreground">
                Workload Target
              </TableHead>
              <TableHead className="font-mono text-xs font-semibold text-foreground">
                Namespace
              </TableHead>
              <TableHead className="text-right font-mono text-xs font-semibold text-foreground">
                Current RPS
              </TableHead>
              <TableHead className="text-right font-mono text-xs font-semibold text-foreground">
                Predicted RPS
              </TableHead>
              <TableHead className="text-right font-mono text-xs font-semibold text-foreground">
                Scale Factor
              </TableHead>
              <TableHead className="text-right font-mono text-xs font-semibold text-foreground">
                Desired Replicas
              </TableHead>
              <TableHead className="text-right font-mono text-xs font-semibold text-foreground pr-4">
                Autonomy Mode
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {displayedPredictions.map((p, idx) => {
              const scaleStr = p.scale_factor
                ? `${Number(p.scale_factor).toFixed(2)}x`
                : "—"
              return (
                <TableRow key={idx} className="border-border">
                  <TableCell className="font-mono font-bold text-foreground">
                    {p.target}
                  </TableCell>
                  <TableCell className="font-mono text-muted-foreground">
                    {p.namespace}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-foreground">
                    {p.current_rps ?? "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums font-semibold text-foreground">
                    {p.predicted_rps ?? "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums text-primary font-semibold">
                    {scaleStr}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums font-bold text-foreground">
                    {p.desired_replicas ?? "—"}
                  </TableCell>
                  <TableCell className="text-right pr-4">
                    <div className="flex justify-end">
                      {getModeBadge(p.mode)}
                    </div>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
