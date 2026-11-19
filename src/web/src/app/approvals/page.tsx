"use client"

import * as React from "react"
import Link from "next/link"
import { NavigationHeader } from "@/components/dashboard/navigation-header"
import { TelemetryStrip } from "@/components/dashboard/telemetry-strip"
import { IncidentQueue } from "@/components/dashboard/incident-queue"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  ArrowLeft01Icon,
  Shield02Icon,
} from "@hugeicons/core-free-icons"

export default function ApprovalsPage() {
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
            <span className="font-semibold text-foreground">Approvals</span>
          </nav>

          <Link href="/">
            <Button variant="ghost" size="sm" className="h-7 text-xs">
              <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5 mr-1" />
              Back to All Threads
            </Button>
          </Link>
        </div>

        {/* Telemetry Strip */}
        <TelemetryStrip />

        {/* Section Header */}
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <HugeiconsIcon icon={Shield02Icon} className="size-5 text-chart-2" />
            <h1 className="text-xl font-bold tracking-tight text-foreground">
              Awaiting Human Approval Queue
            </h1>
          </div>
          <p className="text-xs text-muted-foreground">
            Incidents paused at the <code>approval_node</code> requiring human sign-off due to OPA policies or high blast radius
          </p>
        </div>

        {/* Filtered Queue set to Approvals */}
        <IncidentQueue initialFilter="APPROVAL" />
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        <p>NEXUS Autonomous SRE Control Plane · Human-in-the-Loop Queue</p>
      </footer>
    </div>
  )
}
