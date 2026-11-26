import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { EmptyState } from "@/components/empty-state"
import { formatNumber, formatPercent, formatTimestamp } from "@/lib/format"
import type { NexusPrediction } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

const MODE_CLASS: Record<string, string> = {
  autonomous: "border-primary/30 bg-primary/10 text-primary",
  advisory: "border-chart-2/30 bg-chart-2/10 text-chart-2",
  shadow: "border-border bg-muted text-muted-foreground",
}

const OUTCOME_CLASS: Record<string, string> = {
  // The engine reports "correct" / "false_positive"; success/failed are kept
  // for older records.
  correct: "border-primary/30 bg-primary/10 text-primary",
  success: "border-primary/30 bg-primary/10 text-primary",
  false_positive: "border-destructive/30 bg-destructive/10 text-destructive",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
  pending: "border-border bg-muted text-muted-foreground",
}

function pill(value: string, className: string) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
        className
      )}
    >
      {value.toLowerCase()}
    </span>
  )
}

/**
 * Predictive autoscaler decisions, rendered from the fields the API actually returns:
 * `deployment`, `multiplier`, `confidence`, `outcome`, `decided_at` and `description`.
 */
export function PredictionsTable({
  predictions,
  className,
}: {
  predictions: NexusPrediction[]
  className?: string
}) {
  if (predictions.length === 0) {
    return (
      <EmptyState
        title="No scaling decisions recorded"
        description="The predictive autoscaler has not published a decision yet."
        className={className}
      />
    )
  }

  return (
    <div className={cn("overflow-x-auto rounded-lg border border-border", className)}>
      <Table className="w-full min-w-[880px]">
        <TableHeader>
          <TableRow>
            <TableHead className="font-mono text-xs">Deployment</TableHead>
            <TableHead className="font-mono text-xs">Namespace</TableHead>
            <TableHead className="text-right font-mono text-xs">Current RPS</TableHead>
            <TableHead className="text-right font-mono text-xs">Predicted RPS</TableHead>
            <TableHead className="text-right font-mono text-xs">Multiplier</TableHead>
            <TableHead className="text-right font-mono text-xs">Confidence</TableHead>
            <TableHead className="font-mono text-xs">Mode</TableHead>
            <TableHead className="font-mono text-xs">Outcome</TableHead>
            <TableHead className="text-right font-mono text-xs">Decided at</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {predictions.map((prediction, index) => (
            <TableRow key={`${prediction.deployment}-${prediction.decided_at}-${index}`}>
              <TableCell className="max-w-[220px]">
                <span className="block truncate font-mono text-xs font-semibold">
                  {prediction.deployment}
                </span>
                <span className="mt-0.5 block max-w-[220px] truncate text-[11px] text-muted-foreground">
                  {prediction.description}
                </span>
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {prediction.namespace}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                {formatNumber(prediction.current_rps, 1)}
              </TableCell>
              <TableCell className="text-right font-mono text-xs font-semibold tabular-nums">
                {formatNumber(prediction.predicted_rps, 1)}
              </TableCell>
              <TableCell className="text-right font-mono text-xs font-semibold tabular-nums text-primary">
                {formatNumber(prediction.multiplier, 2)}x
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {formatPercent(prediction.confidence)}
              </TableCell>
              <TableCell>
                {pill(
                  prediction.mode,
                  MODE_CLASS[prediction.mode?.toLowerCase()] ??
                    MODE_CLASS.shadow
                )}
              </TableCell>
              <TableCell>
                {pill(
                  prediction.outcome,
                  OUTCOME_CLASS[prediction.outcome?.toLowerCase()] ??
                    OUTCOME_CLASS.pending
                )}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                {formatTimestamp(prediction.decided_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
