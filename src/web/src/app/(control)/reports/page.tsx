import * as React from "react"
import Link from "next/link"
import { Suspense } from "react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/components/api-error"
import { EmptyState } from "@/components/empty-state"
import { PageHeader } from "@/components/page-header"
import { StatusBadge } from "@/components/dashboard/status-badge"
import { TableSkeleton } from "@/components/skeletons"
import { getIncidents } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"
import { formatTimestamp } from "@/lib/format"

export default function ReportsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Post-incident reports"
        description="Incidents the NEXUS engine has finished with, each with a generated post-incident report from the outcome of the remediation."
      />
      <Suspense fallback={<TableSkeleton rows={6} columns={5} />}>
        <ReportList />
      </Suspense>
    </div>
  )
}

async function ReportList() {
  let incidents
  try {
    incidents = await getIncidents(200)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Incident feed unavailable" />
  }

  const withReports = incidents.filter((i) => i.post_incident_report_ready)

  if (withReports.length === 0) {
    return (
      <EmptyState
        title="No post-incident reports yet"
        description="The NEXUS engine writes a report once an incident reaches a terminal state with at least one executed remediation step."
      />
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <Table className="w-full min-w-[820px]">
        <TableHeader>
          <TableRow>
            <TableHead className="font-mono text-xs">Incident</TableHead>
            <TableHead className="font-mono text-xs">Target</TableHead>
            <TableHead className="font-mono text-xs">Status</TableHead>
            <TableHead className="font-mono text-xs">Outcome</TableHead>
            <TableHead className="text-right font-mono text-xs">Updated</TableHead>
            <TableHead className="text-right font-mono text-xs">Report</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {withReports.map((incident) => (
            <TableRow key={incident.incident_id} className="hover:bg-muted/40">
              <TableCell className="font-mono text-xs font-semibold">
                <Link
                  href={`/incidents/${encodeURIComponent(incident.incident_id)}`}
                  className="underline-offset-4 hover:text-primary hover:underline"
                >
                  {incident.incident_id}
                </Link>
              </TableCell>
              <TableCell className="max-w-[280px]">
                <span className="block truncate font-mono text-xs">
                  {incident.target_resource}
                </span>
              </TableCell>
              <TableCell>
                <StatusBadge
                  status={incident.status}
                  isInterrupted={incident.is_interrupted}
                />
              </TableCell>
              <TableCell className="font-mono text-xs text-muted-foreground">
                {incident.outcome}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                {formatTimestamp(incident.updated_at)}
              </TableCell>
              <TableCell className="text-right">
                <Link
                  href={`/incidents/${encodeURIComponent(incident.incident_id)}`}
                  className="font-mono text-[11px] text-primary underline-offset-4 hover:underline"
                >
                  View report
                </Link>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
