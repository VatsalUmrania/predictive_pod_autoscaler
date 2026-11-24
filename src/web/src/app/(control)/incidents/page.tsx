import * as React from "react"
import { Suspense } from "react"

import { ApiError } from "@/components/api-error"
import { IncidentTable } from "@/components/incident-table"
import { PageHeader } from "@/components/page-header"
import { TableSkeleton } from "@/components/skeletons"
import { getIncidents } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"

export default function IncidentsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Incidents"
        description="Every incident the NEXUS engine has observed, with the graph node it is parked on and the confidence behind its root cause analysis."
      />
      <Suspense fallback={<TableSkeleton rows={8} columns={6} />}>
        <IncidentFeed />
      </Suspense>
    </div>
  )
}

async function IncidentFeed() {
  let incidents
  try {
    incidents = await getIncidents(200)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Incident feed unavailable" />
  }

  return (
    <div className="space-y-3">
      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {incidents.length} incident{incidents.length === 1 ? "" : "s"} in the feed
      </p>
      <IncidentTable incidents={incidents} />
    </div>
  )
}
