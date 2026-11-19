"use client"

import * as React from "react"
import Link from "next/link"
import { useIncidents } from "@/context/incident-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Card, CardHeader, CardDescription, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  PlayIcon,
  RefreshIcon,
  Layers01Icon,
  Shield02Icon,
  CheckmarkCircle02Icon,
  ChartLineData02Icon,
} from "@hugeicons/core-free-icons"

interface TelemetryStripProps {
  className?: string
  showControls?: boolean
}

export function TelemetryStrip({ className, showControls = true }: TelemetryStripProps) {
  const {
    incidents,
    predictions,
    isOnline,
    isDemoMode,
    lastSync,
    toggleDemoMode,
    refreshData,
    isLoading,
  } = useIncidents()

  const total = incidents.length
  const awaiting = incidents.filter((i) => i.approval.is_interrupted).length
  const resolved = incidents.filter((i) => i.status === "RESOLVED").length
  const terminal = incidents.filter((i) =>
    ["RESOLVED", "ESCALATED", "ROLLED_BACK", "EXECUTION_FAILED", "POLICY_DENIED"].includes(
      i.status
    )
  ).length
  const successRateText =
    terminal > 0 ? `(${Math.round((resolved / terminal) * 100)}%)` : ""

  const syncTimeStr = lastSync
    ? lastSync.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : ""

  return (
    <div className={cn("space-y-3", className)}>
      {/* Utility Toolbar: Live Sync & Preview Mode Controls */}
      {showControls && (
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Operational Telemetry &amp; KPIs
            </span>
          </div>

          <div className="flex items-center gap-2.5">
            <div className="flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground">
              <span
                className={cn(
                  "size-2 rounded-full",
                  isDemoMode
                    ? "bg-chart-2"
                    : isOnline
                    ? "bg-primary animate-pulse"
                    : "bg-destructive"
                )}
              />
              <span>
                {isDemoMode
                  ? "Sample Preview Mode"
                  : isOnline
                  ? `Synced ${syncTimeStr}`
                  : "API Unreachable (Offline)"}
              </span>
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={() => toggleDemoMode(!isDemoMode)}
              className="h-7 text-xs font-mono"
            >
              <HugeiconsIcon icon={PlayIcon} className="size-3 mr-1" />
              {isDemoMode ? "Live API Mode" : "Preview Mode"}
            </Button>

            <Button
              variant="ghost"
              size="sm"
              onClick={() => refreshData()}
              disabled={isLoading}
              className="h-7 text-xs font-mono"
              title="Manual Sync"
            >
              <HugeiconsIcon
                icon={RefreshIcon}
                className={cn("size-3.5", isLoading && "animate-spin")}
              />
              <span className="sr-only">Sync</span>
            </Button>
          </div>
        </div>
      )}

      {/* 4 shadcn/ui KPI Metric Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 sm:gap-4">
        {/* Metric 1: Threads */}
        <Link href="/threads" className="group block focus:outline-none">
          <Card
            size="sm"
            className="border border-border bg-card transition-all group-hover:border-primary/40 group-hover:bg-muted/10 h-full"
          >
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1">
              <CardDescription className="text-xs font-medium text-muted-foreground group-hover:text-foreground transition-colors">
                Threads
              </CardDescription>
              <HugeiconsIcon
                icon={Layers01Icon}
                className="size-4 text-muted-foreground group-hover:text-primary transition-colors"
              />
            </CardHeader>
            <CardContent className="space-y-1">
              <div className="text-2xl font-bold font-mono tracking-tight text-foreground">
                {total}
              </div>
              <p className="text-[11px] text-muted-foreground">
                Active LangGraph pipelines
              </p>
            </CardContent>
          </Card>
        </Link>

        {/* Metric 2: Awaiting Approval */}
        <Link href="/approvals" className="group block focus:outline-none">
          <Card
            size="sm"
            className={cn(
              "border border-border bg-card transition-all group-hover:border-primary/40 group-hover:bg-muted/10 h-full",
              awaiting > 0 && "border-chart-2/40 bg-chart-2/5 group-hover:border-chart-2/70"
            )}
          >
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1">
              <CardDescription
                className={cn(
                  "text-xs font-medium transition-colors",
                  awaiting > 0
                    ? "text-chart-2 font-semibold"
                    : "text-muted-foreground group-hover:text-foreground"
                )}
              >
                Awaiting Approval
              </CardDescription>
              <HugeiconsIcon
                icon={Shield02Icon}
                className={cn(
                  "size-4 transition-colors",
                  awaiting > 0 ? "text-chart-2" : "text-muted-foreground group-hover:text-primary"
                )}
              />
            </CardHeader>
            <CardContent className="space-y-1">
              <div
                className={cn(
                  "text-2xl font-bold font-mono tracking-tight",
                  awaiting > 0 ? "text-chart-2 font-extrabold" : "text-foreground"
                )}
              >
                {awaiting}
              </div>
              <p className="text-[11px] text-muted-foreground">
                {awaiting > 0 ? "Requires operator sign-off" : "Zero pending approvals"}
              </p>
            </CardContent>
          </Card>
        </Link>

        {/* Metric 3: Self-Healed */}
        <Link href="/threads" className="group block focus:outline-none">
          <Card
            size="sm"
            className="border border-border bg-card transition-all group-hover:border-primary/40 group-hover:bg-muted/10 h-full"
          >
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1">
              <CardDescription className="text-xs font-medium text-muted-foreground group-hover:text-foreground transition-colors">
                Self-Healed
              </CardDescription>
              <HugeiconsIcon
                icon={CheckmarkCircle02Icon}
                className="size-4 text-primary"
              />
            </CardHeader>
            <CardContent className="space-y-1">
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-bold font-mono tracking-tight text-primary">
                  {resolved}
                </span>
                {successRateText && (
                  <Badge
                    variant="outline"
                    className="border-primary/30 bg-primary/10 font-mono text-[10px] text-primary"
                  >
                    {successRateText}
                  </Badge>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground">
                Closed-loop verified
              </p>
            </CardContent>
          </Card>
        </Link>

        {/* Metric 4: Predictive Scaling */}
        <Link href="/predictions" className="group block focus:outline-none">
          <Card
            size="sm"
            className="border border-border bg-card transition-all group-hover:border-primary/40 group-hover:bg-muted/10 h-full"
          >
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1">
              <CardDescription className="text-xs font-medium text-muted-foreground group-hover:text-foreground transition-colors">
                Predictive Scaling
              </CardDescription>
              <HugeiconsIcon
                icon={ChartLineData02Icon}
                className="size-4 text-muted-foreground group-hover:text-primary transition-colors"
              />
            </CardHeader>
            <CardContent className="space-y-1">
              <div className="text-2xl font-bold font-mono tracking-tight text-foreground">
                {predictions.length}
              </div>
              <p className="text-[11px] text-muted-foreground">
                Proactive pod adjustments
              </p>
            </CardContent>
          </Card>
        </Link>
      </div>
    </div>
  )
}
