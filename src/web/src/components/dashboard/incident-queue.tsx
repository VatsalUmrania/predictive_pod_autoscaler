"use client"

import * as React from "react"
import Link from "next/link"
import { useIncidents } from "@/context/incident-context"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Search01Icon,
  ArrowRight01Icon,
  Shield02Icon,
  Alert02Icon,
  CheckmarkCircle02Icon,
  Clock01Icon,
} from "@hugeicons/core-free-icons"

type FilterType = "ALL" | "APPROVAL" | "ACTIVE" | "RESOLVED" | "ESCALATED"

interface IncidentQueueProps {
  className?: string
  initialFilter?: FilterType
  limit?: number
  showFilters?: boolean
}

export function IncidentQueue({
  className,
  initialFilter = "ALL",
  limit,
  showFilters = true,
}: IncidentQueueProps) {
  const { incidents, isOnline, isDemoMode, refreshData } = useIncidents()
  const [filter, setFilter] = React.useState<FilterType>(initialFilter)
  const [searchQuery, setSearchQuery] = React.useState("")

  const filteredIncidents = React.useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    return incidents.filter((inc) => {
      if (filter === "APPROVAL" && !inc.approval.is_interrupted) return false
      if (filter === "RESOLVED" && inc.status !== "RESOLVED") return false
      if (
        filter === "ESCALATED" &&
        !["ESCALATED", "ROLLED_BACK", "EXECUTION_FAILED", "POLICY_DENIED"].includes(
          inc.status
        )
      )
        return false
      if (
        filter === "ACTIVE" &&
        ["RESOLVED", "ESCALATED", "ROLLED_BACK", "POLICY_DENIED"].includes(
          inc.status
        )
      )
        return false

      if (q) {
        const hay = `${inc.incident_id} ${inc.target_resource} ${inc.failure_class} ${inc.suspected_cause} ${inc.service_name}`.toLowerCase()
        return hay.includes(q)
      }
      return true
    })
  }, [incidents, filter, searchQuery])

  const displayedIncidents = React.useMemo(() => {
    return limit ? filteredIncidents.slice(0, limit) : filteredIncidents
  }, [filteredIncidents, limit])

  const getStatusBadge = (status: string, isInterrupted: boolean) => {
    const s = String(status || "").toUpperCase()
    if (isInterrupted) {
      return (
        <span className="flex items-center gap-1 rounded border border-chart-2/40 bg-chart-2/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-chart-2">
          <HugeiconsIcon icon={Shield02Icon} className="size-3" />
          Awaiting Approval
        </span>
      )
    }
    if (s === "RESOLVED" || s === "AUTO_APPROVED" || s === "APPROVED") {
      return (
        <span className="flex items-center gap-1 rounded border border-primary/30 bg-primary/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-primary">
          <HugeiconsIcon icon={CheckmarkCircle02Icon} className="size-3" />
          Resolved
        </span>
      )
    }
    if (["ESCALATED", "ROLLED_BACK", "EXECUTION_FAILED", "POLICY_DENIED"].includes(s)) {
      return (
        <span className="flex items-center gap-1 rounded border border-destructive/30 bg-destructive/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-destructive">
          <HugeiconsIcon icon={Alert02Icon} className="size-3" />
          {s.replace(/_/g, " ")}
        </span>
      )
    }
    return (
      <span className="flex items-center gap-1 rounded border border-chart-2/40 bg-chart-2/10 px-2 py-0.5 font-mono text-[11px] font-medium text-chart-2">
        <HugeiconsIcon icon={Clock01Icon} className="size-3" />
        {s.replace(/_/g, " ")}
      </span>
    )
  }

  return (
    <div className={cn("space-y-4", className)}>
      {/* Controls & Search */}
      {showFilters && (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          {/* Filter Tabs */}
          <div className="flex flex-wrap items-center gap-1 rounded-md border border-border bg-card p-1 text-xs">
            {(
              [
                { id: "ALL", label: "All Threads" },
                { id: "APPROVAL", label: "Approvals" },
                { id: "ACTIVE", label: "Active" },
                { id: "RESOLVED", label: "Resolved" },
                { id: "ESCALATED", label: "Escalated" },
              ] as const
            ).map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setFilter(t.id)}
                className={cn(
                  "rounded px-3 py-1 font-medium transition-colors",
                  filter === t.id
                    ? "bg-muted font-semibold text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                {t.label}
                {t.id === "APPROVAL" && (
                  <span className="ml-1.5 rounded-full border border-chart-2/30 bg-chart-2/10 px-1.5 py-0.2 font-mono text-[10px] text-chart-2">
                    {incidents.filter((i) => i.approval.is_interrupted).length}
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Search Input */}
          <div className="relative w-full sm:w-64">
            <HugeiconsIcon
              icon={Search01Icon}
              className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
            />
            <Input
              type="search"
              placeholder="Search threads, resources..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8 pl-8 text-xs font-mono"
            />
          </div>
        </div>
      )}

      {/* Incident List */}
      <div className="space-y-2">
        {displayedIncidents.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-card/50 p-8 text-center">
            <p className="text-sm text-muted-foreground">
              {!isOnline && !isDemoMode
                ? "Could not reach /developer/incidents. Verify NEXUS is running or toggle preview mode."
                : "No incident threads match the current filter or search criteria."}
            </p>
            <div className="mt-3 flex items-center justify-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setFilter("ALL")
                  setSearchQuery("")
                  refreshData()
                }}
                className="text-xs"
              >
                Reset Filters &amp; Sync
              </Button>
            </div>
          </div>
        ) : (
          displayedIncidents.map((inc) => {
            const confPct = Math.round((inc.confidence || 0) * 100)
            return (
              <Link
                key={inc.incident_id}
                href={`/incidents/${encodeURIComponent(inc.incident_id)}`}
                className={cn(
                  "group relative block rounded-lg border border-border bg-card p-4 transition-all hover:border-primary/40 hover:bg-muted/10",
                  inc.approval.is_interrupted &&
                    "border-chart-2/40 bg-chart-2/10 hover:border-chart-2/60"
                )}
              >
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-foreground group-hover:text-primary transition-colors">
                      {inc.incident_id}
                    </span>
                    <span className="text-muted-foreground">·</span>
                    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                      {inc.failure_class}
                    </code>
                    {inc.platform && (
                      <span className="text-xs text-muted-foreground">
                        ({inc.platform})
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    {getStatusBadge(inc.status, inc.approval.is_interrupted)}
                    <span className="font-mono text-xs text-muted-foreground">
                      {confPct}% conf
                    </span>
                  </div>
                </div>

                {/* Target resource & cause */}
                <div className="mt-2 text-xs">
                  <div className="flex items-center gap-1 font-medium text-foreground">
                    {inc.involved_object && (
                      <span className="text-muted-foreground font-mono">
                        {inc.involved_object} &rarr;{" "}
                      </span>
                    )}
                    <span className="font-mono text-primary font-semibold">
                      {inc.target_resource}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-muted-foreground leading-relaxed">
                    {inc.suspected_cause}
                  </p>
                </div>

                {/* Card footer */}
                <div className="mt-3 flex items-center justify-between border-t border-border/60 pt-2 text-[11px] text-muted-foreground">
                  <span className="font-mono">
                    Node: <strong>{inc.active_node}</strong> · Attempts:{" "}
                    {inc.execution.remediation_attempts}/
                    {inc.execution.max_remediation_attempts}
                  </span>
                  <span className="flex items-center gap-1 font-medium text-foreground group-hover:text-primary transition-colors">
                    Inspect Thread
                    <HugeiconsIcon
                      icon={ArrowRight01Icon}
                      className="size-3.5 transition-transform group-hover:translate-x-0.5"
                    />
                  </span>
                </div>
              </Link>
            )
          })
        )}
      </div>
    </div>
  )
}
