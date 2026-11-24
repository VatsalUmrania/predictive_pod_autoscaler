import Link from "next/link"
import type * as React from "react"
import { HugeiconsIcon } from "@hugeicons/react"

import { cn } from "@/lib/utils"

export type KpiTone = "default" | "attention" | "success" | "failure"

const VALUE_TONE: Record<KpiTone, string> = {
  default: "text-foreground",
  attention: "text-chart-2",
  success: "text-primary",
  failure: "text-destructive",
}

/**
 * One metric tile.
 *
 * Every value is a real number pulled from the NEXUS API. The value is rendered in
 * Geist Mono with tabular figures so columns of numbers line up and do not shift as
 * they update.
 */
export function KpiTile({
  label,
  value,
  hint,
  href,
  icon,
  tone = "default",
  className,
}: {
  label: string
  value: React.ReactNode
  hint?: string
  href?: string
  icon?: React.ComponentProps<typeof HugeiconsIcon>["icon"]
  tone?: KpiTone
  className?: string
}) {
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        {icon ? (
          <HugeiconsIcon
            icon={icon}
            className="size-4 shrink-0 text-muted-foreground"
          />
        ) : null}
      </div>
      <div
        className={cn(
          "mt-2 font-mono text-2xl font-bold tabular-nums tracking-tight",
          VALUE_TONE[tone]
        )}
      >
        {value}
      </div>
      {hint ? (
        <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </>
  )

  const shell = cn(
    "block rounded-lg border border-border bg-card p-3.5 transition-colors",
    href && "hover:border-primary/40 hover:bg-muted/10",
    className
  )

  if (href) {
    return (
      <Link href={href} className={shell}>
        {body}
      </Link>
    )
  }

  return <div className={shell}>{body}</div>
}
