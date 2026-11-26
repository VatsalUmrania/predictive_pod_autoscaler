"use client"

import * as React from "react"
import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import { Search01Icon } from "@hugeicons/core-free-icons"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Input } from "@/components/ui/input"
import { StatusBadge, SeverityBadge } from "@/components/dashboard/status-badge"
import { EmptyState } from "@/components/empty-state"
import { formatPercent, formatTimestamp } from "@/lib/format"
import type { NexusIncident } from "@/lib/nexus/types"
import { FAILURE_STATUSES, IGNORED_STATUSES } from "@/lib/nexus/types"
import { cn } from "@/lib/utils"

type FilterKey = "ALL" | "ACTIVE" | "APPROVAL" | "RESOLVED" | "FAILED"

const FILTERS: Array<{ id: FilterKey; label: string }> = [
  { id: "ALL", label: "All" },
  { id: "ACTIVE", label: "In flight" },
  { id: "APPROVAL", label: "Awaiting approval" },
  { id: "RESOLVED", label: "Resolved" },
  { id: "FAILED", label: "Failed" },
]

function matchesFilter(incident: NexusIncident, filter: FilterKey): boolean {
  switch (filter) {
    case "APPROVAL":
      return incident.is_interrupted
    case "RESOLVED":
      return incident.status === "RESOLVED"
    case "FAILED":
      return FAILURE_STATUSES.includes(incident.status)
    case "ACTIVE":
      return (
        !incident.is_interrupted &&
        incident.status !== "RESOLVED" &&
        !FAILURE_STATUSES.includes(incident.status) &&
        !IGNORED_STATUSES.includes(incident.status)
      )
    default:
      return true
  }
}

export function IncidentTable({
  incidents,
  basePath = "/incidents",
  className,
}: {
  incidents: NexusIncident[]
  basePath?: string
  className?: string
}) {
  const [filter, setFilter] = React.useState<FilterKey>("ALL")
  const [query, setQuery] = React.useState("")

  const counts = React.useMemo(() => {
    const map: Record<FilterKey, number> = {
      ALL: incidents.length,
      ACTIVE: 0,
      APPROVAL: 0,
      RESOLVED: 0,
      FAILED: 0,
    }
    for (const incident of incidents) {
      for (const key of ["ACTIVE", "APPROVAL", "RESOLVED", "FAILED"] as const) {
        if (matchesFilter(incident, key)) map[key] += 1
      }
    }
    return map
  }, [incidents])

  const visible = React.useMemo(() => {
    const needle = query.trim().toLowerCase()
    return incidents.filter((incident) => {
      if (!matchesFilter(incident, filter)) return false
      if (!needle) return true
      const haystack = [
        incident.incident_id,
        incident.target_resource,
        incident.failure_class,
        incident.suspected_cause,
        incident.service_name ?? "",
        incident.namespace ?? "",
        incident.active_node,
      ]
        .join(" ")
        .toLowerCase()
      return haystack.includes(needle)
    })
  }, [incidents, filter, query])

  return (
    <div className={cn("space-y-3", className)}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div
          role="group"
          aria-label="Filter incidents"
          className="flex flex-wrap items-center gap-1 rounded-lg border border-border bg-card p-1"
        >
          {FILTERS.map((item) => {
            const active = filter === item.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setFilter(item.id)}
                aria-pressed={active}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  active
                    ? "bg-muted font-semibold text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                {item.label}
                <span className="ml-1.5 font-mono text-[10px] tabular-nums opacity-70">
                  {counts[item.id]}
                </span>
              </button>
            )
          })}
        </div>

        <div className="relative w-full sm:w-64">
          <HugeiconsIcon
            icon={Search01Icon}
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="search"
            aria-label="Search incidents"
            placeholder="Search id, target, failure class"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="h-8 pl-8 font-mono text-xs"
          />
        </div>
      </div>

      {visible.length === 0 ? (
        <EmptyState
          title="No incidents match this view"
          description={
            incidents.length === 0
              ? "The NEXUS engine has not reported any incidents yet."
              : "No incident matches the current filter and search combination."
          }
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <Table className="w-full min-w-[880px]">
            <TableHeader>
              <TableRow>
                <TableHead className="font-mono text-xs">Incident</TableHead>
                <TableHead className="font-mono text-xs">Severity</TableHead>
                <TableHead className="font-mono text-xs">Target</TableHead>
                <TableHead className="font-mono text-xs">Failure class</TableHead>
                <TableHead className="font-mono text-xs">Status</TableHead>
                <TableHead className="font-mono text-xs">Node</TableHead>
                <TableHead className="text-right font-mono text-xs">
                  Confidence
                </TableHead>
                <TableHead className="text-right font-mono text-xs">Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((incident) => (
                <TableRow
                  key={incident.incident_id}
                  className="hover:bg-muted/40"
                >
                  <TableCell className="font-mono text-xs font-semibold">
                    <Link
                      href={`${basePath}/${encodeURIComponent(incident.incident_id)}`}
                      className="text-foreground underline-offset-4 hover:text-primary hover:underline"
                    >
                      {incident.incident_id}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <SeverityBadge severity={incident.severity} />
                  </TableCell>
                  <TableCell className="max-w-[280px]">
                    <span
                      className="block truncate font-mono text-xs"
                      title={incident.target_resource}
                    >
                      {incident.target_resource}
                    </span>
                  </TableCell>
                  <TableCell>
                    <span className="block max-w-[180px] truncate font-mono text-xs text-muted-foreground">
                      {incident.failure_class}
                    </span>
                  </TableCell>
                  <TableCell>
                    <StatusBadge
                      status={incident.status}
                      isInterrupted={incident.is_interrupted}
                    />
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {incident.active_node}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs tabular-nums">
                    {formatPercent(incident.confidence)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs tabular-nums text-muted-foreground">
                    {formatTimestamp(incident.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
