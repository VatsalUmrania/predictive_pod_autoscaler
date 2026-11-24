import { getHealth, getStatus } from "@/lib/nexus/client"
import { formatUptime } from "@/lib/format"
import { SidebarMenuBadge } from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"

/**
 * Live engine status for the sidebar footer.
 *
 * Reads `/health` for liveness and `/status` for the uptime figure. When the engine
 * is unreachable the panel says so rather than showing a stale or invented value.
 */
export async function EngineStatus() {
  const [health, status] = await Promise.allSettled([getHealth(), getStatus()])

  const reachable = health.status === "fulfilled"
  const uptimeSeconds =
    health.status === "fulfilled"
      ? health.value.uptime_seconds
      : status.status === "fulfilled"
        ? status.value.uptime_seconds
        : undefined

  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      <span
        aria-hidden="true"
        className={cn(
          "size-2 shrink-0 rounded-full",
          reachable ? "bg-primary" : "bg-destructive"
        )}
      />
      <span className="min-w-0 truncate text-sidebar-foreground/80">
        {reachable
          ? `Engine up ${formatUptime(uptimeSeconds)}`
          : "Engine unreachable"}
      </span>
    </span>
  )
}

/** Count of incidents currently paused at the approval node. */
export async function SidebarPendingBadge() {
  const count = await getPendingApprovalCount()
  if (!count) return null
  return <SidebarMenuBadge className="text-chart-2">{count}</SidebarMenuBadge>
}

async function getPendingApprovalCount(): Promise<number> {
  try {
    const status = await getStatus()
    return status.workflow?.pending_approvals ?? 0
  } catch {
    return 0
  }
}
