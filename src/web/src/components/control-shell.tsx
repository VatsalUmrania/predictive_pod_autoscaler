import * as React from "react"
import { Suspense } from "react"

import { AppSidebar } from "@/components/app-sidebar"
import { EngineStatus, SidebarPendingBadge } from "@/components/engine-status"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"

/**
 * Application shell: sidebar navigation, a slim top bar and the page surface.
 *
 * The live engine status and the pending approval count are server-rendered slots
 * inside their own Suspense boundaries, so navigation renders immediately and the
 * counters stream in behind it.
 */
export function ControlShell({
  children,
  liveStatus = true,
}: {
  children: React.ReactNode
  liveStatus?: boolean
}) {
  return (
    <SidebarProvider>
      <AppSidebar
        pendingBadge={
          liveStatus ? (
            <Suspense fallback={null}>
              <SidebarPendingBadge />
            </Suspense>
          ) : null
        }
        footerStatus={
          liveStatus ? (
            <Suspense
              fallback={
                <span className="min-w-0 flex-1 truncate text-sidebar-foreground/60">
                  Checking engine
                </span>
              }
            >
              <EngineStatus />
            </Suspense>
          ) : (
            <span className="min-w-0 flex-1 truncate text-sidebar-foreground/80">
              Sample data
            </span>
          )
        }
      />
      <SidebarInset className="min-h-[100dvh]">
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3 sm:px-4">
          <SidebarTrigger className="size-7" />
          <span className="font-heading text-xs font-semibold tracking-tight lg:hidden">
            NEXUS
          </span>
        </div>
        <div className="flex-1 px-4 py-5 sm:px-6 sm:py-6">
          <div className="mx-auto w-full max-w-7xl">{children}</div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
