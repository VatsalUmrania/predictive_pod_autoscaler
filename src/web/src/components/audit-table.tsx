import Link from "next/link"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { EmptyState } from "@/components/empty-state"
import { formatTimestamp } from "@/lib/format"
import type { NexusAuditRecord } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

const OUTCOME_CLASS: Record<string, string> = {
  success: "border-primary/30 bg-primary/10 text-primary",
  // Approve/reject decisions are recorded in the audit trail with these outcomes.
  approved: "border-primary/30 bg-primary/10 text-primary",
  failed: "border-destructive/30 bg-destructive/10 text-destructive",
  rejected: "border-destructive/30 bg-destructive/10 text-destructive",
  rolled_back: "border-destructive/30 bg-destructive/10 text-destructive",
  pending: "border-border bg-muted text-muted-foreground",
}

function outcomePill(outcome: string) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded border px-2 py-0.5 font-mono text-[11px] font-semibold",
        OUTCOME_CLASS[outcome.toLowerCase()] ?? OUTCOME_CLASS.pending
      )}
    >
      {outcome.toLowerCase().replace(/_/g, " ")}
    </span>
  )
}

/**
 * Governance audit trail.
 *
 * Every row is one action the NEXUS engine decided to take, or was asked to take by
 * an operator.
 */
export function AuditTable({
  records,
  className,
}: {
  records: NexusAuditRecord[]
  className?: string
}) {
  if (records.length === 0) {
    return (
      <EmptyState
        title="No audit records"
        description="The governance audit trail is empty. Records appear here once the engine acts on an incident."
        className={className}
      />
    )
  }

  return (
    <div className={cn("overflow-x-auto rounded-lg border border-border", className)}>
      <Table className="w-full min-w-[980px]">
        <TableHeader>
          <TableRow>
            <TableHead className="font-mono text-xs">Timestamp</TableHead>
            <TableHead className="font-mono text-xs">Action</TableHead>
            <TableHead className="font-mono text-xs">Incident</TableHead>
            <TableHead className="font-mono text-xs">Runbook</TableHead>
            <TableHead className="font-mono text-xs">Triggered by</TableHead>
            <TableHead className="text-right font-mono text-xs">Level</TableHead>
            <TableHead className="font-mono text-xs">Outcome</TableHead>
            <TableHead className="font-mono text-xs">Rollback</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {records.map((record, index) => (
            <TableRow key={`${record.action_id}-${index}`}>
              <TableCell className="whitespace-nowrap font-mono text-xs tabular-nums text-muted-foreground">
                {formatTimestamp(record.timestamp)}
              </TableCell>
              <TableCell className="max-w-[220px]">
                <span className="block truncate font-mono text-xs">
                  {record.action_id}
                </span>
                {record.target ? (
                  <span className="mt-0.5 block max-w-[220px] truncate text-[11px] text-muted-foreground">
                    {record.target}
                  </span>
                ) : null}
              </TableCell>
              <TableCell className="font-mono text-xs">
                {record.incident_id ? (
                  <Link
                    href={`/incidents/${encodeURIComponent(record.incident_id)}`}
                    className="underline-offset-4 hover:text-primary hover:underline"
                  >
                    {record.incident_id}
                  </Link>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </TableCell>
              <TableCell className="max-w-[200px]">
                <span className="block truncate font-mono text-xs text-muted-foreground">
                  {record.runbook_id || "-"}
                </span>
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {record.triggered_by || "-"}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {record.healing_level}
              </TableCell>
              <TableCell>{outcomePill(record.execution_outcome)}</TableCell>
              <TableCell className="font-mono text-xs">
                {record.rollback_triggered ? (
                  <span className="text-destructive">triggered</span>
                ) : (
                  <span className="text-muted-foreground">no</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
