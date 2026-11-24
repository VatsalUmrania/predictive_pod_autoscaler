import * as React from "react"
import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import { Alert02Icon, ArrowRight01Icon } from "@hugeicons/core-free-icons"

import { ControlShell } from "@/components/control-shell"

/**
 * Sample-data shell.
 *
 * The banner is always visible and uses the one accent colour that never appears on
 * a live page, so a sample view can never be mistaken for live telemetry.
 */
export default function DemoLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <ControlShell liveStatus={false}>
      <div className="mb-5 flex flex-col gap-2 rounded-lg border border-chart-1/40 bg-chart-1/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-2.5">
          <HugeiconsIcon
            icon={Alert02Icon}
            className="mt-0.5 size-4 shrink-0 text-chart-1"
          />
          <div className="min-w-0">
            <p className="text-xs font-semibold text-foreground">
              Sample data
            </p>
            <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
              This view renders a static fixture, not live NEXUS telemetry.
            </p>
          </div>
        </div>
        <Link
          href="/"
          className="inline-flex shrink-0 items-center gap-1 font-mono text-[11px] text-chart-1 underline-offset-4 hover:underline"
        >
          Open the live control plane
          <HugeiconsIcon icon={ArrowRight01Icon} className="size-3" />
        </Link>
      </div>
      {children}
    </ControlShell>
  )
}
