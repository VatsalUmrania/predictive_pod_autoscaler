import * as React from "react"
import { Suspense } from "react"

import { ApiError } from "@/components/api-error"
import { EmptyState } from "@/components/empty-state"
import { PageHeader } from "@/components/page-header"
import { PolicyPicker } from "@/components/policy-picker"
import { PanelSkeleton } from "@/components/skeletons"
import { getIncidents, getPolicy } from "@/lib/nexus/client"
import { describeApiError } from "@/lib/nexus/errors"

export default function PolicyPage({
  searchParams,
}: {
  searchParams: Promise<{ app?: string }>
}) {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Self-heal policy"
        description="The resolved selfheal.yaml policy the NEXUS engine applies to each application, including any runtime overrides that are active."
      />
      <Suspense fallback={<PanelSkeleton rows={3} />}>
        <PolicyView searchParams={searchParams} />
      </Suspense>
    </div>
  )
}

async function PolicyView({
  searchParams,
}: {
  searchParams: Promise<{ app?: string }>
}) {
  const { app } = await searchParams

  let incidents
  try {
    incidents = await getIncidents(200)
  } catch (error) {
    return <ApiError error={describeApiError(error)} title="Incident feed unavailable" />
  }

  const apps = Array.from(
    new Set(incidents.map((i) => i.service_name).filter((name): name is string => !!name))
  ).sort()

  return (
    <div className="space-y-4">
      {apps.length === 0 ? (
        <EmptyState
          title="No applications known yet"
          description="The NEXUS engine has not reported an incident for any application, so there is no service name to look a policy up by."
        />
      ) : (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <PolicyPicker apps={apps} selected={app} />
          <p className="font-mono text-[11px] tabular-nums text-muted-foreground">
            {apps.length} application{apps.length === 1 ? "" : "s"} seen by the engine
          </p>
        </div>
      )}

      {app ? <PolicyDetail app={app} /> : null}
    </div>
  )
}

async function PolicyDetail({ app }: { app: string }) {
  let policy
  try {
    policy = await getPolicy(app)
  } catch (error) {
    const info = describeApiError(error)
    if (info.status === 404) {
      return (
        <EmptyState
          title={`No policy published for ${app}`}
          description="The NEXUS engine has no selfheal.yaml policy cached for this application."
        />
      )
    }
    return <ApiError error={info} title="Policy unavailable" />
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <h2 className="font-mono text-sm font-semibold">{app}</h2>
        {policy._overrides_active ? (
          <span className="rounded border border-chart-2/40 bg-chart-2/10 px-2 py-0.5 font-mono text-[11px] font-semibold text-chart-2">
            runtime overrides active
          </span>
        ) : null}
      </div>
      <pre className="max-h-[32rem] overflow-auto p-4 font-mono text-[11px] leading-relaxed">
        {JSON.stringify(policy, null, 2)}
      </pre>
    </section>
  )
}
