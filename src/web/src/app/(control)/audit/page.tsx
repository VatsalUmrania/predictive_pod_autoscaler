import * as React from "react"
import { Suspense } from "react"

import { ApiError } from "@/components/api-error"
import { AuditTable } from "@/components/audit-table"
import { PageHeader } from "@/components/page-header"
import { TableSkeleton } from "@/components/skeletons"
import { getAuditTail } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"

export default function AuditPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit trail"
        description="Every action the NEXUS engine decided to take, or was asked to take by an operator, with the pre-check, execution and rollback outcomes."
      />
      <Suspense fallback={<TableSkeleton rows={10} columns={6} />}>
        <AuditFeed />
      </Suspense>
    </div>
  )
}

async function AuditFeed() {
  let records
  try {
    records = await getAuditTail(100)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Audit trail unavailable" />
  }

  return (
    <div className="space-y-3">
      <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
        {records.length} audit record{records.length === 1 ? "" : "s"}
      </p>
      <AuditTable records={records} />
    </div>
  )
}
