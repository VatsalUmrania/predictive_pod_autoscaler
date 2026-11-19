"use client"

import * as React from "react"
import Link from "next/link"
import { NavigationHeader } from "@/components/dashboard/navigation-header"
import { TelemetryStrip } from "@/components/dashboard/telemetry-strip"
import { PredictionsTable } from "@/components/dashboard/predictions-table"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowLeft01Icon,
  ChartLineData02Icon,
} from "@hugeicons/core-free-icons"

export default function PredictionsPage() {
  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <NavigationHeader />

      <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-6">
        {/* Breadcrumb */}
        <div className="flex items-center justify-between">
          <nav className="flex items-center gap-2 text-xs text-muted-foreground" aria-label="Breadcrumb">
            <Link href="/" className="hover:text-foreground transition-colors">
              Control Plane
            </Link>
            <span>/</span>
            <span className="font-semibold text-foreground">Predictive Scaling</span>
          </nav>

          <Link href="/">
            <Button variant="ghost" size="sm" className="h-7 text-xs">
              <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5 mr-1" />
              Back to Threads
            </Button>
          </Link>
        </div>

        {/* Telemetry Strip */}
        <TelemetryStrip />

        {/* Section Header */}
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <HugeiconsIcon icon={ChartLineData02Icon} className="size-5 text-primary" />
            <h1 className="text-xl font-bold tracking-tight text-foreground">
              Predictive Pod Autoscaler (PPA) Decisions
            </h1>
          </div>
          <p className="text-xs text-muted-foreground">
            Machine learning load forecasting and proactive Kubernetes horizontal scaling decisions
          </p>
        </div>

        {/* Full Predictions Table */}
        <PredictionsTable />

        {/* Informational Guidance Cards */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3 pt-4 border-t border-border text-xs">
          <div className="rounded-lg border border-border bg-card p-4 space-y-1.5">
            <span className="font-mono font-semibold text-primary">Autonomous Mode</span>
            <p className="text-muted-foreground leading-relaxed">
              Target replicas are mutated proactively without human gatekeeper intervention when model confidence is high.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-card p-4 space-y-1.5">
            <span className="font-mono font-semibold text-chart-2">Advisory Mode</span>
            <p className="text-muted-foreground leading-relaxed">
              Recommendations are published to the telemetry stream and Slack/alerts without modifying cluster state.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-card p-4 space-y-1.5">
            <span className="font-mono font-semibold text-muted-foreground">Shadow Mode</span>
            <p className="text-muted-foreground leading-relaxed">
              Decisions are logged and scored against historical ground truth to calibrate scaling thresholds.
            </p>
          </div>
        </div>
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        <p>NEXUS Autonomous SRE Control Plane · Predictive Pod Autoscaler</p>
      </footer>
    </div>
  )
}
