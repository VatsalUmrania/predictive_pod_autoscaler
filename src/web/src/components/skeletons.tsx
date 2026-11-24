import { Skeleton } from "@/components/ui/skeleton"

/**
 * Skeletons that match the shape of the region they stand in for.
 *
 * A generic spinner would collapse the layout on every load. These keep the grid so
 * the page does not jump when the data arrives.
 */

export function KpiRowSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-lg border border-border bg-card p-3.5"
          aria-hidden="true"
        >
          <Skeleton className="h-3 w-20" />
          <Skeleton className="mt-2.5 h-7 w-14" />
          <Skeleton className="mt-2 h-3 w-28" />
        </div>
      ))}
    </div>
  )
}

export function TableSkeleton({
  rows = 6,
  columns = 5,
}: {
  rows?: number
  columns?: number
}) {
  return (
    <div
      className="overflow-hidden rounded-lg border border-border"
      aria-hidden="true"
    >
      <div className="border-b border-border bg-muted/30 px-4 py-2.5">
        <Skeleton className="h-3 w-40" />
      </div>
      <div className="divide-y divide-border">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="flex items-center gap-4 px-4 py-3">
            {Array.from({ length: columns }).map((_, c) => (
              <Skeleton
                key={c}
                className="h-3"
                style={{ width: `${Math.max(10, 100 / columns - 6)}%` }}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

export function PanelSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div
      className="rounded-lg border border-border bg-card p-4"
      aria-hidden="true"
    >
      <Skeleton className="h-3.5 w-32" />
      <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="space-y-1.5">
            <Skeleton className="h-2.5 w-20" />
            <Skeleton className="h-3.5 w-24" />
          </div>
        ))}
      </div>
    </div>
  )
}

export function DetailHeaderSkeleton() {
  return (
    <div
      className="rounded-lg border border-border bg-card p-5"
      aria-hidden="true"
    >
      <div className="flex flex-wrap items-center gap-3">
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-5 w-24" />
        <Skeleton className="h-5 w-20" />
      </div>
      <div className="mt-3 flex flex-wrap gap-3">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="h-3 w-56" />
        <Skeleton className="h-3 w-28" />
      </div>
    </div>
  )
}

export function ListSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-4">
          <div className="flex items-center justify-between gap-3">
            <Skeleton className="h-3.5 w-28" />
            <Skeleton className="h-3.5 w-20" />
          </div>
          <Skeleton className="mt-2.5 h-3 w-full" />
          <Skeleton className="mt-1.5 h-3 w-3/4" />
        </div>
      ))}
    </div>
  )
}

export function PageSkeleton() {
  return (
    <div className="space-y-6" aria-hidden="true">
      <div className="flex items-center justify-between border-b border-border pb-4">
        <div className="space-y-2">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-3 w-64" />
        </div>
        <Skeleton className="h-7 w-32" />
      </div>
      <KpiRowSkeleton />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <TableSkeleton rows={4} columns={4} />
        <PanelSkeleton rows={6} />
      </div>
    </div>
  )
}
