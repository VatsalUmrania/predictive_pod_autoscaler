"use client"

import * as React from "react"
import { HugeiconsIcon } from "@hugeicons/react"
import { Alert02Icon, CheckmarkCircle02Icon } from "@hugeicons/core-free-icons"

import { Button } from "@/components/ui/button"
import { setPrescalerModeAction } from "@/lib/nexus/actions"
import { cn } from "@/lib/utils"

const MODES = ["shadow", "advisory", "autonomous"] as const
type Mode = (typeof MODES)[number]

const MODE_DESCRIPTION: Record<Mode, string> = {
  shadow: "Decisions are logged and scored, cluster state is never changed.",
  advisory: "Decisions are published to operators, cluster state is never changed.",
  autonomous: "Decisions are applied to the cluster.",
}

/**
 * Autonomy mode switcher.
 *
 * Posts to `POST /prescaler/mode/{mode}` and shows the engine's real response. The
 * mode descriptions state what each mode does; they do not claim what the engine is
 * currently doing.
 */
export function PrescalerModeSwitch({
  currentMode,
}: {
  currentMode?: string
}) {
  const [isPending, startTransition] = React.useTransition()
  const [result, setResult] = React.useState<
    | { ok: true; data: { status: string; result: string } }
    | { ok: false; error: { message: string; status: number; path: string } }
    | null
  >(null)

  const change = (mode: Mode) => {
    startTransition(async () => {
      setResult(await setPrescalerModeAction(mode))
    })
  }

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Autonomy mode</h2>
        {currentMode ? (
          <span className="rounded border border-border bg-muted px-2 py-0.5 font-mono text-[11px] font-semibold text-muted-foreground">
            engine reports: {currentMode}
          </span>
        ) : null}
      </div>

      <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        {MODES.map((mode) => {
          const isCurrent = currentMode?.toLowerCase() === mode
          return (
            <div
              key={mode}
              className={cn(
                "rounded-md border p-3",
                isCurrent
                  ? "border-chart-2/40 bg-chart-2/10"
                  : "border-border bg-background"
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs font-semibold">{mode}</span>
                {isCurrent ? (
                  <HugeiconsIcon
                    icon={CheckmarkCircle02Icon}
                    className="size-3.5 text-chart-2"
                  />
                ) : null}
              </div>
              <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
                {MODE_DESCRIPTION[mode]}
              </p>
              <Button
                variant={isCurrent ? "outline" : "secondary"}
                size="sm"
                disabled={isPending || isCurrent}
                onClick={() => change(mode)}
                className={cn(
                  "mt-2.5 h-7 w-full font-mono text-xs",
                  !isCurrent &&
                    "border-chart-2/40 bg-chart-2/20 text-chart-2 hover:bg-chart-2/30"
                )}
              >
                {isCurrent ? "Active" : `Switch to ${mode}`}
              </Button>
            </div>
          )
        })}
      </div>

      {result ? (
        <div
          role="status"
          className={cn(
            "mt-3 rounded-md border p-3 text-xs",
            result.ok
              ? "border-border bg-muted/40"
              : "border-destructive/30 bg-destructive/10 text-destructive"
          )}
        >
          {result.ok ? (
            <>
              <p className="font-semibold text-foreground">
                Engine responded: {result.data.status}
              </p>
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                {result.data.result}
              </p>
            </>
          ) : (
            <div className="flex items-start gap-2">
              <HugeiconsIcon icon={Alert02Icon} className="mt-0.5 size-4 shrink-0" />
              <div className="min-w-0">
                <p className="font-semibold">The mode was not changed</p>
                <p>{result.error.message}</p>
                {result.error.path ? (
                  <p className="mt-1 truncate font-mono text-[11px]">
                    {result.error.status ? `HTTP ${result.error.status} ` : ""}
                    {result.error.path}
                  </p>
                ) : null}
              </div>
            </div>
          )}
        </div>
      ) : null}
    </section>
  )
}
