import * as React from "react"
import Link from "next/link"
import { Shield02Icon } from "@hugeicons/core-free-icons"

import { EmptyState } from "@/components/empty-state"
import { PageHeader } from "@/components/page-header"
import { DEMO_PENDING_APPROVALS } from "@/lib/demo-data"
import { formatPercent, formatTimestamp } from "@/lib/format"

export default function DemoApprovalsPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Sample approvals"
        description="A static view of the human approval queue. Approve and reject are disabled on sample pages."
      />

      {DEMO_PENDING_APPROVALS.length === 0 ? (
        <EmptyState
          title="Nothing waiting on an operator"
          description="No sample incident is paused at the approval node."
          icon={Shield02Icon}
        />
      ) : (
        <div className="space-y-3">
          {DEMO_PENDING_APPROVALS.map((approval) => (
            <article
              key={approval.approval_id}
              className="rounded-lg border border-chart-2/40 bg-card p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Link
                  href={`/demo/incidents/${encodeURIComponent(approval.incident_id as string)}`}
                  className="font-mono text-sm font-semibold underline-offset-4 hover:text-primary hover:underline"
                >
                  {approval.incident_id}
                </Link>
                {approval.action_type ? (
                  <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                    {approval.action_type}
                  </span>
                ) : null}
                {approval.runbook_id ? (
                  <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                    {approval.runbook_id}
                  </span>
                ) : null}
                {approval.confidence !== undefined ? (
                  <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                    {formatPercent(approval.confidence)} confidence
                  </span>
                ) : null}
              </div>

              {approval.target ? (
                <p className="mt-2 font-mono text-xs text-foreground">
                  {approval.target}
                </p>
              ) : null}

              {approval.reasons && approval.reasons.length > 0 ? (
                <ul className="mt-2 space-y-1">
                  {approval.reasons.map((reason) => (
                    <li key={reason} className="text-xs text-muted-foreground">
                      {reason}
                    </li>
                  ))}
                </ul>
              ) : null}

              {approval.enqueued_at ? (
                <p className="mt-2 font-mono text-[11px] tabular-nums text-muted-foreground">
                  queued {formatTimestamp(approval.enqueued_at)}
                </p>
              ) : null}

              <p className="mt-3 font-mono text-[11px] text-chart-1">
                Approve and reject are disabled on sample pages.
              </p>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
