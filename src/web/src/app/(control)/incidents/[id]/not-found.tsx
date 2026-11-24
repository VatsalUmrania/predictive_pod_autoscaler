import Link from "next/link"
import { HugeiconsIcon } from "@hugeicons/react"
import { Alert02Icon, ArrowLeft01Icon } from "@hugeicons/core-free-icons"

import { Button } from "@/components/ui/button"

export default function IncidentNotFound() {
  return (
    <div className="flex min-h-[60dvh] flex-col items-center justify-center text-center">
      <div className="max-w-md space-y-4 rounded-lg border border-border bg-card p-8">
        <HugeiconsIcon
          icon={Alert02Icon}
          className="mx-auto size-7 text-destructive"
        />
        <h2 className="font-heading text-lg font-bold tracking-tight text-foreground">
          Incident not found
        </h2>
        <p className="text-xs leading-relaxed text-muted-foreground">
          The NEXUS engine has no record of this incident id. It may never have
          existed, or it may have aged out of the feed.
        </p>
        <Link href="/incidents">
          <Button variant="outline" size="sm" className="h-8 text-xs">
            <HugeiconsIcon icon={ArrowLeft01Icon} className="size-3.5" />
            Back to incident feed
          </Button>
        </Link>
      </div>
    </div>
  )
}
