"use client"

import * as React from "react"
import { useRouter } from "next/navigation"
import { HugeiconsIcon } from "@hugeicons/react"
import { Alert02Icon, RefreshIcon } from "@hugeicons/core-free-icons"

import { Button } from "@/components/ui/button"
import type { ApiErrorInfo } from "@/lib/nexus/errors"

export type { ApiErrorInfo }

/**
 * Honest failure panel for a region that could not load.
 *
 * There is no mock-data fallback anywhere in the live control plane, so this is what
 * an operator sees when the NEXUS engine cannot be reached.
 */
export function ApiError({
  error,
  title = "NEXUS engine unreachable",
  className,
}: {
  error: ApiErrorInfo
  title?: string
  className?: string
}) {
  const router = useRouter()
  const [isRetrying, setIsRetrying] = React.useState(false)

  const retry = () => {
    setIsRetrying(true)
    router.refresh()
    // The refresh re-renders the server tree. Clear the spinner either way.
    window.setTimeout(() => setIsRetrying(false), 800)
  }

  return (
    <div
      role="alert"
      className={
        "flex flex-col gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-xs sm:flex-row sm:items-center sm:justify-between " +
        (className ?? "")
      }
    >
      <div className="flex min-w-0 items-start gap-2.5">
        <HugeiconsIcon
          icon={Alert02Icon}
          className="mt-0.5 size-4 shrink-0 text-destructive"
        />
        <div className="min-w-0">
          <p className="font-semibold text-foreground">{title}</p>
          <p className="text-muted-foreground">{error.message}</p>
          {error.path ? (
            <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground/80">
              {error.status ? `HTTP ${error.status} ` : ""}
              {error.path}
            </p>
          ) : null}
        </div>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={retry}
        disabled={isRetrying}
        className="h-7 shrink-0 font-mono text-xs"
      >
        <HugeiconsIcon
          icon={RefreshIcon}
          className={isRetrying ? "size-3.5 animate-spin" : "size-3.5"}
        />
        Retry
      </Button>
    </div>
  )
}
