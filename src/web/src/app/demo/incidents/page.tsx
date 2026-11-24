import * as React from "react"

import { IncidentTable } from "@/components/incident-table"
import { PageHeader } from "@/components/page-header"
import { DEMO_INCIDENTS } from "@/lib/demo-data"

export default function DemoIncidentsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Sample incidents"
        description="A static set of incidents rendered through the same components the live feed uses."
      />
      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {DEMO_INCIDENTS.length} sample incident
        {DEMO_INCIDENTS.length === 1 ? "" : "s"}
      </p>
      <IncidentTable incidents={DEMO_INCIDENTS} basePath="/demo/incidents" />
    </div>
  )
}
