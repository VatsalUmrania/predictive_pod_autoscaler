"use client"

import * as React from "react"
import Link from "next/link"
import Image from "next/image"
import { usePathname } from "next/navigation"
import { useIncidents } from "@/context/incident-context"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { HugeiconsIcon } from "@hugeicons/react"
import {
  Layers01Icon,
  Shield02Icon,
  ChartLineData02Icon,
  Sun01Icon,
  Moon01Icon,
  DashboardSquare01Icon,
} from "@hugeicons/core-free-icons"

interface NavigationHeaderProps {
  className?: string
}

export function NavigationHeader({ className }: NavigationHeaderProps) {
  const pathname = usePathname()
  const { incidents, isOnline, isDemoMode } = useIncidents()
  const [theme, setTheme] = React.useState<"light" | "dark">("dark")

  React.useEffect(() => {
    const timer = setTimeout(() => {
      try {
        const savedTheme = localStorage.getItem("nexus-theme") as
          | "light"
          | "dark"
          | null
        const prefersDark = window.matchMedia(
          "(prefers-color-scheme: dark)"
        ).matches
        const activeTheme = savedTheme || (prefersDark ? "dark" : "light")
        setTheme(activeTheme)
        document.documentElement.classList.toggle(
          "dark",
          activeTheme === "dark"
        )
      } catch {
        // ignore SSR
      }
    }, 0)

    return () => clearTimeout(timer)
  }, [])

  const toggleTheme = () => {
    const nextTheme = theme === "dark" ? "light" : "dark"
    setTheme(nextTheme)
    document.documentElement.classList.toggle("dark", nextTheme === "dark")
    try {
      localStorage.setItem("nexus-theme", nextTheme)
    } catch {
      // ignore
    }
  }

  const awaitingApprovalsCount = incidents.filter(
    (i) => i.approval.is_interrupted
  ).length

  return (
    <header
      className={cn(
        "sticky top-0 z-40 flex items-center justify-between border-b border-border bg-background/80 px-4 py-3 backdrop-blur-md transition-colors sm:px-6",
        className
      )}
    >
      {/* Brand & Nav */}
      <div className="flex items-center gap-6">
        <Link href="/" className="flex items-center gap-2.5 group">
          <div className="flex items-center justify-center shrink-0">
            {/* Light mode logo (dark fill) */}
            <Image
              src="/logo-dark.svg"
              alt="NEXUS Logo"
              width={34}
              height={24}
              className="block dark:hidden h-6 w-auto object-contain"
              priority
            />
            {/* Dark mode logo (light fill) */}
            <Image
              src="/logo-light.svg"
              alt="NEXUS Logo"
              width={34}
              height={24}
              className="hidden dark:block h-6 w-auto object-contain"
              priority
            />
          </div>
          <div>
            <span className="font-heading text-sm font-bold tracking-tight text-foreground group-hover:text-primary transition-colors">
              NEXUS
            </span>
          </div>
        </Link>

        {/* Navigation links */}
        <nav className="flex items-center gap-1 text-xs" aria-label="Views">
          <Link
            href="/"
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors",
              pathname === "/"
                ? "bg-muted font-semibold text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <HugeiconsIcon icon={DashboardSquare01Icon} className="size-3.5" />
            <span>Control Panel</span>
          </Link>

          <Link
            href="/threads"
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors",
              pathname === "/threads" || pathname.startsWith("/incidents")
                ? "bg-muted font-semibold text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <HugeiconsIcon icon={Layers01Icon} className="size-3.5" />
            <span>Threads</span>
            <span className="rounded-full border border-border bg-muted/60 px-1.5 py-0.2 font-mono text-[10px] text-muted-foreground">
              {incidents.length}
            </span>
          </Link>

          <Link
            href="/approvals"
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors",
              pathname === "/approvals"
                ? "bg-muted font-semibold text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <HugeiconsIcon icon={Shield02Icon} className="size-3.5" />
            <span>Approvals</span>
            {awaitingApprovalsCount > 0 && (
              <span className="rounded-full border border-chart-2/30 bg-chart-2/10 px-1.5 py-0.2 font-mono text-[10px] text-chart-2">
                {awaitingApprovalsCount}
              </span>
            )}
          </Link>

          <Link
            href="/predictions"
            className={cn(
              "flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors",
              pathname === "/predictions"
                ? "bg-muted font-semibold text-foreground shadow-xs"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            <HugeiconsIcon icon={ChartLineData02Icon} className="size-3.5" />
            <span>Predictive Scaling</span>
          </Link>
        </nav>
      </div>

      {/* Right Actions */}
      <div className="flex items-center gap-3">
        {/* Engine status indicator */}
        <div className="flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs font-mono">
          <span
            className={cn(
              "size-2 rounded-full",
              isDemoMode
                ? "bg-chart-2"
                : isOnline
                  ? "bg-primary"
                  : "bg-destructive"
            )}
          />
          <span className="text-muted-foreground">
            {isDemoMode ? "Preview" : isOnline ? "Online" : "Offline"}
          </span>
        </div>

        {/* Theme Toggle */}
        <Button
          variant="ghost"
          size="sm"
          onClick={toggleTheme}
          className="size-8 p-0"
          title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
        >
          <HugeiconsIcon
            icon={theme === "dark" ? Sun01Icon : Moon01Icon}
            className="size-4 text-foreground"
          />
          <span className="sr-only">Toggle theme</span>
        </Button>
      </div>
    </header>
  )
}
