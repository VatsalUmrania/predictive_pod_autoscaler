import * as React from "react"
import { Suspense } from "react"

import { ApiError } from "@/components/api-error"
import { PageHeader } from "@/components/page-header"
import { PendingApprovals } from "@/components/pending-approvals"
import { ListSkeleton } from "@/components/skeletons"
import { getIncidents, getPendingApprovals } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Approvals"
        description="Incidents the NEXUS engine has paused at the approval node because policy, blast radius, or confidence requires an operator decision."
      />
      <Suspense fallback={<ListSkeleton rows={3} />}>
        <ApprovalQueue />
      </Suspense>
    </div>
  )
}

async function ApprovalQueue() {
  const [approvals, incidents] = await Promise.allSettled([
    getPendingApprovals(),
    getIncidents(200),
  ])

  if (approvals.status === "rejected") {
    return (
      <ApiError
        error={describeApiError(approvals.reason)}
        title="Approval queue unavailable"
      />
    )
  }
  if (incidents.status === "rejected") {
    return (
      <ApiError
        error={describeApiError(incidents.reason)}
        title="Incident feed unavailable"
      />
    )
  }

  const pending = approvals.value.filter((entry) => entry.incident_id)

  return (
    <div className="space-y-3">
      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {pending.length} pending approval{pending.length === 1 ? "" : "s"}
      </p>
      <PendingApprovals approvals={approvals.value} incidents={incidents.value} />
    </div>
  )
}
