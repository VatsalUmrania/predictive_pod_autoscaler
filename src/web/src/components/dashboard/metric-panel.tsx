import type * as React from "react"
import { HugeiconsIcon } from "@hugeicons/react"

import { cn } from "@/lib/utils"

export type MetricItem = {
  label: string
  value: React.ReactNode
  tone?: "default" | "attention" | "success" | "failure"
}

const VALUE_TONE: Record<NonNullable<MetricItem["tone"]>, string> = {
  default: "text-foreground",
  attention: "text-chart-2",
  success: "text-primary",
  failure: "text-destructive",
}

/**
 * A panel of label and value pairs.
 *
 * Used for the engine status blocks (prescaler, learning, workflow) where the data is
 * a fixed set of named metrics rather than a list.
 */
export function MetricPanel({
  title,
  icon,
  items,
  columns = 2,
  className,
}: {
  title: string
  icon?: React.ComponentProps<typeof HugeiconsIcon>["icon"]
  items: MetricItem[]
  columns?: 1 | 2 | 3
  className?: string
}) {
  return (
    <section
      className={cn("rounded-lg border border-border bg-card p-4", className)}
    >
      <div className="flex items-center gap-2">
        {icon ? (
          <HugeiconsIcon icon={icon} className="size-4 text-muted-foreground" />
        ) : null}
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      </div>
      <dl
        className={cn(
          "mt-3.5 grid gap-x-6 gap-y-3",
          columns === 1
            ? "grid-cols-1"
            : columns === 3
              ? "grid-cols-2 sm:grid-cols-3"
              : "grid-cols-2"
        )}
      >
        {items.map((item) => (
          <div key={item.label} className="min-w-0">
            <dt className="text-[11px] text-muted-foreground">{item.label}</dt>
            <dd
              className={cn(
                "mt-0.5 truncate font-mono text-sm font-semibold tabular-nums",
                VALUE_TONE[item.tone ?? "default"]
              )}
              title={typeof item.value === "string" ? item.value : undefined}
            >
              {item.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
