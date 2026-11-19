"use client"

import * as React from "react"
import Link from "next/link"
import { NavigationHeader } from "@/components/dashboard/navigation-header"
import { TelemetryStrip } from "@/components/dashboard/telemetry-strip"
import { IncidentQueue } from "@/components/dashboard/incident-queue"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Layers01Icon,
  ArrowLeft01Icon,
} from "@hugeicons/core-free-icons"

export default function ThreadsPage() {
  return (
    <div className="min-h-screen flex flex-col bg-background text-foreground">
      <NavigationHeader />

      <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-6">
        {/* Navigation Breadcrumb & Back */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Link href="/">
              <Button variant="ghost" size="sm" className="h-7 text-xs text-muted-foreground hover:text-foreground">
                <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5 mr-1" />
                Control Panel
              </Button>
            </Link>
            <span className="text-muted-foreground">/</span>
            <span className="text-xs font-semibold text-foreground">Threads Feed</span>
          </div>
        </div>

        {/* Telemetry Strip */}
        <TelemetryStrip />

        {/* Section Header */}
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <HugeiconsIcon icon={Layers01Icon} className="size-5 text-primary" />
            <h1 className="text-xl font-bold tracking-tight text-foreground">
              LangGraph Incident Feed
            </h1>
          </div>
          <p className="text-xs text-muted-foreground">
            Complete autonomous remediation thread history, active ReAct diagnostic loops, and closed-loop verification states
          </p>
        </div>

        {/* Full Filterable Incident Queue */}
        <IncidentQueue showFilters={true} />
      </main>

      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        <p>NEXUS Autonomous SRE Control Plane · Incident Threads Feed</p>
      </footer>
    </div>
  )
}
