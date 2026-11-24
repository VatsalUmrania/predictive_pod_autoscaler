import type * as React from "react"
import { HugeiconsIcon } from "@hugeicons/react"

import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"

/**
 * Empty state for a region that has no data yet.
 *
 * Says what is missing and what would populate it, instead of rendering a blank box.
 */
export function EmptyState({
  title,
  description,
  icon,
  className,
  children,
}: {
  title: string
  description: string
  icon?: React.ComponentProps<typeof HugeiconsIcon>["icon"]
  className?: string
  children?: React.ReactNode
}) {
  return (
    <div className={className}>
      <Empty className="rounded-lg border border-dashed border-border bg-card/40 py-10">
        <EmptyHeader>
          {icon ? (
            <EmptyMedia variant="icon">
              <HugeiconsIcon icon={icon} className="size-5" />
            </EmptyMedia>
          ) : null}
          <EmptyTitle className="text-sm font-semibold">{title}</EmptyTitle>
          <EmptyDescription className="max-w-[52ch] text-xs">
            {description}
          </EmptyDescription>
        </EmptyHeader>
        {children}
      </Empty>
    </div>
  )
}
