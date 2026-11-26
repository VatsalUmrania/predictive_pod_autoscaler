"use client"

import * as React from "react"
import { Suspense } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Activity01Icon,
  Audit01Icon,
  DashboardSquare01Icon,
  FileTextIcon,
  ChartLineData02Icon,
  Shield02Icon,
  Settings02Icon,
  PlayCircleIcon,
} from "@hugeicons/core-free-icons"

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { Skeleton } from "@/components/ui/skeleton"

type NavItem = {
  label: string
  href: string
  icon: React.ComponentProps<typeof HugeiconsIcon>["icon"]
}

const CONTROL_ITEMS: NavItem[] = [
  { label: "Overview", href: "/", icon: DashboardSquare01Icon },
  { label: "Incidents", href: "/incidents", icon: Activity01Icon },
  { label: "Approvals", href: "/approvals", icon: Shield02Icon },
  { label: "Predictions", href: "/predictions", icon: ChartLineData02Icon },
]

const GOVERNANCE_ITEMS: NavItem[] = [
  { label: "Audit trail", href: "/audit", icon: Audit01Icon },
  { label: "Reports", href: "/reports", icon: FileTextIcon },
  { label: "Policy", href: "/policy", icon: Settings02Icon },
]

function isPathActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/"
  return pathname === href || pathname.startsWith(`${href}/`)
}

function NavGroup({
  label,
  items,
  pathname,
  badges,
}: {
  label: string
  items: NavItem[]
  pathname: string
  badges?: Record<string, React.ReactNode>
}) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map((item) => {
            const active = isPathActive(pathname, item.href)
            return (
              <SidebarMenuItem key={item.href}>
                <SidebarMenuButton
                  isActive={active}
                  tooltip={item.label}
                  render={
                    <Link href={item.href} aria-current={active ? "page" : undefined} />
                  }
                >
                  <HugeiconsIcon icon={item.icon} className="size-4 shrink-0" />
                  <span>{item.label}</span>
                </SidebarMenuButton>
                {badges?.[item.href]}
              </SidebarMenuItem>
            )
          })}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}

/**
 * Navigation items.
 *
 * `usePathname()` reads the URL, which is only known at request time for routes with
 * dynamic segments. Keeping the read in this leaf lets the rest of the sidebar stay
 * in the prerendered shell.
 */
function SidebarNav({ pendingBadge }: { pendingBadge?: React.ReactNode }) {
  const pathname = usePathname()

  return (
    <>
      <NavGroup
        label="Control"
        items={CONTROL_ITEMS}
        pathname={pathname}
        badges={{ "/approvals": pendingBadge }}
      />
      <NavGroup label="Governance" items={GOVERNANCE_ITEMS} pathname={pathname} />

      <SidebarGroup>
        <SidebarGroupLabel>Sample data</SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                isActive={isPathActive(pathname, "/demo")}
                tooltip="Sample control plane"
                render={<Link href="/demo" />}
              >
                <HugeiconsIcon icon={PlayCircleIcon} className="size-4 shrink-0" />
                <span>Sample control plane</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    </>
  )
}

function NavSkeleton() {
  return (
    <div className="space-y-4 px-2" aria-hidden="true">
      {[4, 3, 1].map((count, group) => (
        <div key={group} className="space-y-1">
          <Skeleton className="h-3 w-20" />
          {Array.from({ length: count }).map((_, i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      ))}
    </div>
  )
}

/**
 * Primary navigation for the control plane.
 *
 * `pendingBadge` and `footerStatus` are server-rendered slots so the live engine
 * state streams in without blocking the navigation itself.
 */
export function AppSidebar({
  pendingBadge,
  footerStatus,
}: {
  pendingBadge?: React.ReactNode
  footerStatus?: React.ReactNode
}) {
  return (
    <Sidebar collapsible="icon" variant="inset">
      <SidebarHeader>
        <SidebarTrigger className="hidden size-7 md:inline-flex" />
      </SidebarHeader>

      <SidebarContent>
        <Suspense fallback={<NavSkeleton />}>
          <SidebarNav pendingBadge={pendingBadge} />
        </Suspense>
      </SidebarContent>

      <SidebarFooter>
        <div className="flex items-center gap-2 rounded-md border border-sidebar-border px-2 py-1.5 text-xs group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-1.5">
          <div className="flex min-w-0 flex-1 items-center gap-2 group-data-[collapsible=icon]:hidden">
            {footerStatus ?? (
              <span className="min-w-0 flex-1 truncate text-sidebar-foreground/60">
                Checking engine
              </span>
            )}
          </div>
        </div>
      </SidebarFooter>
    </Sidebar>
  )
}
